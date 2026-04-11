"""
madhav.ai — Search Enhancements
Plug into your existing Elasticsearch + PostgreSQL search pipeline.

Provides:
  - Synonym expansion (legal domain)
  - Spell correction (Elasticsearch did-you-mean)
  - Boolean / phrase / proximity query parser
  - Wildcard + regex support
  - Advanced filter builders (outcome, party, precedent status, case type)
"""

import re
from typing import Optional
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────
# 1. LEGAL SYNONYM MAP
#    Expand query terms before sending to Elasticsearch.
#    Add more as you discover gaps from student feedback.
# ─────────────────────────────────────────────────────────

LEGAL_SYNONYMS: dict[str, list[str]] = {
    # Bail
    "bail":              ["anticipatory bail", "regular bail", "interim bail", "bail application"],
    "anticipatory bail": ["bail", "pre-arrest bail", "section 438 crpc"],
    "regular bail":      ["bail", "section 439 crpc"],

    # Common legal terms
    "cheque bounce":     ["dishonour of cheque", "section 138 ni act", "negotiable instruments"],
    "cheating":          ["fraud", "section 420 ipc", "criminal breach of trust"],
    "murder":            ["section 302 ipc", "culpable homicide", "homicide"],
    "rape":              ["section 376 ipc", "sexual assault", "pocso"],
    "defamation":        ["section 499 ipc", "section 500 ipc", "libel", "slander"],
    "contempt":          ["contempt of court", "section 2 contempt of courts act"],
    "injunction":        ["temporary injunction", "permanent injunction", "stay order", "order 39 cpc"],
    "writ":              ["writ petition", "habeas corpus", "mandamus", "certiorari", "quo warranto"],
    "habeas corpus":     ["writ", "illegal detention", "article 226", "article 32"],
    "PIL":               ["public interest litigation", "article 226", "article 32"],
    "FIR":               ["first information report", "section 154 crpc"],
    "chargesheet":       ["charge sheet", "section 173 crpc", "police report"],
    "custody":           ["police custody", "judicial custody", "remand"],
    "acquittal":         ["acquitted", "not guilty", "discharge", "section 232 crpc"],
    "conviction":        ["convicted", "guilty", "sentenced"],
    "appeal":            ["section 374 crpc", "section 96 cpc", "letters patent appeal"],
    "revision":          ["section 397 crpc", "section 115 cpc", "revisional jurisdiction"],
    "specific performance": ["section 10 specific relief act", "specific relief"],
    "divorce":           ["section 13 hindu marriage act", "dissolution of marriage", "matrimonial"],
    "maintenance":       ["section 125 crpc", "alimony", "section 24 hma"],
    "property dispute":  ["title suit", "declaratory suit", "partition", "possession"],
    "contract":          ["breach of contract", "indian contract act", "section 73 ica"],
    "consumer":          ["consumer forum", "consumer protection act", "district forum", "NCDRC"],
    "trademark":         ["section 29 trade marks act", "infringement", "passing off"],
    "copyright":         ["section 51 copyright act", "infringement", "literary work"],
    "arbitration":       ["section 34 arbitration act", "award", "section 11 arbitration"],
    "company":           ["companies act", "NCLT", "insolvency", "IBC"],
    "tax":               ["income tax", "GST", "section 263 income tax act", "demand notice"],
    "environment":       ["NGT", "national green tribunal", "pollution", "environmental clearance"],
    "land acquisition":  ["section 4 land acquisition act", "compensation", "LARR act"],
}

def expand_synonyms(query: str) -> list[str]:
    """Return list of synonym-expanded terms to add to the search."""
    query_lower = query.lower()
    extra_terms = []
    for key, synonyms in LEGAL_SYNONYMS.items():
        if key in query_lower:
            extra_terms.extend(synonyms)
    return list(set(extra_terms))


