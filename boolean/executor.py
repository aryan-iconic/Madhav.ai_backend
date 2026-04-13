"""
boolean/executor.py
===================
Walks the parsed AST and builds parameterised PostgreSQL SQL queries.

Architecture:
  - Every node type maps to a SQL fragment (CTE or subquery)
  - Uses your EXISTING GIN tsvector indexes on:
      legal_paragraphs.text           → idx_legal_paragraphs_text / idx_paragraphs_text_search
      legal_cases.case_name           → idx_cases_case_name
      legal_cases.petitioner          → idx_cases_petitioner
      legal_cases.respondent          → idx_cases_respondent
  - Proximity (W/n) uses PostgreSQL's  ts_headline + regexp approach
    since native tsvector doesn't support word proximity directly.
    We use paragraph-level windowed matching via pg regex.
  - All queries are fully parameterised (no f-string SQL injection risk)
  - Returns case-level results ranked by relevance

Query strategy per node:
  TermNode / PhraseNode / WildcardNode
      → plainto_tsquery / phraseto_tsquery / to_tsquery on legal_paragraphs.text
  AtleastNode
      → count occurrences in paragraph text, filter by count >= n
  AndNode / OrNode / NotNode
      → INTERSECT / UNION / EXCEPT on paragraph-level case_ids
  ProximityNode (W/n, NEAR/n, PRE/n)
      → regex-based word-window check on paragraph text
  FieldNode
      → routes to the correct column/table:
          court         → legal_cases.court
          judges        → legal_paragraphs.judges_mentioned (array)
          act           → case_acts.act_name
          section       → case_acts.section
          year          → legal_cases.year
          case_name     → legal_cases.case_name  (tsvector)
          petitioner    → legal_cases.petitioner (tsvector)
          respondent    → legal_cases.respondent (tsvector)
          constitutional_articles → legal_cases.constitutional_articles (array)
          subject_tags  → legal_cases.subject_tags (array)
          citation      → legal_cases.appeal_no / case_id
"""

from __future__ import annotations
import re
from typing import Any

from .parser import (
    TermNode, PhraseNode, WildcardNode, AtleastNode,
    FieldNode, NotNode, AndNode, OrNode, ProximityNode,
)


# ─────────────────────────────────────────────────────────────────────────────
# Result container
# ─────────────────────────────────────────────────────────────────────────────

class SQLFragment:
    """
    Holds a SQL SELECT that returns a set of case_ids matching the node.
    All fragments return exactly one column: case_id (text).
    Params is a list of positional values ($1, $2, ...) — we use %s for psycopg2.
    """
    def __init__(self, sql: str, params: list):
        self.sql    = sql       # complete SELECT ... FROM ... WHERE ...
        self.params = params    # psycopg2 positional params list


# ─────────────────────────────────────────────────────────────────────────────
# Executor class
# ─────────────────────────────────────────────────────────────────────────────

