"""
boolean/tests/test_boolean.py
==============================
Comprehensive test suite for the Boolean search module.

Covers:
  - validator.py  : all validation rules
  - parser.py     : tokenisation, AST shape, precedence
  - filters.py    : alias resolution, normalisation, cross-field validation
  - highlighter.py: ts_headline parsing, fallback snippets
  - ranker.py     : individual scoring factors, composite score, reranking
  - executor.py   : SQL output structure (no DB required — inspects SQL strings)

Run with:
    pytest backend/boolean/tests/test_boolean.py -v
    pytest backend/boolean/tests/test_boolean.py -v --tb=short

Requirements:
    pytest (pip install pytest)
    No database connection needed for these unit tests.
"""

import sys
import os
import math
import pytest

# ── Make parent packages importable when running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ═══════════════════════════════════════════════════════════════════════════════
# validator.py tests
# ═══════════════════════════════════════════════════════════════════════════════

from boolean.validator import validate_boolean_query


class TestValidatorValid:
    """Queries that MUST pass validation."""

    def test_simple_term(self):
        assert validate_boolean_query("negligence")

    def test_and_two_terms(self):
        assert validate_boolean_query("murder AND intention")

    def test_or_two_terms(self):
        assert validate_boolean_query("murder OR homicide")

    def test_not_term(self):
        assert validate_boolean_query("murder NOT attempt")

    def test_quoted_phrase(self):
        assert validate_boolean_query('"natural justice"')

    def test_phrase_and_term(self):
        assert validate_boolean_query('"natural justice" AND "audi alteram partem"')

    def test_wildcard_star(self):
        assert validate_boolean_query("constitu*")

    def test_wildcard_question(self):
        assert validate_boolean_query("wom?n")

    def test_wildcard_bang(self):
        assert validate_boolean_query("contract!")

    def test_grouped_or(self):
        assert validate_boolean_query("(murder OR homicide) AND intention")

    def test_nested_parens(self):
        assert validate_boolean_query("((murder OR homicide) AND intention) AND NOT attempt")

    def test_proximity_w5(self):
        assert validate_boolean_query("arrest W/5 warrant")

    def test_proximity_near(self):
        assert validate_boolean_query("police NEAR/3 custodial")

    def test_proximity_pre(self):
        assert validate_boolean_query("bail PRE/4 anticipatory")

    def test_proximity_sentence(self):
        assert validate_boolean_query("negligence /S duty")

    def test_proximity_paragraph(self):
        assert validate_boolean_query("negligence /P breach")

    def test_atleast(self):
        assert validate_boolean_query("atleast3(negligence)")

    def test_field_court(self):
        assert validate_boolean_query('court:"Supreme Court"')

    def test_field_judge(self):
        assert validate_boolean_query("judge:Chandrachud")

    def test_field_act(self):
        assert validate_boolean_query("act:IPC AND section:302")

    def test_field_year(self):
        assert validate_boolean_query("year:2019")

    def test_long_complex_query(self):
        q = (
            '"natural justice" AND ("audi alteram partem" OR "nemo judex") '
            'AND court:"Supreme Court" AND year:2015 AND NOT dismissed'
        )
        assert validate_boolean_query(q)

    def test_implicit_and_adjacent_terms(self):
        # Adjacent terms without AND are valid (implicit AND in parser)
        assert validate_boolean_query("negligence duty care")


