# Days 4–6 Integration Notes

## What Was Built

One new file: `Backend/retrieval/arguments_router.py`

Mount it in `main.py`:
```python
from Backend.retrieval.arguments_router import router as arguments_router
app.include_router(arguments_router)
```

That's it. No changes to any existing file.

---

## What Already Existed (No Work Needed)

| Sprint Task | Status | Existing Endpoint |
|-------------|--------|-------------------|
| 2A: Argument Extractor | ✅ Existed | `POST /api/study/arguments` |
| 2B: 30s summary (study) | ✅ Existed | `POST /api/study/quick-brief` (Day 3) |
| 2D: Multi-case synthesis | ✅ Existed | `POST /api/study/synthesize` |

The new file adds the **upgraded versions** of these:
- Argument extractor with DB caching + legal test extraction
- Quick summary with DB-first fallback + caching
- Issue spotter with **real DB case lookups** (not LLM-invented names)
- Multi-case brief with structured JSON output (not free-form prose)

---

## New Endpoints Summary

### `GET /api/cases/{case_id}/arguments`
Returns cached arguments instantly. Returns 404 if not yet generated.
Frontend should try this first before calling the POST.

### `POST /api/cases/{case_id}/arguments`
Generates arguments via Ollama and caches in `case_arguments_cache` table.
First call: ~60-90s. Every subsequent call: instant (served from cache).
Optional query param: `?force_regenerate=true` to regenerate.

Response shape:
```json
{
  "arguments": {
    "petitioner_name": "Ram Prasad Sharma",
    "respondent_name": "State of Maharashtra",
    "petitioner_arguments": [
      { "point": "Arrest without valid warrant", "detail": "...", "para_ref": 12, "strength": "strong" }
    ],
    "respondent_arguments": [...],
    "court_finding": "The court held...",
    "winning_side": "petitioner",
    "key_legal_test": "Three-part proportionality test",
    "test_parts": ["Legality — ...", "Necessity — ...", "Proportionality — ..."]
  },
  "cached": false,
  "done": true
}
```

---

### `POST /api/cases/{case_id}/quick-summary`
One-liner + 30-second summary. Cached in `case_brief_cache`.

If `ratio_decidendi` exists in DB → fast path (DB-only fallback ready immediately, LLM enhances in background).
If no ratio in DB → full LLM call needed.

Response shape:
```json
{
  "one_liner": "Held: Right to privacy is a fundamental right under Article 21.",
  "summary_30s": "The SC held that privacy is intrinsic to personal liberty...",
  "cached": false,
  "done": true
}
```

---

### `POST /api/legal/issue-spot`
Facts → legal issues → real cases from your DB.

Request:
```json
{
  "facts": "Client arrested without warrant, held 5 days without magistrate production...",
  "context": "Criminal matter, Sessions Court",
  "max_issues": 4
}
```

Response shape:
```json
{
  "issues": {
    "issues": [
      {
        "issue": "Violation of Article 22(2) — detention beyond 24 hours",
        "explanation": "CrPC Section 57 mandates production within 24 hours",
        "applicable_acts": ["CrPC Section 57", "Article 22(2) Constitution"],
        "search_query": "custodial detention 24 hours Article 22",
        "priority": "high",
        "relief_available": "Writ of habeas corpus",
        "relevant_cases": [
          {
            "case_id": "uuid-here",
            "case_name": "D.K. Basu v. State of West Bengal",
            "citation": "AIR 1997 SC 610",
            "court": "Supreme Court",
            "year": 1997,
            "outcome": "Allowed"
          }
        ]
      }
    ],
    "immediate_reliefs": ["Habeas corpus petition", "Bail application"],
    "limitation_concern": null,
    "matter_type": "criminal"
  },
  "done": true
}
```

---

### `POST /api/brief/multi`
Structured multi-case brief in 3 modes.

Request:
```json
{
  "case_ids": ["id1", "id2", "id3"],
  "topic": "Article 21 — right to privacy",
  "mode": "brief"
}
```