# ─────────────────────────────────────────────────────────
# 2. BOOLEAN / PHRASE / PROXIMITY QUERY PARSER
#    Parses user input like:
#      fraud AND contract
#      "breach of contract"
#      fraud NEAR/5 cheque
#      fraud*
#      NOT acquittal
# ─────────────────────────────────────────────────────────

class ParsedQuery:
    def __init__(self):
        self.must: list[dict]     = []   # AND clauses
        self.must_not: list[dict] = []   # NOT clauses
        self.should: list[dict]   = []   # OR clauses
        self.phrases: list[str]   = []   # exact phrase matches
        self.wildcards: list[str] = []   # wildcard terms
        self.proximity: list[dict]= []   # NEAR queries
        self.raw_terms: list[str] = []   # plain terms

    def to_es_query(self, fields: list[str] = None) -> dict:
        """Convert parsed query to Elasticsearch bool query."""
        fields = fields or ["full_text", "case_name^2", "headnotes^1.5", "acts_sections"]
        bool_q: dict = {"bool": {}}

        all_must = list(self.must)
        all_must_not = list(self.must_not)
        all_should = list(self.should)

        # Phrases → match_phrase
        for phrase in self.phrases:
            all_must.append({"multi_match": {"query": phrase, "fields": fields, "type": "phrase"}})

        # Wildcards
        for wc in self.wildcards:
            all_should.append({"wildcard": {"full_text": {"value": wc.lower()}}})

        # Proximity (NEAR) → span_near approximation via match_phrase with slop
        for prox in self.proximity:
            slop = prox.get("slop", 10)
            combined = f"{prox['left']} {prox['right']}"
            all_must.append({"match_phrase": {"full_text": {"query": combined, "slop": slop}}})

        # Plain terms
        if self.raw_terms:
            combined_raw = " ".join(self.raw_terms)
            all_should.append({"multi_match": {
                "query": combined_raw,
                "fields": fields,
                "type": "best_fields",
                "fuzziness": "AUTO",
                "minimum_should_match": "60%",
            }})

        if all_must:     bool_q["bool"]["must"]     = all_must
        if all_must_not: bool_q["bool"]["must_not"] = all_must_not
        if all_should:   bool_q["bool"]["should"]   = all_should

        if not bool_q["bool"]:
            return {"match_all": {}}
        return bool_q


def parse_boolean_query(query: str) -> ParsedQuery:
    """
    Parse a user search query into structured clauses.
    Supports: AND, OR, NOT, "phrases", wildcards*, NEAR/N
    """
    pq = ParsedQuery()
    remaining = query.strip()

    # 1. Extract quoted phrases
    phrases = re.findall(r'"([^"]+)"', remaining)
    pq.phrases = phrases
    remaining = re.sub(r'"[^"]+"', "", remaining).strip()

    # 2. Extract NEAR/N proximity: term1 NEAR/5 term2
    near_pattern = re.compile(r'(\w+)\s+NEAR(?:/(\d+))?\s+(\w+)', re.IGNORECASE)
    for m in near_pattern.finditer(remaining):
        slop = int(m.group(2)) if m.group(2) else 10
        pq.proximity.append({"left": m.group(1), "right": m.group(3), "slop": slop})
    remaining = near_pattern.sub("", remaining).strip()

    # 3. Split on AND / OR / NOT
    # Tokenize keeping operators
    tokens = re.split(r'\s+(AND|OR|NOT)\s+', remaining, flags=re.IGNORECASE)
    
    i = 0
    current_op = "OR"   # default operator between plain terms

    while i < len(tokens):
        token = tokens[i].strip()
        if not token:
            i += 1
            continue

        upper = token.upper()
        if upper == "AND":
            current_op = "AND"
            i += 1
            continue
        elif upper == "OR":
            current_op = "OR"
            i += 1
            continue
        elif upper == "NOT":
            # next token is the term to exclude
            i += 1
            if i < len(tokens):
                term = tokens[i].strip()
                if term:
                    pq.must_not.append({"multi_match": {
                        "query": term,
                        "fields": ["full_text", "case_name"],
                    }})
            i += 1
            continue

        # Regular term or wildcard
        if "*" in token or "?" in token:
            pq.wildcards.append(token)
        elif current_op == "AND":
            pq.must.append({"multi_match": {
                "query": token,
                "fields": ["full_text", "case_name^2", "acts_sections"],
                "fuzziness": "AUTO",
            }})
        else:
            pq.raw_terms.append(token)

        i += 1

    return pq