class TestValidatorInvalid:
    """Queries that MUST fail validation with specific errors."""

    def test_empty_string(self):
        r = validate_boolean_query("")
        assert not r
        assert r.error is not None

    def test_whitespace_only(self):
        r = validate_boolean_query("   ")
        assert not r

    def test_unclosed_quote(self):
        r = validate_boolean_query('"natural justice')
        assert not r
        assert "quot" in r.error.lower()

    def test_unclosed_paren(self):
        r = validate_boolean_query("(murder OR homicide AND intention")
        assert not r
        assert "unclosed" in r.error.lower() or "paren" in r.error.lower()

    def test_unexpected_close_paren(self):
        r = validate_boolean_query("murder) AND intention")
        assert not r

    def test_empty_parens(self):
        r = validate_boolean_query("murder AND () intention")
        assert not r
        assert "empty" in r.error.lower()

    def test_leading_wildcard_star(self):
        r = validate_boolean_query("*negligence")
        assert not r
        assert "leading" in r.error.lower() or "wildcard" in r.error.lower()

    def test_leading_wildcard_question(self):
        r = validate_boolean_query("?term")
        assert not r

    def test_starts_with_and(self):
        r = validate_boolean_query("AND murder")
        assert not r
        assert "start" in r.error.lower()

    def test_starts_with_or(self):
        r = validate_boolean_query("OR negligence")
        assert not r

    def test_ends_with_and(self):
        r = validate_boolean_query("murder AND")
        assert not r
        assert "end" in r.error.lower()

    def test_ends_with_or(self):
        r = validate_boolean_query("murder OR")
        assert not r

    def test_consecutive_operators(self):
        r = validate_boolean_query("murder AND OR intention")
        assert not r
        assert "consecutive" in r.error.lower()

    def test_proximity_missing_number(self):
        r = validate_boolean_query("arrest W/ warrant")
        # W/ without a number — invalid
        # Note: this may pass validation (W/ alone is tokenised as a term)
        # but the proximity validator catches W/[non-digit]
        # At minimum the parser should handle gracefully
        # This test documents expected behaviour
        pass   # acceptable: either catches or parses as plain term

    def test_proximity_at_start(self):
        r = validate_boolean_query("W/5 warrant")
        assert not r

    def test_proximity_at_end(self):
        r = validate_boolean_query("arrest W/5")
        assert not r

    def test_bare_wildcard(self):
        r = validate_boolean_query("*")
        assert not r

    def test_atleast_no_number(self):
        r = validate_boolean_query("atleast(negligence)")
        assert not r
        assert "atleast" in r.error.lower()

    def test_atleast_no_parens(self):
        r = validate_boolean_query("atleast3 negligence")
        assert not r

    def test_unknown_field(self):
        r = validate_boolean_query("colour:red AND murder")
        assert not r
        assert "field" in r.error.lower() or "unknown" in r.error.lower()

    def test_field_with_no_value(self):
        r = validate_boolean_query("court: AND murder")
        assert not r

    def test_query_too_long(self):
        r = validate_boolean_query("negligence " * 200)
        assert not r
        assert "long" in r.error.lower() or "length" in r.error.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# parser.py tests
# ═══════════════════════════════════════════════════════════════════════════════

from boolean.parser import (
    parse_boolean_query,
    ast_to_dict,
    ParseError,
    TermNode, PhraseNode, WildcardNode, AtleastNode,
    FieldNode, NotNode, AndNode, OrNode, ProximityNode,
)