**Mode `brief`** — per-case structured data + synthesis:
```json
{
  "topic": "Article 21 — right to privacy",
  "mode": "brief",
  "cases": [
    {
      "case_name": "...",
      "citation": "...",
      "year": "2017",
      "court": "Supreme Court",
      "key_facts": "...",
      "holding_on_topic": "...",
      "ratio": "...",
      "precedent_value": "high"
    }
  ],
  "synthesis": "Reading these cases together...",
  "key_principle": "Privacy is a fundamental right under Article 21.",
  "conflicts": null
}
```

**Mode `evolution`** — timeline of how the law changed:
```json
{
  "topic": "...",
  "mode": "evolution",
  "starting_position": "Before 2017, privacy was not recognised as a fundamental right...",
  "timeline": [
    { "case_name": "...", "year": "1954", "development": "...", "shift_type": "established" }
  ],
  "current_position": "...",
  "key_turning_point": "..."
}
```

**Mode `conflict`** — where cases agree vs disagree:
```json
{
  "topic": "...",
  "mode": "conflict",
  "consensus_points": ["..."],
  "conflict_points": [
    { "point": "...", "case_a": "...", "case_b": "...", "resolution": "Case A prevails as later SC bench" }
  ],
  "recommended_approach": "..."
}
```

---

## DB Tables Created Automatically

Two new tables, created by `ensure_cache_tables()` on first request:

```sql
-- Argument cache
case_arguments_cache (
    case_id         TEXT PRIMARY KEY,
    arguments_json  JSONB NOT NULL,
    generated_at    TIMESTAMPTZ DEFAULT NOW(),
    model_used      TEXT
)

-- Brief cache (used for quick-summary AND multi-brief)
case_brief_cache (
    cache_key       TEXT PRIMARY KEY,
    brief_json      JSONB NOT NULL,
    generated_at    TIMESTAMPTZ DEFAULT NOW(),
    model_used      TEXT
)
```

Cache keys:
- Quick summary: `summary:{case_id}`
- Multi-brief: `multi:{sorted_ids}:{topic_hash}:{mode}`

---

## For the Frontend Person

### Arguments Tab (Case Viewer)

1. On case page load → `GET /api/cases/{id}/arguments`
   - If 200 → render immediately (cached)
   - If 404 → show "Analysing arguments..." spinner, then `POST /api/cases/{id}/arguments`

2. Layout — two-column:
```
┌─────────────────────┬─────────────────────┐
│ 📋 PETITIONER       │ ⚖️ RESPONDENT        │
│ Ram Prasad Sharma   │ State of Maharashtra │
├─────────────────────┼─────────────────────┤
│ • Arg 1 [strong]    │ • Arg 1 [moderate]  │
│   Para 12 ↗        │   Para 18 ↗         │
│ • Arg 2 [moderate]  │ • Arg 2 [strong]    │
└─────────────────────┴─────────────────────┘
    COURT FINDING: The court held...
    ✅ Petitioner succeeded
```

3. If `key_legal_test` is not null → show numbered test steps below the arguments.

### Quick Summary (Case Header)

Call `POST /api/cases/{id}/quick-summary` on case page load (non-blocking).

Show `one_liner` in a yellow highlighted box at the very top of the case viewer (above tabs):
```
┌─────────────────────────────────────────────────────────┐
│ 💡 Held: Right to privacy is a fundamental right        │
│         under Article 21.                               │
└─────────────────────────────────────────────────────────┘
```

Show `summary_30s` at the top of the Brief tab, before the FIHR sections.

### Issue Spotter

New entry point — add to the main search area as a tab or secondary button:
`🔍 Identify Issues from Facts`

- Textarea: "Describe your client's situation..."
- Optional: jurisdiction, type of matter
- Submit → show cards per issue
- Each issue card has: issue title, explanation, acts as tags, "Search cases →" button (uses `search_query` field), and the `relevant_cases` list with links

### Multi-Case Brief

Add a "Compare Cases" button when user has 2+ cases open or bookmarked.
- Let user pick 2-5 cases
- Topic input: "What legal question connects these?"
- Mode selector: Brief / Evolution / Conflict
- Renders as a comparison table (brief mode) or timeline (evolution mode)