# ─────────────────────────────────────────────────────────
# 3. ELASTICSEARCH FILTER BUILDERS
#    Build ES filter clauses for each advanced filter.
# ─────────────────────────────────────────────────────────

def build_es_filters(
    court: Optional[str]            = None,
    judge: Optional[str]            = None,
    year_from: Optional[int]        = None,
    year_to: Optional[int]          = None,
    bench_strength: Optional[int]   = None,
    act_section: Optional[str]      = None,
    party_name: Optional[str]       = None,
    case_type: Optional[str]        = None,
    outcome: Optional[str]          = None,
    precedent_status: Optional[str] = None,
) -> list[dict]:
    """
    Build Elasticsearch filter clauses from advanced filter inputs.
    Add these to your bool query's 'filter' array.
    """
    filters = []

    if court:
        filters.append({"match": {"court": {"query": court, "fuzziness": "AUTO"}}})

    if judge:
        filters.append({"match": {"judges": {"query": judge, "fuzziness": "AUTO"}}})

    if year_from or year_to:
        date_range = {}
        if year_from: date_range["gte"] = f"{year_from}-01-01"
        if year_to:   date_range["lte"] = f"{year_to}-12-31"
        filters.append({"range": {"date_of_judgment": date_range}})

    if bench_strength:
        filters.append({"term": {"bench_strength": bench_strength}})

    if act_section:
        filters.append({"match": {"acts_sections": {"query": act_section, "fuzziness": "AUTO"}}})

    if party_name:
        filters.append({"multi_match": {
            "query": party_name,
            "fields": ["petitioner", "respondent", "party_names"],
            "fuzziness": "AUTO",
        }})

    if case_type:
        # civil / criminal / writ / tax / company / consumer / arbitration
        filters.append({"term": {"case_type": case_type.lower()}})

    if outcome:
        # allowed / dismissed / partly_allowed / acquitted / convicted
        filters.append({"term": {"outcome": outcome.lower()}})

    if precedent_status:
        # good_law / overruled / distinguished / followed / doubted
        filters.append({"term": {"precedent_status": precedent_status.lower()}})

    return filters


# ─────────────────────────────────────────────────────────
# 4. SPELL CORRECTION via Elasticsearch suggest API
#    Use this to power "Did you mean?" suggestions.
# ─────────────────────────────────────────────────────────

def build_suggest_query(query: str) -> dict:
    """
    Build an ES suggest request body.
    Call /your-index/_search with this body to get spell suggestions.
    """
    return {
        "suggest": {
            "spell_check": {
                "text": query,
                "term": {
                    "field": "full_text",
                    "suggest_mode": "missing",   # only suggest if no results
                    "min_word_length": 3,
                    "prefix_length": 2,
                    "max_edits": 2,
                }
            },
            "phrase_check": {
                "text": query,
                "phrase": {
                    "field": "full_text.shingle",  # needs shingle analyzer on this field
                    "gram_size": 3,
                    "direct_generator": [{
                        "field": "full_text",
                        "suggest_mode": "always",
                    }],
                    "highlight": {
                        "pre_tag": "<em>",
                        "post_tag": "</em>",
                    }
                }
            }
        },
        "size": 0   # we only want suggestions, not results
    }