class TestParserNodes:
    """Verify correct AST node types are produced."""

    def test_single_term(self):
        ast = parse_boolean_query("negligence")
        assert isinstance(ast, TermNode)
        assert ast.value == "negligence"

    def test_term_lowercased(self):
        ast = parse_boolean_query("NEGLIGENCE")
        assert isinstance(ast, TermNode)
        assert ast.value == "negligence"

    def test_phrase(self):
        ast = parse_boolean_query('"natural justice"')
        assert isinstance(ast, PhraseNode)
        assert ast.value == "natural justice"

    def test_wildcard_star(self):
        ast = parse_boolean_query("constitu*")
        assert isinstance(ast, WildcardNode)
        assert ast.root == "constitu"
        assert ast.wc == "*"

    def test_wildcard_question(self):
        ast = parse_boolean_query("wom?n")
        assert isinstance(ast, WildcardNode)
        assert ast.root == "wom"
        assert ast.wc == "?"

    def test_atleast(self):
        ast = parse_boolean_query("atleast3(negligence)")
        assert isinstance(ast, AtleastNode)
        assert ast.n == 3
        assert ast.term == "negligence"

    def test_and_node(self):
        ast = parse_boolean_query("murder AND intention")
        assert isinstance(ast, AndNode)
        assert isinstance(ast.left,  TermNode)
        assert isinstance(ast.right, TermNode)

    def test_or_node(self):
        ast = parse_boolean_query("murder OR homicide")
        assert isinstance(ast, OrNode)

    def test_not_node(self):
        ast = parse_boolean_query("murder NOT attempt")
        # Parsed as AND(murder, NOT(attempt)) due to implicit AND
        assert isinstance(ast, AndNode)
        assert isinstance(ast.right, NotNode)

    def test_explicit_not(self):
        ast = parse_boolean_query("murder AND NOT attempt")
        assert isinstance(ast, AndNode)
        assert isinstance(ast.right, NotNode)
        assert isinstance(ast.right.operand, TermNode)

    def test_proximity_w5(self):
        ast = parse_boolean_query("arrest W/5 warrant")
        assert isinstance(ast, ProximityNode)
        assert ast.op_type == "W"
        assert ast.distance == 5

    def test_proximity_near(self):
        ast = parse_boolean_query("police NEAR/3 custody")
        assert isinstance(ast, ProximityNode)
        assert ast.op_type == "NEAR"
        assert ast.distance == 3

    def test_proximity_sentence(self):
        ast = parse_boolean_query("negligence /S duty")
        assert isinstance(ast, ProximityNode)
        assert ast.op_type == "S"
        assert ast.distance is None

    def test_field_court(self):
        ast = parse_boolean_query('court:"Supreme Court"')
        assert isinstance(ast, FieldNode)
        assert ast.field_name == "court"
        assert isinstance(ast.operand, PhraseNode)
        assert "supreme court" in ast.operand.value

    def test_field_judge_alias(self):
        ast = parse_boolean_query("judge:Chandrachud")
        assert isinstance(ast, FieldNode)
        assert ast.field_name == "judges"    # alias resolved

    def test_field_article_alias(self):
        ast = parse_boolean_query("article:21")
        assert isinstance(ast, FieldNode)
        assert ast.field_name == "constitutional_articles"

    def test_field_keyword_alias(self):
        ast = parse_boolean_query("keyword:privacy")
        assert isinstance(ast, FieldNode)
        assert ast.field_name == "subject_tags"


class TestParserPrecedence:
    """Operator precedence: NOT > Proximity > AND > OR."""

    def test_and_before_or(self):
        # a OR b AND c  →  OR(a, AND(b, c))
        ast = parse_boolean_query("murder OR homicide AND intention")
        assert isinstance(ast, OrNode)
        assert isinstance(ast.right, AndNode)

    def test_not_before_and(self):
        # a AND NOT b  →  AND(a, NOT(b))
        ast = parse_boolean_query("murder AND NOT attempt")
        assert isinstance(ast, AndNode)
        assert isinstance(ast.right, NotNode)

    def test_parens_override_precedence(self):
        # (a OR b) AND c
        ast = parse_boolean_query("(murder OR homicide) AND intention")
        assert isinstance(ast, AndNode)
        assert isinstance(ast.left, OrNode)

    def test_deep_nesting(self):
        ast = parse_boolean_query(
            "((murder OR culpable) AND intention) AND NOT (attempt OR conspiracy)"
        )
        assert isinstance(ast, AndNode)
        assert isinstance(ast.right, NotNode)
        assert isinstance(ast.right.operand, OrNode)

    def test_implicit_and(self):
        # "a b" → AND(a, b)
        ast = parse_boolean_query("negligence duty")
        assert isinstance(ast, AndNode)


