"""
Smart Fuzzy Matching for Drafting Templates
Provides intelligent template matching when users provide partial/typo inputs
"""

from difflib import SequenceMatcher, get_close_matches
from typing import Dict, List, Tuple, Optional


class TemplateMatcher:
    """Smart fuzzy matching for legal document templates."""

    def __init__(self, templates: Dict):
        """Initialize with available templates."""
        self.templates = templates
        self.template_keys = list(templates.keys())
        self.template_titles = {k: v["title"] for k, v in templates.items()}
        self._build_aliases()

    def _build_aliases(self):
        """Build common aliases for templates."""
        self.aliases = {
            # Criminal
            "bail": "bail_application",
            "bailable": "bail_application",
            "anticipatory": "anticipatory_bail",
            "ab": "anticipatory_bail",
            "quashing": "quashing_petition",
            "fir": "quashing_petition",
            "discharge": "discharge_application",
            "revision": "revision_petition_criminal",
            # Civil
            "plaint": "plaint",
            "suit": "plaint",
            "written": "written_statement",
            "ws": "written_statement",
            "injunction": "injunction_application",
            "interim": "injunction_application",
            "appeal": "appeal_civil",
            "execution": "execution_application",
            "counter": "counter_claim",
            # Constitutional
            "writ": "writ_petition_hc",
            "article226": "writ_petition_hc",
            "article32": "writ_petition_sc",
            "sc": "writ_petition_sc",
            # Notices
            "notice": "legal_notice",
            "legal": "legal_notice",
            "reply": "reply_to_legal_notice",
            # Family
            "divorce": "divorce_petition",
            "maintenance": "maintenance_application",
            "alimony": "maintenance_application",
            # Property
            "eviction": "eviction_petition",
            "tenant": "eviction_petition",
            # Commercial
            "contract": "contract_agreement",
            "agreement": "contract_agreement",
            "affidavit": "affidavit",
            "consumer": "consumer_complaint",
        }

    def find_by_exact_match(self, query: str) -> Optional[str]:
        """Check exact template key match."""
        if query in self.template_keys:
            return query
        return None

    def find_by_alias(self, query: str) -> Optional[str]:
        """Check alias match."""
        query_lower = query.lower().strip()
        return self.aliases.get(query_lower)

    def find_by_fuzzy(self, query: str) -> Tuple[Optional[str], float]:
        """Find closest match using sequence matching."""
        query_lower = query.lower().strip()
        best_key = None
        best_score = 0.0

        for tmpl_key in self.template_keys:
            # Score against template key
            score_key = SequenceMatcher(None, query_lower, tmpl_key.lower()).ratio()
            # Score against template title
            title = self.template_titles[tmpl_key].lower()
            score_title = SequenceMatcher(None, query_lower, title).ratio()
            # Use max of both
            score = max(score_key, score_title)

            if score > best_score:
                best_score = score
                best_key = tmpl_key

        return (best_key, best_score) if best_score >= 0.5 else (None, 0.0)

    def find_closest_matches(self, query: str, n: int = 3) -> List[Tuple[str, float]]:
        """Find N closest template matches with scores."""
        query_lower = query.lower().strip()
        # Combine template keys and titles for matching
        all_options = self.template_keys + list(self.template_titles.values())

        close = get_close_matches(query_lower, all_options, n=n, cutoff=0.3)
        matches = []
        for match in close:
            # Determine if match is key or title
            if match in self.template_keys:
                tmpl_key = match
                score = SequenceMatcher(None, query_lower, match.lower()).ratio()
            else:
                # Find which template has this title
                tmpl_key = next(
                    (k for k, v in self.template_titles.items() if v == match), None
                )
                if not tmpl_key:
                    continue
                score = SequenceMatcher(None, query_lower, match.lower()).ratio()

            matches.append((tmpl_key, score))

        return matches

    def resolve_template(self, query: str) -> Tuple[str, float, str]:
        """
        Resolve template query to best match.
        Returns: (template_key, confidence_score, resolution_method)
        Methods: exact, alias, fuzzy
        """
        # Try exact match first
        exact = self.find_by_exact_match(query)
        if exact:
            return (exact, 1.0, "exact")

        # Try alias
        alias = self.find_by_alias(query)
        if alias:
            return (alias, 0.95, "alias")

        # Try fuzzy matching
        fuzzy_key, fuzzy_score = self.find_by_fuzzy(query)
        if fuzzy_key and fuzzy_score >= 0.6:
            return (fuzzy_key, fuzzy_score, "fuzzy")

        # Return best fuzzy match even if below threshold
        if fuzzy_key:
            return (fuzzy_key, fuzzy_score, "fuzzy_below_threshold")

        return (None, 0.0, "no_match")

    def get_suggestions(self, query: str) -> List[Dict]:
        """Get suggestions with score details."""
        suggestions = []

        # Add alias suggestion if found
        alias = self.find_by_alias(query)
        if alias:
            suggestions.append({
                "template": alias,
                "score": 0.95,
                "method": "alias",
                "title": self.template_titles[alias],
            })

        # Add fuzzy suggestions
        matches = self.find_closest_matches(query, n=3)
        for tmpl_key, score in matches:
            suggestions.append({
                "template": tmpl_key,
                "score": score,
                "method": "fuzzy",
                "title": self.template_titles[tmpl_key],
            })

        # Deduplicate and sort by score
        seen = set()
        unique = []
        for s in sorted(suggestions, key=lambda x: x["score"], reverse=True):
            if s["template"] not in seen:
                unique.append(s)
                seen.add(s["template"])

        return unique[:3]  # Return top 3


# Global matcher instance
_matcher = None


def init_matcher(templates: Dict):
    """Initialize global template matcher."""
    global _matcher
    _matcher = TemplateMatcher(templates)


def resolve_template(query: str) -> Tuple[str, float, str]:
    """Resolve template (uses global matcher)."""
    if _matcher is None:
        raise RuntimeError("TemplateMatcher not initialized. Call init_matcher() first.")
    return _matcher.resolve_template(query)


def get_suggestions(query: str) -> List[Dict]:
    """Get template suggestions."""
    if _matcher is None:
        raise RuntimeError("TemplateMatcher not initialized. Call init_matcher() first.")
    return _matcher.get_suggestions(query)