def extract_suggestion(es_suggest_response: dict) -> Optional[str]:
    """
    Extract the best spelling suggestion from ES suggest response.
    Returns corrected query string or None if no correction needed.
    """
    try:
        suggestions = es_suggest_response.get("suggest", {})
        spell = suggestions.get("spell_check", [])
        corrected_tokens = []
        changed = False

        for item in spell:
            original = item.get("text", "")
            options  = item.get("options", [])
            if options and options[0].get("score", 0) > 0.7:
                corrected_tokens.append(options[0]["text"])
                changed = True
            else:
                corrected_tokens.append(original)

        if changed:
            return " ".join(corrected_tokens)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────
# 5. AUTOCOMPLETE — build prefix query for case names / acts
# ─────────────────────────────────────────────────────────

def build_autocomplete_query(prefix: str, size: int = 8) -> dict:
    """
    ES query for autocomplete suggestions.
    Requires a 'completion' field mapped on case_name_suggest,
    OR falls back to prefix match on case_name and acts_sections.
    """
    return {
        "size": size,
        "_source": ["case_name", "citation", "court", "year", "case_id"],
        "query": {
            "bool": {
                "should": [
                    # prefix on case name (fastest)
                    {"prefix": {"case_name.keyword": {"value": prefix, "boost": 3}}},
                    # fuzzy prefix on case name text
                    {"match_phrase_prefix": {"case_name": {"query": prefix, "boost": 2}}},
                    # match on acts/sections
                    {"match_phrase_prefix": {"acts_sections": {"query": prefix}}},
                    # match on citation
                    {"prefix": {"citation": {"value": prefix}}},
                ],
                "minimum_should_match": 1,
            }
        },
        "highlight": {
            "fields": {"case_name": {}, "citation": {}},
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
        }
    }


# ─────────────────────────────────────────────────────────
# 6. MASTER QUERY BUILDER
#    Combines everything: parse → synonyms → filters → ES body
# ─────────────────────────────────────────────────────────

def build_full_es_query(
    raw_query: str,
    filters: dict = None,
    from_: int = 0,
    size: int = 20,
    fields: list[str] = None,
) -> dict:
    """
    Build a complete Elasticsearch query body from a raw user query + filters.

    Usage in your search endpoint:
        body = build_full_es_query(
            raw_query = "cheque bounce fraud",
            filters = {
                "court": "Supreme Court",
                "year_from": 2010,
                "outcome": "allowed",
            }
        )
        results = es_client.search(index="cases", body=body)
    """
    filters = filters or {}
    fields  = fields  or ["full_text", "case_name^3", "headnotes^2", "acts_sections^1.5", "citation^2"]

    # Parse boolean / phrase / proximity
    pq = parse_boolean_query(raw_query)

    # Expand synonyms and add to should
    extra_terms = expand_synonyms(raw_query)
    if extra_terms:
        pq.should.append({"multi_match": {
            "query": " ".join(extra_terms),
            "fields": fields,
            "boost": 0.5,
        }})

    # Build core query
    core_query = pq.to_es_query(fields)

    # Wrap with filters
    es_body = {
        "from": from_,
        "size": size,
        "query": {
            "bool": {
                "must": [core_query],
                "filter": build_es_filters(**filters),
            }
        },
        "highlight": {
            "fields": {
                "full_text":    {"fragment_size": 200, "number_of_fragments": 3},
                "case_name":    {"number_of_fragments": 0},
                "headnotes":    {"fragment_size": 150, "number_of_fragments": 2},
            },
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
        },
        "aggs": {
            "courts":   {"terms": {"field": "court.keyword",    "size": 15}},
            "years":    {"date_histogram": {"field": "date_of_judgment", "calendar_interval": "year"}},
            "outcomes": {"terms": {"field": "outcome.keyword",  "size": 10}},
            "case_types":{"terms":{"field": "case_type.keyword","size": 10}},
        },
        "_source": [
            "case_id", "case_name", "citation", "court", "judges",
            "date_of_judgment", "year", "outcome", "case_type",
            "acts_sections", "petitioner", "respondent",
            "bench_strength", "precedent_status", "headnotes",
        ],
    }

    return es_body