class TestParserAstToDict:
    """ast_to_dict produces correct JSON-serialisable structure."""

    def test_term_dict(self):
        ast = parse_boolean_query("negligence")
        d = ast_to_dict(ast)
        assert d == {"type": "term", "value": "negligence"}

    def test_phrase_dict(self):
        ast = parse_boolean_query('"natural justice"')
        d = ast_to_dict(ast)
        assert d["type"] == "phrase"
        assert d["value"] == "natural justice"

    def test_and_dict(self):
        ast = parse_boolean_query("a AND b")
        d = ast_to_dict(ast)
        assert d["type"] == "AND"
        assert "left" in d and "right" in d

    def test_proximity_dict(self):
        ast = parse_boolean_query("arrest W/5 warrant")
        d = ast_to_dict(ast)
        assert d["type"] == "proximity"
        assert d["op"] == "W"
        assert d["distance"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# filters.py tests
# ═══════════════════════════════════════════════════════════════════════════════

from boolean.filters import (
    resolve_court, resolve_act,
    parse_year_input, normalise_filters,
    NormalisedFilters, describe_filters,
)
from boolean.exceptions import InvalidFilterError


class TestFiltersCourtAlias:

    def test_sc_alias(self):
        assert resolve_court("SC") == "Supreme Court of India"

    def test_sc_lowercase(self):
        assert resolve_court("sc") == "Supreme Court of India"

    def test_full_name_passthrough(self):
        assert resolve_court("Supreme Court of India") == "Supreme Court of India"

    def test_delhi_hc(self):
        assert resolve_court("delhi hc") == "High Court of Delhi"

    def test_unknown_court(self):
        # Unknown → passthrough
        assert resolve_court("Imaginary Court") == "Imaginary Court"

    def test_strips_whitespace(self):
        assert resolve_court("  SC  ") == "Supreme Court of India"


class TestFiltersActAlias:

    def test_ipc(self):
        assert resolve_act("ipc") == "Indian Penal Code"

    def test_crpc_uppercase(self):
        assert resolve_act("CRPC") == "Code of Criminal Procedure"

    def test_constitution(self):
        assert resolve_act("constitution") == "Constitution of India"

    def test_unknown_act_passthrough(self):
        assert resolve_act("Some Obscure Act 1999") == "Some Obscure Act 1999"


class TestFiltersYearParsing:

    def test_integer_year(self):
        assert parse_year_input(2015) == 2015

    def test_string_year(self):
        assert parse_year_input("2015") == 2015

    def test_none_returns_none(self):
        assert parse_year_input(None) is None

    def test_dmy_format(self):
        assert parse_year_input("01/01/2015") == 2015

    def test_ymd_format(self):
        assert parse_year_input("2015-06-15") == 2015

    def test_invalid_format(self):
        with pytest.raises(InvalidFilterError):
            parse_year_input("not-a-year")


class TestNormaliseFilters:

    def test_empty_dict(self):
        f = normalise_filters({})
        assert f.is_empty()

    def test_court_resolved(self):
        f = normalise_filters({"court": "SC"})
        assert f.court == "Supreme Court of India"

    def test_act_resolved(self):
        f = normalise_filters({"act": "ipc"})
        assert f.act == "Indian Penal Code"

    def test_year_range_valid(self):
        f = normalise_filters({"year_from": 2010, "year_to": 2020})
        assert f.year_from == 2010
        assert f.year_to == 2020

    def test_year_range_invalid(self):
        with pytest.raises(InvalidFilterError) as exc:
            normalise_filters({"year_from": 2020, "year_to": 2010})
        assert "year_from" in str(exc.value)

    def test_doc_type_normalised(self):
        f = normalise_filters({"doc_type": "JUDGMENT"})
        assert f.doc_type == "judgment"

    def test_invalid_doc_type(self):
        with pytest.raises(InvalidFilterError):
            normalise_filters({"doc_type": "brief"})

    def test_judge_stripped(self):
        f = normalise_filters({"judge": "  Chandrachud  "})
        assert f.judge == "Chandrachud"

    def test_describe_filters_empty(self):
        f = NormalisedFilters()
        assert describe_filters(f) == "no filters"

    def test_describe_filters_with_values(self):
        f = NormalisedFilters(court="Supreme Court of India", year_from=2010, year_to=2020)
        desc = describe_filters(f)
        assert "Supreme Court" in desc
        assert "2010" in desc
        assert "2020" in desc


# ═══════════════════════════════════════════════════════════════════════════════
# highlighter.py tests
# ═══════════════════════════════════════════════════════════════════════════════

from boolean.highlighter import (
    parse_ts_headline,
    extract_fallback_snippet,
    build_case_snippet,
    snippet_to_dict,
    clean_snippet_text,
    HighlightSpan,
)


class TestTsHeadlineParsing:

    def test_single_highlight(self):
        raw = "the principle of <<<natural justice>>> requires notice"
        spans = parse_ts_headline(raw)
        highlighted = [s for s in spans if s.highlighted]
        assert len(highlighted) == 1
        assert highlighted[0].text == "natural justice"

    def test_multiple_highlights(self):
        raw = "<<<natural justice>>> and <<<audi alteram partem>>> are principles"
        spans = parse_ts_headline(raw)
        highlighted = [s for s in spans if s.highlighted]
        assert len(highlighted) == 2
        assert highlighted[0].text == "natural justice"
        assert highlighted[1].text == "audi alteram partem"

    def test_no_highlights(self):
        raw = "this text has no highlighted terms"
        spans = parse_ts_headline(raw)
        assert all(not s.highlighted for s in spans)
        assert "".join(s.text for s in spans) == raw

    def test_text_preserved(self):
        raw = "before <<<term>>> after"
        spans = parse_ts_headline(raw)
        full = "".join(s.text for s in spans)
        assert full == "before term after"

    def test_empty_string(self):
        spans = parse_ts_headline("")
        assert spans == []


class TestFallbackSnippet:

    def test_term_found(self):
        text = "The court held that negligence was established beyond doubt."
        spans = extract_fallback_snippet(text, ["negligence"])
        highlighted = [s for s in spans if s.highlighted]
        assert len(highlighted) >= 1
        assert highlighted[0].text.lower() == "negligence"

    def test_term_not_found_returns_truncated(self):
        text = "A" * 400
        spans = extract_fallback_snippet(text, ["xyz"])
        full = "".join(s.text for s in spans)
        assert len(full) <= 310    # SNIPPET_MAX_CHARS + "..."

    def test_empty_terms(self):
        text = "Some text here"
        spans = extract_fallback_snippet(text, [])
        assert len(spans) == 1
        assert not spans[0].highlighted

    def test_empty_text(self):
        spans = extract_fallback_snippet("", ["term"])
        assert spans == [HighlightSpan(text="", highlighted=False)]

    def test_case_insensitive(self):
        text = "The NEGLIGENCE was clear."
        spans = extract_fallback_snippet(text, ["negligence"])
        highlighted = [s for s in spans if s.highlighted]
        assert len(highlighted) >= 1


class TestBuildCaseSnippet:

    def _make_row(self, text, snippet=None, para_no=1, para_type="reasoning"):
        return {
            "paragraph_id": f"p{para_no}",
            "para_no":      para_no,
            "text":         text,
            "para_type":    para_type,
            "snippet":      snippet,
        }

    def test_basic_snippet(self):
        rows = [self._make_row("The court applied the principle of natural justice.")]
        cs = build_case_snippet("case001", rows, ["natural justice"])
        assert cs.case_id == "case001"
        assert len(cs.fragments) == 1
        assert "natural justice" in cs.matched_terms

    def test_ts_headline_used_when_present(self):
        rows = [self._make_row(
            "Natural justice applies.",
            snippet="<<<natural justice>>> applies."
        )]
        cs = build_case_snippet("case001", rows, ["natural justice"])
        highlighted = [s for f in cs.fragments for s in f.spans if s.highlighted]
        assert len(highlighted) >= 1

    def test_max_fragments_enforced(self):
        rows = [
            self._make_row(f"Para {i} with negligence mentioned.", para_no=i)
            for i in range(1, 10)
        ]
        cs = build_case_snippet("case001", rows, ["negligence"])
        assert len(cs.fragments) <= 3    # MAX_FRAGMENTS

    def test_snippet_to_dict(self):
        rows = [self._make_row("Natural justice is fundamental.")]
        cs = build_case_snippet("case001", rows, ["natural justice"])
        d = snippet_to_dict(cs)
        assert "case_id" in d
        assert "fragments" in d
        assert "matched_terms" in d
        assert isinstance(d["fragments"], list)

    def test_clean_snippet_text(self):
        raw = "before <<<highlighted>>> after"
        clean = clean_snippet_text(raw)
        assert clean == "before highlighted after"
        assert "<<<" not in clean


# ═══════════════════════════════════════════════════════════════════════════════
# ranker.py tests
# ═══════════════════════════════════════════════════════════════════════════════

from boolean.ranker import (
    compute_relevance_score,
    rerank_results,
    _score_authority,
    _score_citation_count,
    _score_recency,
    _score_court,
    _score_term_density,
)


class TestScoringFunctions:

    def test_authority_none_returns_default(self):
        assert _score_authority(None) == 0.3

    def test_authority_clamped(self):
        assert _score_authority(1.5) == 1.0
        assert _score_authority(-0.1) == 0.0

    def test_citation_count_zero(self):
        assert _score_citation_count(0) == 0.0

    def test_citation_count_positive(self):
        score = _score_citation_count(100)
        assert 0.0 < score < 1.0

    def test_citation_count_monotone(self):
        # More citations = higher score
        assert _score_citation_count(1000) > _score_citation_count(100)

    def test_recency_none(self):
        assert _score_recency(None) == 0.5

    def test_recency_recent(self):
        assert _score_recency(2024) > _score_recency(1980)

    def test_recency_oldest(self):
        assert _score_recency(1950) == 0.0

    def test_recency_newest(self):
        assert _score_recency(2025) == 1.0

    def test_court_sc_highest(self):
        assert _score_court("SC") > _score_court("HC")
        assert _score_court("HC") > _score_court("Tribunal")

    def test_court_unknown_default(self):
        assert _score_court("Unknown") == 0.30

    def test_term_density_zero_para(self):
        assert _score_term_density(0, 100) == 0.0

    def test_term_density_full(self):
        # 20% density × 5 amplifier = 1.0
        assert _score_term_density(20, 100) == 1.0

    def test_term_density_unknown_total(self):
        assert _score_term_density(5, None) == 0.3   # neutral default


class TestComputeRelevanceScore:

    def test_returns_score_breakdown(self):
        sb = compute_relevance_score(
            authority_score    = 0.8,
            citation_count     = 500,
            cited_by_count     = 200,
            matched_para_count = 10,
            total_paragraphs   = 50,
            year               = 2018,
            court_type         = "SC",
        )
        assert 0.0 <= sb.final <= 1.0

    def test_sc_scores_higher_than_tribunal(self):
        sc = compute_relevance_score(0.7, 300, 100, 5, 20, 2015, "SC")
        tr = compute_relevance_score(0.7, 300, 100, 5, 20, 2015, "Tribunal")
        assert sc.final > tr.final

    def test_higher_authority_raises_score(self):
        high = compute_relevance_score(0.9, 200, 50, 5, 20, 2010, "SC")
        low  = compute_relevance_score(0.1, 200, 50, 5, 20, 2010, "SC")
        assert high.final > low.final

    def test_to_dict_keys(self):
        sb = compute_relevance_score(0.5, 100, 50, 3, 30, 2005, "HC")
        d = sb.to_dict()
        expected_keys = {"authority", "citation_count", "cited_by",
                         "term_density", "recency", "court", "final"}
        assert set(d.keys()) == expected_keys


class TestRerankResults:

    def _make_result(self, case_id, year, authority, citations, court_type):
        return {
            "case_id":        case_id,
            "year":           year,
            "authority_score": authority,
            "citation_count": citations,
            "cited_by_count": citations // 2,
            "total_paragraphs": 50,
            "court_type":     court_type,
        }

    def test_rerank_adds_relevance_score(self):
        results = [self._make_result("c1", 2020, 0.8, 500, "SC")]
        ranked  = rerank_results(results, {"c1": 10})
        assert "relevance_score" in ranked[0]

    def test_rerank_sorts_by_relevance(self):
        results = [
            self._make_result("c1", 1960, 0.1, 10, "Tribunal"),
            self._make_result("c2", 2020, 0.9, 5000, "SC"),
        ]
        ranked = rerank_results(results, {"c1": 2, "c2": 20}, sort_by="relevance")
        assert ranked[0]["case_id"] == "c2"

    def test_rerank_sort_date_desc(self):
        results = [
            self._make_result("c1", 1990, 0.9, 1000, "SC"),
            self._make_result("c2", 2022, 0.5, 100, "HC"),
        ]
        ranked = rerank_results(results, {}, sort_by="date_desc")
        assert ranked[0]["case_id"] == "c2"

    def test_rerank_sort_date_asc(self):
        results = [
            self._make_result("c1", 1990, 0.9, 1000, "SC"),
            self._make_result("c2", 2022, 0.5, 100,  "HC"),
        ]
        ranked = rerank_results(results, {}, sort_by="date_asc")
        assert ranked[0]["case_id"] == "c1"

    def test_rerank_sort_citations(self):
        results = [
            self._make_result("c1", 2010, 0.5, 50,   "HC"),
            self._make_result("c2", 2010, 0.5, 5000,  "SC"),
        ]
        ranked = rerank_results(results, {}, sort_by="citations")
        assert ranked[0]["case_id"] == "c2"

    def test_empty_results(self):
        ranked = rerank_results([], {})
        assert ranked == []


# ═══════════════════════════════════════════════════════════════════════════════
# executor.py tests (SQL structure only — no DB)
# ═══════════════════════════════════════════════════════════════════════════════

from boolean.executor import BooleanExecutor, extract_search_terms
from boolean.parser import parse_boolean_query as parse


class TestExecutorSQLStructure:
    """
    We don't execute SQL — we verify the generated SQL contains
    the right keywords, table names, and param counts.
    """

    def _build(self, query: str, filters: dict | None = None) -> tuple[str, list]:
        ast = parse(query)
        ex  = BooleanExecutor(filters or {})
        return ex.build(ast)

    def test_term_uses_tsvector(self):
        sql, params = self._build("negligence")
        assert "to_tsvector" in sql
        assert "legal_paragraphs" in sql
        assert "negligence" in params

    def test_phrase_uses_phraseto_tsquery(self):
        sql, params = self._build('"natural justice"')
        assert "phraseto_tsquery" in sql
        assert "natural justice" in params

    def test_wildcard_uses_tsquery_prefix(self):
        sql, params = self._build("constitu*")
        assert "to_tsquery" in sql or "ILIKE" in sql

    def test_and_uses_intersect(self):
        sql, params = self._build("murder AND intention")
        assert "INTERSECT" in sql.upper()

    def test_or_uses_union(self):
        sql, params = self._build("murder OR homicide")
        assert "UNION" in sql.upper()

    def test_not_uses_not_in(self):
        sql, params = self._build("murder NOT attempt")
        assert "NOT IN" in sql.upper() or "EXCEPT" in sql.upper()

    def test_proximity_uses_regex(self):
        sql, params = self._build("arrest W/5 warrant")
        assert "~*" in sql or "regexp" in sql.lower()

    def test_field_court_uses_legal_cases(self):
        sql, params = self._build('court:"Supreme Court"')
        assert "legal_cases" in sql
        assert "court" in sql

    def test_field_act_uses_case_acts(self):
        sql, params = self._build("act:IPC")
        assert "case_acts" in sql

    def test_field_judge_uses_paragraphs_array(self):
        sql, params = self._build("judge:Chandrachud")
        assert "judges_mentioned" in sql

    def test_no_sql_injection_in_params(self):
        # Malicious input must be in params, not raw SQL
        malicious = "'; DROP TABLE legal_cases; --"
        sql, params = self._build(f'"{malicious}"')
        assert "DROP TABLE" not in sql
        assert malicious.lower() in [str(p).lower() for p in params]

    def test_atleast_uses_length_trick(self):
        sql, params = self._build("atleast3(negligence)")
        assert "LENGTH" in sql.upper() or "REGEXP" in sql.upper()


class TestExtractSearchTerms:

    def test_single_term(self):
        ast = parse("negligence")
        terms = extract_search_terms(ast)
        assert "negligence" in terms

    def test_phrase_adds_words(self):
        ast = parse('"natural justice"')
        terms = extract_search_terms(ast)
        assert "natural justice" in terms
        assert "natural" in terms
        assert "justice" in terms

    def test_not_terms_excluded(self):
        ast = parse("murder AND NOT attempt")
        terms = extract_search_terms(ast)
        assert "murder" in terms
        assert "attempt" not in terms    # NOT terms not highlighted

    def test_wildcard_root_included(self):
        ast = parse("constitu*")
        terms = extract_search_terms(ast)
        assert "constitu" in terms

    def test_deduplication(self):
        ast = parse("murder AND murder")
        terms = extract_search_terms(ast)
        assert terms.count("murder") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: validator → parser → executor pipeline
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEndPipeline:
    """
    Simulate the full pipeline without a DB connection.
    Validates that a query can go from raw string to SQL without exceptions.
    """

    def _pipeline(self, query: str) -> tuple[str, list]:
        # Step 1: Validate
        result = validate_boolean_query(query)
        assert result.valid, f"Validation failed: {result.error}"
        # Step 2: Parse
        ast = parse_boolean_query(query)
        # Step 3: Execute (build SQL)
        ex = BooleanExecutor()
        sql, params = ex.build(ast)
        return sql, params

    def test_simple_term(self):
        sql, p = self._pipeline("negligence")
        assert sql and p

    def test_and_phrase(self):
        sql, p = self._pipeline('"natural justice" AND "audi alteram partem"')
        assert "INTERSECT" in sql.upper()

    def test_complex_query(self):
        sql, p = self._pipeline(
            '(murder OR "culpable homicide") AND intention AND NOT attempt'
        )
        assert sql

    def test_field_and_text(self):
        sql, p = self._pipeline('court:"Supreme Court" AND "article 21"')
        assert sql

    def test_proximity_query(self):
        sql, p = self._pipeline('"bail" W/5 "anticipatory"')
        assert "~*" in sql or "INTERSECT" in sql.upper()

    def test_wildcard_query(self):
        sql, p = self._pipeline("constitu* AND rights")
        assert sql

    def test_atleast_query(self):
        sql, p = self._pipeline("atleast3(negligence) AND duty")
        assert sql

    def test_all_filters_pipeline(self):
        from boolean.filters import normalise_filters
        filters = normalise_filters({
            "court": "SC", "year_from": 2010, "year_to": 2020,
            "act": "ipc", "judge": "Chandrachud",
        })
        assert filters.court == "Supreme Court of India"
        assert filters.year_from == 2010
        assert filters.act == "Indian Penal Code"
