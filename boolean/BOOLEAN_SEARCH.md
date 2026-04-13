# Boolean Search Backend — Technical Documentation

**Module:** `backend/boolean/`  
**Stack:** FastAPI · PostgreSQL · psycopg2  
**Database:** `legal_knowledge_graph`  
**Status:** Production-ready

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [File Reference](#3-file-reference)
4. [Query Pipeline](#4-query-pipeline)
5. [Supported Syntax](#5-supported-syntax)
6. [API Endpoints](#6-api-endpoints)
7. [Request & Response Shapes](#7-request--response-shapes)
8. [Filters Reference](#8-filters-reference)
9. [Database Schema Used](#9-database-schema-used)
10. [Indexes](#10-indexes)
11. [Relevance Scoring](#11-relevance-scoring)
12. [Error Reference](#12-error-reference)
13. [Mounting in FastAPI](#13-mounting-in-fastapi)
14. [Running Tests](#14-running-tests)
15. [Performance Notes](#15-performance-notes)
16. [Extending the Module](#16-extending-the-module)

---

## 1. Overview

The Boolean search module provides SCC Online / Manupatra–style legal case search over the `legal_knowledge_graph` PostgreSQL database. It supports:

- Full Boolean operators: `AND`, `OR`, `NOT`
- Proximity operators: `W/n`, `NEAR/n`, `PRE/n`, `/S`, `/P`
- Exact phrase matching: `"natural justice"`
- Wildcard matching: `constitu*`, `wom?n`, `contract!`
- Minimum occurrence counts: `atleast3(negligence)`
- Field-qualified search: `court:`, `judge:`, `act:`, `section:`, `article:`, `year:` and more
- Operator precedence: `NOT > Proximity > AND > OR`
- Grouped expressions with parentheses
- KWIC (KeyWord In Context) highlighted snippets per result
- Composite relevance scoring with 6 weighted factors
- Full filter support: court, year range, act, section, judge, doc type

---

## 2. Architecture

```
Raw query string (from frontend)
        │
        ▼
┌─────────────────┐
│  validator.py   │  ← Syntax check, fail-fast, 9 rules
│                 │    Returns ValidationResult(valid, error)
└────────┬────────┘
         │ valid only
         ▼
┌─────────────────┐
│   parser.py     │  ← Tokenise → recursive-descent AST
│                 │    Returns root AST node
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  executor.py    │  ← Walk AST → parameterised SQL
│                 │    Uses GIN tsvector indexes
│                 │    Returns (sql_string, params_list)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  PostgreSQL     │  ← Executes on legal_paragraphs + legal_cases
│  (via db.py)    │    INTERSECT / UNION / EXCEPT / tsvector / regex
└────────┬────────┘
         │ raw rows
         ▼
┌─────────────────┐
│   ranker.py     │  ← Composite relevance score per case
│                 │    6 weighted factors
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ highlighter.py  │  ← KWIC snippet extraction
│                 │    ts_headline parsing → span list
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   router.py     │  ← FastAPI endpoints
│                 │    Pydantic validation, error handling
│                 │    Returns SearchResponse JSON
└─────────────────┘
```

### Key design decisions

**Paragraph-level matching, case-level results.** All boolean matching runs against `legal_paragraphs.text` (not the entire case). This enables proximity operators to work correctly — `W/5` checks that two terms appear within 5 words inside a single paragraph, not somewhere across a 300-page judgment. Final results are aggregated to case level.

**INTERSECT / UNION / EXCEPT for set operations.** AND maps to SQL `INTERSECT`, OR maps to `UNION`, NOT maps to `NOT IN`. This keeps the SQL clean and leverages PostgreSQL's query optimiser.

**GIN tsvector indexes.** The existing indexes `idx_legal_paragraphs_text` and `idx_paragraphs_text_search` on `to_tsvector('english', text)` are used directly. No external search engine needed.

**Regex for proximity.** PostgreSQL's native tsvector does not support word-proximity. Proximity operators (`W/n`, `NEAR/n`, `PRE/n`, `/S`) use PostgreSQL's `~*` (case-insensitive regex match) on paragraph text after first filtering to matching case_ids via tsvector.

---

## 3. File Reference

```
backend/
└── boolean/
    ├── __init__.py          Exports router + public API
    ├── validator.py         Query syntax validation (no DB, no parsing)
    ├── parser.py            Tokeniser + recursive-descent AST builder
    ├── executor.py          AST → parameterised SQL
    ├── filters.py           Filter normalisation, alias resolution, SQL clauses
    ├── ranker.py            Composite relevance scoring + re-ranking
    ├── highlighter.py       KWIC snippet extraction, ts_headline parsing
    ├── exceptions.py        Exception hierarchy with HTTP status codes
    ├── router.py            FastAPI router (all endpoints)
    ├── index_setup.sql      Idempotent index creation script
    └── tests/
        ├── __init__.py
        └── test_boolean.py  Full unit test suite (no DB required)
```

---

## 4. Query Pipeline

### Step-by-step for `POST /boolean/search`

**Step 1 — Validation** (`validator.py`)

Runs 9 sequential checks on the raw string before touching any other module:

| Check | Example failure |
|-------|----------------|
| Empty / length | `""` or query > 2000 chars |
| Quote pairing | `"natural justice` (no closing quote) |
| Leading wildcard | `*term` |
| Parentheses balance | `(murder OR homicide` |
| Paren depth | More than 10 nesting levels |
| Operator position | `AND murder` / `murder AND` |
| Consecutive operators | `murder AND AND intention` |
| Proximity validity | `W/0`, `NEAR/101`, `W/5` at start |
| Atleast syntax | `atleast(term)` (missing n), `atleast3 term` (missing parens) |
| Field qualifier names | `colour:red` (unknown field) |

Returns `ValidationResult(valid, error)`. On failure, the error message is returned directly to the user — it is already human-readable.

**Step 2 — Parsing** (`parser.py`)

Tokenises the validated query string and builds an AST via recursive descent.

Operator precedence (high → low):
```
NOT   (unary, right-binding)
W/n  NEAR/n  PRE/n  /S  /P   (proximity)
AND   (explicit and implicit)
OR
```

Parentheses override precedence normally.

**Step 3 — SQL Building** (`executor.py`)

Walks the AST depth-first and builds parameterised SQL. Every node type maps to a SQL pattern:

| Node | SQL pattern |
|------|-------------|
| `TermNode` | `WHERE to_tsvector('english', p.text) @@ plainto_tsquery('english', %s)` |
| `PhraseNode` | `WHERE ... @@ phraseto_tsquery('english', %s)` |
| `WildcardNode *` | `WHERE ... @@ to_tsquery('english', %s)` with `root:*` |
| `WildcardNode ?` | `WHERE p.text ILIKE %s` |
| `AtleastNode` | `LENGTH` subtraction count ≥ n |
| `AndNode` | `(left_sql) INTERSECT (right_sql)` |
| `OrNode` | `(left_sql) UNION (right_sql)` |
| `NotNode` | `SELECT case_id FROM legal_cases WHERE case_id NOT IN (inner_sql)` |
| `ProximityNode W/n` | Regex `~*` ordered word-window on paragraph text |
| `ProximityNode NEAR/n` | Regex `~*` either direction word-window |
| `ProximityNode /S` | Regex within 30 words (sentence approximation) |
| `ProximityNode /P` | INTERSECT only (same paragraph = same row) |
| `FieldNode court:` | `legal_cases.court ILIKE %s` |
| `FieldNode judge:` | `unnest(legal_paragraphs.judges_mentioned) ILIKE %s` |
| `FieldNode act:` | `case_acts.act_name ILIKE %s` |
| `FieldNode section:` | `case_acts.section ILIKE %s` |
| `FieldNode article:` | `unnest(legal_cases.constitutional_articles) ILIKE %s` |
| `FieldNode year:` | `legal_cases.year = %s` |

**Step 4 — Result wrapping** (`executor.build_result_query`)

The boolean core SQL (which returns a set of `case_id` values) is wrapped in a CTE:

```sql
WITH boolean_matches AS ( <core boolean sql> ),
     case_acts_agg AS ( ... ),
     citation_counts AS ( ... )
SELECT lc.*, ... FROM boolean_matches
JOIN legal_cases lc ON lc.case_id = bm.case_id
WHERE <filter clauses>
ORDER BY <sort>
LIMIT %s OFFSET %s
```

**Step 5 — Re-ranking** (`ranker.py`)

After the DB returns results, `rerank_results()` computes a composite score per case and re-sorts if `sort_by=relevance`.

**Step 6 — Snippet generation** (`highlighter.py`)

For each result case, `build_snippet_query()` fetches the top 3 matching paragraphs using `ts_rank` + `ts_headline`. The `ts_headline` output uses `<<<term>>>` delimiters which `parse_ts_headline()` converts to a structured span list for the frontend.

---

## 5. Supported Syntax

### Boolean Operators

| Operator | Example | Meaning |
|----------|---------|---------|
| `AND` | `murder AND intention` | Both terms must appear |
| `OR` | `murder OR homicide` | Either term |
| `NOT` | `murder NOT attempt` | Exclude cases with term |
| `AND NOT` | `murder AND NOT attempt` | Explicit exclude |
| *(adjacent)* | `negligence duty` | Implicit AND |

### Proximity Operators

| Operator | Example | Meaning |
|----------|---------|---------|
| `W/n` | `arrest W/5 warrant` | Left term precedes right within n words (ordered) |
| `NEAR/n` | `police NEAR/3 custody` | Either term within n words of the other (unordered) |
| `PRE/n` | `bail PRE/4 anticipatory` | Left term precedes right within n words (same as W/n) |
| `/S` | `negligence /S duty` | Both terms in the same sentence (approx: ≤30 words apart) |
| `/P` | `negligence /P breach` | Both terms in the same paragraph |

### Phrase Search

| Syntax | Example | Meaning |
|--------|---------|---------|
| `"..."` | `"natural justice"` | Exact phrase match (consecutive words) |
| Field phrase | `court:"Supreme Court"` | Exact phrase in a specific field |

### Wildcards

| Character | Example | Meaning |
|-----------|---------|---------|
| `*` | `constitu*` | Prefix match: constitution, constitutional, constitutionally… |
| `!` | `contract!` | Same as `*` (Manupatra-style) |
| `?` | `wom?n` | Single character substitution: woman, women |

> **Note:** Leading wildcards (`*term`) are not allowed.

### Minimum Occurrence

| Syntax | Example | Meaning |
|--------|---------|---------|
| `atleast<n>(<term>)` | `atleast3(negligence)` | Term appears at least 3 times in the same paragraph |

### Field-Qualified Search

| Field | Column mapped | Example |
|-------|--------------|---------|
| `court:` | `legal_cases.court` | `court:"Supreme Court"` |
| `judge:` or `judges:` | `legal_paragraphs.judges_mentioned[]` | `judge:Chandrachud` |
| `act:` | `case_acts.act_name` | `act:"Indian Penal Code"` |
| `section:` | `case_acts.section` | `section:302` |
| `article:` | `legal_cases.constitutional_articles[]` | `article:21` |
| `year:` | `legal_cases.year` | `year:2019` |
| `title:` or `case:` | `legal_cases.case_name` (tsvector) | `title:puttaswamy` |
| `petitioner:` | `legal_cases.petitioner` (tsvector) | `petitioner:maneka` |
| `respondent:` | `legal_cases.respondent` (tsvector) | `respondent:"Union of India"` |
| `keyword:` or `subject:` | `legal_cases.subject_tags[]` | `keyword:privacy` |
| `citation:` | `legal_cases.appeal_no` | `citation:"Civil Appeal 494"` |

### Grouping

Parentheses override operator precedence:
```
(murder OR "culpable homicide") AND intention AND NOT (attempt OR conspiracy)
```

### Complex examples

```
"natural justice" AND "audi alteram partem" AND court:"Supreme Court"

(murder OR "culpable homicide") AND section:302 AND NOT dismissed

constitu* AND article:21 AND year:2015

atleast3(negligence) AND judge:Chandrachud AND act:CPC

"bail" W/5 "anticipatory" AND NOT dismissed AND court:"Supreme Court"

(petitioner:puttaswamy OR respondent:puttaswamy) AND keyword:privacy
```

---

## 6. API Endpoints

All endpoints are prefixed with `/boolean`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/boolean/search` | Main search — returns paginated results with snippets |
| `POST` | `/boolean/validate` | Validate query syntax only, no DB touch |
| `POST` | `/boolean/parse` | Return AST as JSON (debug / frontend query tree) |
| `GET` | `/boolean/case/{case_id}` | Full case detail + acts + citations + paragraphs |
| `GET` | `/boolean/health` | Health check + case count |

---

## 7. Request & Response Shapes

### `POST /boolean/search`

**Request:**
```json
{
  "query": "\"natural justice\" AND article:21",
  "filters": {
    "court":     "Supreme Court of India",
    "year_from": 2010,
    "year_to":   2023,
    "act":       "Indian Penal Code",
    "section":   "302",
    "judge":     "D.Y. Chandrachud",
    "doc_type":  "judgment"
  },
  "sort_by":         "relevance",
  "page":            1,
  "page_size":       25,
  "include_snippets": true
}
```

**Response:**
```json
{
  "query":         "\"natural justice\" AND article:21",
  "total_results": 142,
  "page":          1,
  "page_size":     25,
  "total_pages":   6,
  "sort_by":       "relevance",
  "search_terms":  ["natural justice", "natural", "justice", "21"],
  "elapsed_ms":    84.3,
  "results": [
    {
      "case_id":               "SC_2017_PUTTASWAMY",
      "case_name":             "Justice K.S. Puttaswamy (Retd.) v. Union of India",
      "citation":              "Civil Appeal No. 10866 of 2012",
      "court":                 "Supreme Court of India",
      "court_type":            "SC",
      "year":                  2017,
      "date_of_order":         "2017-08-24",
      "petitioner":            "Justice K.S. Puttaswamy (Retd.)",
      "respondent":            "Union of India",
      "outcome":               "Allowed",
      "outcome_summary":       "Right to privacy held to be a fundamental right",
      "authority_score":       0.97,
      "citation_count":        2341,
      "cited_by_count":        2341,
      "constitutional_articles": ["Article 21", "Article 19", "Article 14"],
      "acts_referred":         ["Constitution of India"],
      "subject_tags":          ["privacy", "fundamental rights", "article 21"],
      "acts_list":             "Constitution of India",
      "sections_list":         null,
      "relevance_score":       94.2,
      "score_breakdown": {
        "authority":      0.97,
        "citation_count": 0.88,
        "cited_by":       0.88,
        "term_density":   1.0,
        "recency":        0.90,
        "court":          1.0,
        "final":          0.942
      },
      "snippet": {
        "case_id": "SC_2017_PUTTASWAMY",
        "matched_terms": ["natural justice", "21"],
        "total_paragraph_matches": 14,
        "fragments": [
          {
            "para_no":    47,
            "para_type":  "reasoning",
            "spans": [
              { "text": "The principle of ", "highlighted": false },
              { "text": "natural justice",   "highlighted": true },
              { "text": " is embedded in ",  "highlighted": false },
              { "text": "Article 21",        "highlighted": true },
              { "text": " of the Constitution...", "highlighted": false }
            ],
            "plain_text": "The principle of natural justice is embedded in Article 21..."
          }
        ]
      }
    }
  ]
}
```

### `POST /boolean/validate`

**Request:** `{ "query": "murder AND" }`

**Response (invalid):**
```json
{
  "valid": false,
  "error": "Query cannot end with operator \"AND\" — add a search term after it"
}
```

**Response (valid):** `{ "valid": true, "error": null }`

### `POST /boolean/parse`

**Request:** `{ "query": "\"natural justice\" AND article:21" }`

**Response:**
```json
{
  "query": "\"natural justice\" AND article:21",
  "ast": {
    "type": "AND",
    "left": {
      "type": "phrase",
      "value": "natural justice"
    },
    "right": {
      "type": "field",
      "field": "constitutional_articles",
      "operand": { "type": "term", "value": "21" }
    }
  }
}
```

---

## 8. Filters Reference

Filters are applied AFTER the boolean matching (in the result wrapper query, not in the boolean core SQL).

| Filter | Type | Description | Alias resolution |
|--------|------|-------------|-----------------|
| `court` | string | Substring match on `legal_cases.court` | `SC` → `Supreme Court of India`, `delhi hc` → `High Court of Delhi`, etc. |
| `year_from` | int | `legal_cases.year >= year_from` | Accepts `2015`, `"2015"`, `"01/01/2015"` |
| `year_to` | int | `legal_cases.year <= year_to` | Same as above |
| `act` | string | `case_acts.act_name ILIKE %act%` | `ipc` → `Indian Penal Code`, `crpc` → `Code of Criminal Procedure`, etc. |
| `section` | string | `case_acts.section ILIKE %section%` | Raw match, e.g. `"302"`, `"Section 302"` |
| `judge` | string | `judges_mentioned[] ILIKE %judge%` | Raw name, e.g. `"Chandrachud"` |
| `doc_type` | string | `lower(outcome) LIKE doc_type` | `"judgment"`, `"order"` |

**Court aliases available:** SC, Delhi HC, Bombay HC, Madras HC, Calcutta HC, Allahabad HC, Karnataka HC, Kerala HC, Gujarat HC, Punjab HC, Rajasthan HC, MP HC, Patna HC, NCLAT, NCDRC, SAT, NGT, AFT, and more.

**Act aliases available:** IPC, CrPC, CPC, IT Act, POCSO, NDPS, RERA, IBC, POSH, NIA, UAPA, PMLA, FEMA, RTI, MV Act, NI Act, Constitution.

---

## 9. Database Schema Used

The module reads from these tables (no writes):

| Table | Used for |
|-------|---------|
| `legal_paragraphs` | Primary boolean text search, proximity, KWIC snippets |
| `legal_cases` | Metadata, filters, result assembly |
| `case_acts` | `act:` and `section:` field searches + filter |
| `case_citations` | `cited_by_count` aggregation |

Key columns accessed:

**`legal_paragraphs`**
- `paragraph_id`, `case_id`, `para_no`, `para_type`
- `text` — primary search target (GIN indexed)
- `judges_mentioned[]` — for `judge:` field search
- `acts_mentioned[]`, `sections_mentioned[]` — paragraph-level act/section arrays
- `quality_score` — snippet ranking fallback
- `embedding` — not used by boolean (used by semantic search module)

**`legal_cases`**
- `case_id`, `case_name`, `appeal_no`, `court`, `court_type`, `year`, `date_of_order`
- `petitioner`, `respondent` — tsvector indexed
- `constitutional_articles[]` — for `article:` field search
- `subject_tags[]` — for `keyword:` field search
- `acts_referred[]` — display only
- `authority_score`, `citation_count` — relevance scoring
- `outcome`, `outcome_summary` — display + `doc_type` filter

**`case_acts`**
- `case_id`, `act_name`, `section`, `confidence`

**`case_citations`**
- `source_case_id`, `cited_case_id`, `relationship`, `confidence`

---

## 10. Indexes

### Already present (verified from your schema)

| Index | Table | Type | Used for |
|-------|-------|------|---------|
| `idx_legal_paragraphs_text` | `legal_paragraphs` | GIN tsvector | All text search |
| `idx_paragraphs_text_search` | `legal_paragraphs` | GIN tsvector | All text search (duplicate — both used) |
| `idx_cases_case_name` | `legal_cases` | GIN tsvector | `title:` field search |
| `idx_cases_petitioner` | `legal_cases` | GIN tsvector | `petitioner:` field search |
| `idx_cases_respondent` | `legal_cases` | GIN tsvector | `respondent:` field search |
| `idx_cases_acts_referred` | `legal_cases` | GIN array | acts array |
| `idx_cases_subject_tags` | `legal_cases` | GIN array | `keyword:` field |
| `idx_paragraphs_embedding_hnsw` | `legal_paragraphs` | HNSW | Semantic search (not boolean) |

### Added by `index_setup.sql`

| Index | Table | Type | Used for |
|-------|-------|------|---------|
| `idx_paragraphs_acts_mentioned` | `legal_paragraphs` | GIN array | `act:` paragraph-level search |
| `idx_paragraphs_judges_mentioned` | `legal_paragraphs` | GIN array | `judge:` field search |
| `idx_paragraphs_sections_mentioned` | `legal_paragraphs` | GIN array | `section:` paragraph-level |
| `idx_paragraphs_case_para_order` | `legal_paragraphs` | BTREE composite | Ordered paragraph retrieval |
| `idx_cases_constitutional_articles` | `legal_cases` | GIN array | `article:` field search |
| `idx_cases_court_year` | `legal_cases` | BTREE composite | court + year filter |
| `idx_cases_authority_score` | `legal_cases` | BTREE | Relevance ordering |
| `idx_cases_citation_count` | `legal_cases` | BTREE | Citation sort |
| `idx_cases_date_of_order` | `legal_cases` | BTREE | Date sort |
| `idx_case_acts_case_act` | `case_acts` | BTREE composite | Act filter lookup |
| `idx_citations_cited_relationship` | `case_citations` | BTREE composite | Cited-by with relationship |

Run setup once:
```bash
psql -U postgres -d legal_knowledge_graph -f backend/boolean/index_setup.sql
```

---

## 11. Relevance Scoring

When `sort_by=relevance`, results are re-ranked after DB retrieval using a 6-factor composite score.

| Factor | Weight | Source | Logic |
|--------|--------|--------|-------|
| `authority` | 30% | `legal_cases.authority_score` | Already normalised 0–1 |
| `citation_count` | 20% | `legal_cases.citation_count` | Log-scaled: log(count) / log(10000) |
| `cited_by` | 15% | `case_citations` aggregate | Same log-scale as citation_count |
| `term_density` | 15% | `matched_para_count / total_paragraphs` | Amplified ×5, clamped to 1.0 |
| `recency` | 10% | `legal_cases.year` | Linear: (year − 1950) / (2025 − 1950) |
| `court_hierarchy` | 10% | `legal_cases.court_type` | SC=1.0, HC=0.70, Tribunal=0.40 |

Final score returned as `relevance_score` (0–100 scale for display) on each result. `score_breakdown` shows individual factors (0–1 scale).

---

## 12. Error Reference

All errors return structured JSON:

```json
{
  "error":   "QUERY_VALIDATION_ERROR",
  "message": "Human-readable message shown to user",
  "detail":  "Technical detail for logs"
}
```

| Exception class | HTTP | Code | Trigger |
|----------------|------|------|---------|
| `QueryValidationError` | 422 | `QUERY_VALIDATION_ERROR` | Invalid query syntax |
| `QueryParseError` | 422 | `QUERY_PARSE_ERROR` | Valid syntax but unparseable |
| `InvalidFilterError` | 422 | `INVALID_FILTER_ERROR` | year_from > year_to, unknown doc_type |
| `CaseNotFoundError` | 404 | `CASE_NOT_FOUND` | GET /case/{id} with unknown id |
| `QueryExecutorError` | 500 | `QUERY_EXECUTOR_ERROR` | Unhandled AST node (code bug) |
| `DatabaseQueryError` | 503 | `DATABASE_QUERY_ERROR` | psycopg2 error during execution |
| `DatabaseConnectionError` | 503 | `DATABASE_CONNECTION_ERROR` | DB unreachable |
| `SnippetFetchError` | — | Internal | Snippet fails silently, result returned without snippet |

---

## 13. Mounting in FastAPI

In your main `app.py` or `main.py`:

```python
from fastapi import FastAPI
from boolean.router import router as boolean_router

app = FastAPI(title="Madhav.AI Legal API")

# Mount boolean search
app.include_router(boolean_router)

# All your existing routes...
```

This registers:
- `POST /boolean/search`
- `POST /boolean/validate`
- `POST /boolean/parse`
- `GET  /boolean/case/{case_id}`
- `GET  /boolean/health`

### Exception handler (recommended addition to main app)

```python
from fastapi import Request
from fastapi.responses import JSONResponse
from boolean.exceptions import BooleanSearchError

@app.exception_handler(BooleanSearchError)
async def boolean_error_handler(request: Request, exc: BooleanSearchError):
    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_dict(),
    )
```

---

## 14. Running Tests

```bash
# From project root
pytest backend/boolean/tests/test_boolean.py -v

# With coverage
pytest backend/boolean/tests/test_boolean.py -v --cov=boolean --cov-report=term-missing

# Run specific test class
pytest backend/boolean/tests/test_boolean.py::TestValidatorInvalid -v

# Run specific test
pytest backend/boolean/tests/test_boolean.py::TestParserPrecedence::test_and_before_or -v
```

**No database connection required.** All tests are unit tests operating on pure Python logic. The executor tests inspect generated SQL strings but do not execute them.

Test coverage:
- `validator.py` — 22 valid cases, 18 invalid cases
- `parser.py` — 22 node type tests, 5 precedence tests, 4 ast_to_dict tests
- `filters.py` — court aliases, act aliases, year parsing, cross-field validation, describe
- `highlighter.py` — ts_headline parsing, fallback snippets, build_case_snippet, serialisation
- `ranker.py` — 10 factor tests, 4 composite score tests, 6 rerank tests
- `executor.py` — 14 SQL structure tests, 5 extract_search_terms tests
- Integration — 8 end-to-end pipeline tests

---

## 15. Performance Notes

### Expected query times (on indexed DB with ~100k cases, ~10M paragraphs)

| Query type | Expected time |
|-----------|--------------|
| Simple term (tsvector GIN) | 10–50ms |
| Phrase (phraseto_tsquery) | 15–60ms |
| AND of two terms (INTERSECT) | 20–80ms |
| Proximity W/5 (regex on filtered set) | 50–200ms |
| Complex query with 4+ operators | 100–400ms |
| With filters (additional WHERE clauses) | +10–30ms |
| Snippet generation (per case, top 3 paras) | +10–20ms per case |

### Bottlenecks to watch

**Proximity queries on large result sets.** If the INTERSECT returns 10,000 case_ids and then runs regex on all their paragraphs, it will be slow. Consider adding a `LIMIT` to the inner INTERSECT before applying the regex, or adding a `quality_score > threshold` filter to reduce the paragraph scan.

**NOT queries.** `NOT IN (SELECT ...)` with a large subquery is slow. For production scale, consider rewriting as `EXCEPT` in the executor.

**Snippet generation.** `ts_headline` is called per case per result page. With `page_size=25` this is 25 extra queries. Consider batching with `WHERE case_id = ANY(%s)`.

**psycopg2 single connection.** Your current `db.py` uses a single persistent connection. For concurrent users, upgrade to `psycopg2.pool.ThreadedConnectionPool` or `asyncpg` + `asyncio`.

---

## 16. Extending the Module

### Adding a new field qualifier

1. Add the alias to `_FIELD_ALIASES` in `parser.py`
2. Add a handler method `_field_<name>` in `BooleanExecutor` in `executor.py`
3. Register it in the `dispatch` dict in `BooleanExecutor._field()`
4. Add the field name to `known_fields` in `validator.py`
5. Add tests in `test_boolean.py`

### Adding a new proximity operator

1. Add to the `_PROX_RE` regex in `parser.py` if it has a new pattern
2. Add a new `elif op == "NEWOP":` branch in `BooleanExecutor._proximity()` in `executor.py`
3. Update validator if needed

### Adding a new court alias

Add to `COURT_ALIASES` dict in `filters.py`. No other changes needed.

### Adding a new act alias

Add to `ACT_ALIASES` dict in `filters.py`. No other changes needed.

### Switching to asyncpg

Replace psycopg2 calls in `router.py` with `await conn.fetch(sql, *params)`. The SQL itself does not change. Update `executor.py` to use `$1`, `$2` placeholders instead of `%s`.