class BooleanExecutor:
    """
    Walk an AST and produce a final SQL query + params.

    Usage:
        executor = BooleanExecutor(filters=filters)
        sql, params = executor.build(ast_root)
        # Then wrap with result query (see build_result_query)
    """

    def __init__(self, filters: dict | None = None):
        """
        filters: optional dict from the request's filter fields:
          {
            "court":     "Supreme Court of India",
            "year_from": 2010,
            "year_to":   2023,
            "act":       "Indian Penal Code",
            "section":   "302",
            "judge":     "D.Y. Chandrachud",
            "doc_type":  "judgment",
          }
        """
        self._filters = filters or {}
        self._params:  list = []

    # ── Param management ──────────────────────────────────────────────────

    def _add_param(self, value: Any) -> str:
        """Add a param and return its placeholder string."""
        self._params.append(value)
        return "%s"

    # ── Main dispatcher ───────────────────────────────────────────────────

    def build(self, node: Any) -> tuple[str, list]:
        """
        Entry point. Returns (final_sql, params).
        The SQL selects distinct case_ids matching the boolean query.
        """
        self._params = []
        fragment     = self._dispatch(node)
        return fragment.sql, self._params

    def _dispatch(self, node: Any) -> SQLFragment:
        if isinstance(node, TermNode):
            return self._term(node)
        if isinstance(node, PhraseNode):
            return self._phrase(node)
        if isinstance(node, WildcardNode):
            return self._wildcard(node)
        if isinstance(node, AtleastNode):
            return self._atleast(node)
        if isinstance(node, FieldNode):
            return self._field(node)
        if isinstance(node, NotNode):
            return self._not(node)
        if isinstance(node, AndNode):
            return self._and(node)
        if isinstance(node, OrNode):
            return self._or(node)
        if isinstance(node, ProximityNode):
            return self._proximity(node)
        raise ExecutorError(f"Unknown AST node type: {type(node).__name__}")

    # ── Leaf nodes ────────────────────────────────────────────────────────

    def _term(self, node: TermNode) -> SQLFragment:
        """
        Single keyword → plainto_tsquery on legal_paragraphs.text.
        Uses the existing GIN tsvector index.
        """
        p = self._add_param(node.value)
        sql = f"""
            SELECT DISTINCT p.case_id
            FROM legal_paragraphs p
            WHERE to_tsvector('english', p.text) @@ plainto_tsquery('english', {p})
        """
        return SQLFragment(sql, self._params[:])

    def _phrase(self, node: PhraseNode) -> SQLFragment:
        """
        Exact phrase → phraseto_tsquery.
        PostgreSQL's phraseto_tsquery handles adjacent-word proximity.
        """
        p = self._add_param(node.value)
        sql = f"""
            SELECT DISTINCT p.case_id
            FROM legal_paragraphs p
            WHERE to_tsvector('english', p.text) @@ phraseto_tsquery('english', {p})
        """
        return SQLFragment(sql, self._params[:])

    def _wildcard(self, node: WildcardNode) -> SQLFragment:
        """
        Wildcard → to_tsquery with :* suffix (prefix matching).
        * and ! → prefix match (root:*)
        ? → LIKE with single-char substitution
        """
        if node.wc in ("*", "!"):
            # to_tsquery prefix match:  constitu:*
            tsq = node.root + ":*"
            p   = self._add_param(tsq)
            sql = f"""
                SELECT DISTINCT p.case_id
                FROM legal_paragraphs p
                WHERE to_tsvector('english', p.text) @@ to_tsquery('english', {p})
            """
        else:
            # ? → single-char LIKE pattern
            like_pattern = node.root.replace("?", "_") + "%"
            p = self._add_param(like_pattern)
            sql = f"""
                SELECT DISTINCT p.case_id
                FROM legal_paragraphs p
                WHERE p.text ILIKE {p}
            """
        return SQLFragment(sql, self._params[:])

    def _atleast(self, node: AtleastNode) -> SQLFragment:
        """
        atleast<n>(<term>) → count occurrences of term in paragraph text.
        Uses regexp_count (PostgreSQL 15+) or length subtraction trick for older PG.
        """
        pattern = r'\m' + re.escape(node.term) + r'\M'   # word-boundary regex
        p_term  = self._add_param(pattern)
        p_n     = self._add_param(node.n)
        sql = f"""
            SELECT DISTINCT p.case_id
            FROM legal_paragraphs p
            WHERE (
                LENGTH(p.text) - LENGTH(REGEXP_REPLACE(
                    LOWER(p.text),
                    {p_term},
                    '',
                    'g'
                ))
            ) / GREATEST(LENGTH({self._add_param(node.term)}), 1) >= {p_n}
        """
        return SQLFragment(sql, self._params[:])

    # ── Compound nodes ────────────────────────────────────────────────────

    def _not(self, node: NotNode) -> SQLFragment:
        """NOT → all case_ids EXCEPT those in the operand's result set."""
        inner = self._dispatch(node.operand)
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE lc.case_id NOT IN (
                {inner.sql}
            )
        """
        return SQLFragment(sql, self._params[:])

    def _and(self, node: AndNode) -> SQLFragment:
        """AND → INTERSECT of left and right result sets."""
        left  = self._dispatch(node.left)
        right = self._dispatch(node.right)
        sql = f"""
            ({left.sql})
            INTERSECT
            ({right.sql})
        """
        return SQLFragment(sql, self._params[:])

    def _or(self, node: OrNode) -> SQLFragment:
        """OR → UNION of left and right result sets."""
        left  = self._dispatch(node.left)
        right = self._dispatch(node.right)
        sql = f"""
            ({left.sql})
            UNION
            ({right.sql})
        """
        return SQLFragment(sql, self._params[:])

    def _proximity(self, node: ProximityNode) -> SQLFragment:
        """
        Proximity search at paragraph level.

        Strategy:
          W/n   — left term precedes right term within n words (ordered)
          NEAR/n — either term within n words of the other (unordered)
          PRE/n  — same as W/n (left must precede right)
          /S     — both terms appear in same sentence (approx: same paragraph, ≤ 30 words apart)
          /P     — both terms appear in same paragraph (always true if in same paragraph row)

        We use PostgreSQL regex on the raw paragraph text.
        This is intentionally done at paragraph level so the existing
        paragraph structure (legal_paragraphs) acts as a natural sentence/clause boundary.
        """
        # Get the text representations of left and right
        left_pattern  = _node_to_text_pattern(node.left)
        right_pattern = _node_to_text_pattern(node.right)

        # Also get case_ids from each side via normal dispatch
        left_frag  = self._dispatch(node.left)
        right_frag = self._dispatch(node.right)

        op = node.op_type.upper()

        if op in ("W", "PRE"):
            # Ordered: left appears before right within distance n words
            n = node.distance or 5
            # Build a regex: left_word followed by up to n words then right_word
            word_gap = r"(?:\s+\S+){0," + str(n - 1) + r"}\s+"
            pattern  = left_pattern + word_gap + right_pattern
            p_pat    = self._add_param(pattern)
            # Must appear in cases that match BOTH sides AND the proximity regex
            sql = f"""
                SELECT DISTINCT p.case_id
                FROM legal_paragraphs p
                WHERE p.case_id IN (
                    ({left_frag.sql})
                    INTERSECT
                    ({right_frag.sql})
                )
                AND p.text ~* {p_pat}
            """

        elif op == "NEAR":
            # Unordered: either direction within n words
            n = node.distance or 5
            word_gap  = r"(?:\s+\S+){0," + str(n - 1) + r"}\s+"
            pat_lr    = left_pattern + word_gap + right_pattern
            pat_rl    = right_pattern + word_gap + left_pattern
            p_lr      = self._add_param(pat_lr)
            p_rl      = self._add_param(pat_rl)
            sql = f"""
                SELECT DISTINCT p.case_id
                FROM legal_paragraphs p
                WHERE p.case_id IN (
                    ({left_frag.sql})
                    INTERSECT
                    ({right_frag.sql})
                )
                AND (p.text ~* {p_lr} OR p.text ~* {p_rl})
            """

        elif op == "S":
            # Same sentence — approximate: within 30 words in same paragraph
            word_gap  = r"(?:\s+\S+){0,29}\s+"
            pat_lr    = left_pattern + word_gap + right_pattern
            pat_rl    = right_pattern + word_gap + left_pattern
            p_lr      = self._add_param(pat_lr)
            p_rl      = self._add_param(pat_rl)
            sql = f"""
                SELECT DISTINCT p.case_id
                FROM legal_paragraphs p
                WHERE p.case_id IN (
                    ({left_frag.sql})
                    INTERSECT
                    ({right_frag.sql})
                )
                AND (p.text ~* {p_lr} OR p.text ~* {p_rl})
            """

        else:  # /P — same paragraph (both terms in same paragraph row)
            sql = f"""
                SELECT DISTINCT p.case_id
                FROM legal_paragraphs p
                WHERE p.case_id IN (
                    ({left_frag.sql})
                    INTERSECT
                    ({right_frag.sql})
                )
            """

        return SQLFragment(sql, self._params[:])

    # ── Field-qualified nodes ─────────────────────────────────────────────

    def _field(self, node: FieldNode) -> SQLFragment:
        """Route field:value to the correct column/table."""
        fn = node.field_name
        op = node.operand

        dispatch: dict[str, Any] = {
            "court":                    self._field_court,
            "judges":                   self._field_judges,
            "act":                      self._field_act,
            "section":                  self._field_section,
            "year":                     self._field_year,
            "case_name":                self._field_case_name,
            "petitioner":               self._field_petitioner,
            "respondent":               self._field_respondent,
            "constitutional_articles":  self._field_articles,
            "subject_tags":             self._field_keywords,
            "citation":                 self._field_citation,
        }

        handler = dispatch.get(fn)
        if not handler:
            # Unknown field — fall back to full-text on paragraphs
            return self._term(TermNode(_operand_value(op)))

        return handler(op)

    def _field_court(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE lc.court ILIKE {p}
        """
        return SQLFragment(sql, self._params[:])

    def _field_judges(self, op: Any) -> SQLFragment:
        """Search judges_mentioned array in paragraphs."""
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT p.case_id
            FROM legal_paragraphs p
            WHERE EXISTS (
                SELECT 1 FROM unnest(p.judges_mentioned) j
                WHERE j ILIKE {p}
            )
        """
        return SQLFragment(sql, self._params[:])

    def _field_act(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT ca.case_id
            FROM case_acts ca
            WHERE ca.act_name ILIKE {p}
        """
        return SQLFragment(sql, self._params[:])

    def _field_section(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT ca.case_id
            FROM case_acts ca
            WHERE ca.section ILIKE {p}
        """
        return SQLFragment(sql, self._params[:])

    def _field_year(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        try:
            year = int(val)
        except ValueError:
            raise ExecutorError(f"year: field value must be an integer, got {val!r}")
        p   = self._add_param(year)
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE lc.year = {p}
        """
        return SQLFragment(sql, self._params[:])

    def _field_case_name(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(val)
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE to_tsvector('english', COALESCE(lc.case_name, ''))
                  @@ plainto_tsquery('english', {p})
        """
        return SQLFragment(sql, self._params[:])

    def _field_petitioner(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(val)
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE to_tsvector('english', COALESCE(lc.petitioner, ''))
                  @@ plainto_tsquery('english', {p})
        """
        return SQLFragment(sql, self._params[:])

    def _field_respondent(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(val)
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE to_tsvector('english', COALESCE(lc.respondent, ''))
                  @@ plainto_tsquery('english', {p})
        """
        return SQLFragment(sql, self._params[:])

    def _field_articles(self, op: Any) -> SQLFragment:
        """constitutional_articles is a text[] array on legal_cases."""
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE EXISTS (
                SELECT 1 FROM unnest(lc.constitutional_articles) a
                WHERE a ILIKE {p}
            )
        """
        return SQLFragment(sql, self._params[:])

    def _field_keywords(self, op: Any) -> SQLFragment:
        """subject_tags is a text[] array on legal_cases."""
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE EXISTS (
                SELECT 1 FROM unnest(lc.subject_tags) t
                WHERE t ILIKE {p}
            )
        """
        return SQLFragment(sql, self._params[:])

    def _field_citation(self, op: Any) -> SQLFragment:
        val = _operand_value(op)
        p   = self._add_param(f"%{val}%")
        sql = f"""
            SELECT DISTINCT lc.case_id
            FROM legal_cases lc
            WHERE lc.appeal_no ILIKE {p}
               OR lc.case_id ILIKE {p}
        """
        return SQLFragment(sql, self._params[:])


# ─────────────────────────────────────────────────────────────────────────────
# Result query builder
# ─────────────────────────────────────────────────────────────────────────────

def build_result_query(
    boolean_sql:  str,
    boolean_params: list,
    filters:      dict,
    sort_by:      str = "relevance",
    page:         int = 1,
    page_size:    int = 25,
) -> tuple[str, list]:
    """
    Wraps the boolean core SQL with:
      - Metadata joins (legal_cases, case_citations count)
      - Filter conditions (court, year range, doc_type, judge, act)
      - Sorting (relevance = authority_score + citation_count, date, citations)
      - Pagination (LIMIT / OFFSET)

    Returns (final_sql, params).
    """
    params: list = list(boolean_params)

    def add(val):
        params.append(val)
        return "%s"

    # ── Filter clauses on legal_cases
    filter_clauses: list[str] = []

    if filters.get("court"):
        p = add(f"%{filters['court']}%")
        filter_clauses.append(f"lc.court ILIKE {p}")

    if filters.get("year_from"):
        p = add(int(filters["year_from"]))
        filter_clauses.append(f"lc.year >= {p}")

    if filters.get("year_to"):
        p = add(int(filters["year_to"]))
        filter_clauses.append(f"lc.year <= {p}")

    if filters.get("judge"):
        p = add(f"%{filters['judge']}%")
        # judge info is not a direct column — check paragraphs array
        filter_clauses.append(f"""
            EXISTS (
                SELECT 1 FROM legal_paragraphs jp
                WHERE jp.case_id = lc.case_id
                  AND EXISTS (
                      SELECT 1 FROM unnest(jp.judges_mentioned) jm
                      WHERE jm ILIKE {p}
                  )
            )
        """)

    if filters.get("act"):
        p = add(f"%{filters['act']}%")
        filter_clauses.append(f"""
            EXISTS (
                SELECT 1 FROM case_acts ca
                WHERE ca.case_id = lc.case_id
                  AND ca.act_name ILIKE {p}
            )
        """)

    if filters.get("section"):
        p = add(f"%{filters['section']}%")
        filter_clauses.append(f"""
            EXISTS (
                SELECT 1 FROM case_acts ca
                WHERE ca.case_id = lc.case_id
                  AND ca.section ILIKE {p}
            )
        """)

    if filters.get("doc_type"):
        p = add(filters["doc_type"].lower())
        filter_clauses.append(f"LOWER(lc.outcome) = {p}")

    where_clause = ""
    if filter_clauses:
        where_clause = "AND " + "\nAND ".join(filter_clauses)

    # ── Sort
    order_map = {
        "relevance":  "lc.authority_score DESC NULLS LAST, lc.citation_count DESC",
        "date_desc":  "lc.date_of_order DESC NULLS LAST",
        "date_asc":   "lc.date_of_order ASC  NULLS LAST",
        "citations":  "lc.citation_count DESC NULLS LAST",
    }
    order_sql = order_map.get(sort_by, order_map["relevance"])

    # ── Pagination
    offset = (page - 1) * page_size
    p_limit  = add(page_size)
    p_offset = add(offset)

    final_sql = f"""
        WITH boolean_matches AS (
            {boolean_sql}
        ),
        case_acts_agg AS (
            SELECT
                ca.case_id,
                STRING_AGG(DISTINCT ca.act_name, ' | ' ORDER BY ca.act_name) AS acts_list,
                STRING_AGG(DISTINCT ca.section,   ', '  ORDER BY ca.section)  AS sections_list
            FROM case_acts ca
            WHERE ca.case_id IN (SELECT case_id FROM boolean_matches)
            GROUP BY ca.case_id
        ),
        citation_counts AS (
            SELECT
                cc.cited_case_id                                       AS case_id,
                COUNT(*)                                               AS cited_by_count
            FROM case_citations cc
            WHERE cc.cited_case_id IN (SELECT case_id FROM boolean_matches)
            GROUP BY cc.cited_case_id
        )
        SELECT
            lc.case_id,
            lc.case_name,
            lc.appeal_no                                               AS citation,
            lc.court,
            lc.court_type,
            lc.year,
            lc.date_of_order,
            lc.petitioner,
            lc.respondent,
            lc.outcome,
            lc.outcome_summary,
            lc.authority_score,
            lc.citation_count,
            lc.constitutional_articles,
            lc.acts_referred,
            lc.subject_tags,
            COALESCE(cit.cited_by_count, 0)                           AS cited_by_count,
            ca_agg.acts_list,
            ca_agg.sections_list,
            COUNT(*) OVER ()                                           AS total_count
        FROM boolean_matches bm
        JOIN legal_cases lc ON lc.case_id = bm.case_id
        LEFT JOIN case_acts_agg  ca_agg ON ca_agg.case_id  = lc.case_id
        LEFT JOIN citation_counts cit   ON cit.case_id     = lc.case_id
        WHERE 1=1
        {where_clause}
        ORDER BY {order_sql}
        LIMIT {p_limit} OFFSET {p_offset}
    """

    return final_sql, params


def build_snippet_query(case_id: str, search_terms: list[str]) -> tuple[str, list]:
    """
    For a given case_id, retrieve the top 3 most relevant paragraphs
    and generate highlighted KWIC snippets.
    Used after the main result query to get preview text per case.
    """
    if not search_terms:
        sql = """
            SELECT p.paragraph_id, p.para_no, p.text, p.para_type
            FROM legal_paragraphs p
            WHERE p.case_id = %s
            ORDER BY p.quality_score DESC NULLS LAST
            LIMIT 3
        """
        return sql, [case_id]

    # Build tsquery from terms
    tsq_parts = " | ".join(f"plainto_tsquery('english', %s)" for _ in search_terms)
    combined_tsq = " || ".join([f"plainto_tsquery('english', %s)" for _ in search_terms])

    params = [case_id] + search_terms + search_terms

    sql = f"""
        SELECT
            p.paragraph_id,
            p.para_no,
            p.text,
            p.para_type,
            ts_rank(
                to_tsvector('english', p.text),
                ({combined_tsq})
            ) AS rank,
            ts_headline(
                'english',
                p.text,
                ({combined_tsq}),
                'MaxWords=50, MinWords=20, MaxFragments=2, StartSel=<<<, StopSel=>>>'
            ) AS snippet
        FROM legal_paragraphs p
        WHERE p.case_id = %s
          AND to_tsvector('english', p.text) @@ ({combined_tsq})
        ORDER BY rank DESC
        LIMIT 3
    """
    params = search_terms + search_terms + [case_id] + search_terms

    return sql, params


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _operand_value(op: Any) -> str:
    """Extract a plain string value from a leaf operand node."""
    if isinstance(op, (TermNode, PhraseNode)):
        return op.value
    if isinstance(op, WildcardNode):
        return op.root
    if isinstance(op, AtleastNode):
        return op.term
    return str(op)


def _node_to_text_pattern(node: Any) -> str:
    """
    Convert a node to a regex fragment for proximity matching.
    Used only for ProximityNode children.
    """
    if isinstance(node, TermNode):
        return r'\m' + re.escape(node.value) + r'\M'
    if isinstance(node, PhraseNode):
        words = node.value.split()
        return r'\s+'.join(re.escape(w) for w in words)
    if isinstance(node, WildcardNode):
        return r'\m' + re.escape(node.root) + r'\S*\M'
    # Fallback for compound nodes — extract leftmost term
    if isinstance(node, (AndNode, OrNode)):
        return _node_to_text_pattern(node.left)
    if isinstance(node, NotNode):
        return _node_to_text_pattern(node.operand)
    if isinstance(node, FieldNode):
        return _node_to_text_pattern(node.operand)
    return r'\S+'


def extract_search_terms(node: Any) -> list[str]:
    """
    Walk the AST and collect all plain terms and phrases.
    Used for snippet highlighting and frontend term highlighting.
    """
    terms: list[str] = []

    if isinstance(node, TermNode):
        terms.append(node.value)
    elif isinstance(node, PhraseNode):
        terms.append(node.value)
        terms.extend(node.value.split())
    elif isinstance(node, WildcardNode):
        terms.append(node.root)
    elif isinstance(node, AtleastNode):
        terms.append(node.term)
    elif isinstance(node, FieldNode):
        terms.extend(extract_search_terms(node.operand))
    elif isinstance(node, NotNode):
        pass   # don't highlight NOT terms
    elif isinstance(node, (AndNode, OrNode)):
        terms.extend(extract_search_terms(node.left))
        terms.extend(extract_search_terms(node.right))
    elif isinstance(node, ProximityNode):
        terms.extend(extract_search_terms(node.left))
        terms.extend(extract_search_terms(node.right))

    return list(dict.fromkeys(terms))   # deduplicate preserving order


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class ExecutorError(Exception):
    """Raised when the executor cannot build SQL for an AST node."""
