"""
madhav.ai — Phrase & Abbreviation Matcher
Ensures "supreme court", "SC", "supeme court" all return 2997 cases

Key Features:
  1. PHRASE DETECTION: Handle multi-word terms as single units
  2. ABBREVIATION MAPPING: SC → Supreme Court, HC → High Court
  3. FUZZY MATCHING: supeme → supreme (within term, not whole query)
  4. FIELD-AWARE: Apply logic to each field separately
"""

import re
from typing import Dict, List, Tuple, Optional
from difflib import SequenceMatcher

# ═══════════════════════════════════════════════════════════════════════════════
# PHRASE DICTIONARY: Multi-word terms that must stay together
# ═══════════════════════════════════════════════════════════════════════════════

LEGAL_PHRASES = {
    # Court names (2+ words - MUST match as phrase)
    "supreme court": {
        "type": "court",
        "canonical": "Supreme Court of India",
        "variations": ["supreme court", "supreme court of india", "supream court"],
        "abbreviations": ["SC"],
        "priority": 100,  # Highest priority - exact field match
    },
    "high court": {
        "type": "court",
        "canonical": "High Court",
        "variations": ["high court", "highcourt", "hc"],
        "abbreviations": ["HC"],
        "priority": 100,
    },
    "parliamentary commission": {
        "type": "court",
        "canonical": "Parliamentary Commission",
        "variations": ["parliamentary commission", "parliament commission", "pcommission"],
        "abbreviations": ["PC"],
        "priority": 100,
    },
    "district court": {
        "type": "court",
        "canonical": "District Court",
        "variations": ["district court", "districtcourt"],
        "abbreviations": ["DC"],
        "priority": 95,
    },
    
    # Bail types
    "anticipatory bail": {
        "type": "concept",
        "canonical": "anticipatory bail",
        "variations": ["anticipatory bail", "anticipatry bail", "anti bail"],
        "abbreviations": ["AB"],
        "priority": 90,
    },
    "regular bail": {
        "type": "concept",
        "canonical": "regular bail",
        "variations": ["regular bail", "reguler bail"],
        "abbreviations": [],
        "priority": 85,
    },
    
    # Acts
    "indian penal code": {
        "type": "act",
        "canonical": "Indian Penal Code",
        "variations": ["indian penal code", "indian penalcode", "penal code"],
        "abbreviations": ["IPC"],
        "priority": 95,
    },
    "criminal procedure code": {
        "type": "act",
        "canonical": "Criminal Procedure Code",
        "variations": ["criminal procedure code", "criminalprocedure code"],
        "abbreviations": ["CRPC", "CPC"],
        "priority": 95,
    },
    "civil procedure code": {
        "type": "act",
        "canonical": "Civil Procedure Code",
        "variations": ["civil procedure code", "civalprocedure code"],
        "abbreviations": ["CPC"],
        "priority": 95,
    },
}

# Build reverse indexes
PHRASE_BY_CANONICAL = {v["canonical"].lower(): k for k, v in LEGAL_PHRASES.items()}
PHRASE_BY_ABBREVIATION = {}
for phrase, info in LEGAL_PHRASES.items():
    for abbrev in info.get("abbreviations", []):
        PHRASE_BY_ABBREVIATION[abbrev.lower()] = phrase


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION DETECTION: Find "Section 370", "S. 498A", etc.
# ═══════════════════════════════════════════════════════════════════════════════

SECTION_PATTERNS = [
    r'\bsection\s+(\d{1,4}[a-zA-Z]?)\b',  # "section 370", "Section 498A"
    r'\bs\.?\s+(\d{1,4}[a-zA-Z]?)\b',      # "s. 370", "S. 498A"
    r'\barticle\s+(\d{1,3})\b',             # "article 15", "Article 370"
    r'\bart\.?\s+(\d{1,3})\b',              # "art. 15", "Art. 370"
]


class SectionDetector:
    """Detect legal sections and articles in queries"""
    
    @staticmethod
    def extract_sections(query: str) -> List[str]:
        """Extract all section/article references from query
        
        Args:
            query: User query
            
        Returns:
            List of section numbers found
            
        Examples:
            "section 370 of IPC" → ["370"]
            "sections 498A and 302" → ["498A", "302"]
        """
        sections = []
        for pattern in SECTION_PATTERNS:
            matches = re.finditer(pattern, query, re.IGNORECASE)
            for match in matches:
                section = match.group(1)
                if section not in sections:
                    sections.append(section)
        return sections
    
    @staticmethod
    def get_section_query(sections: List[str]) -> List[Tuple[str, tuple]]:
        """Build SQL queries for section matching
        
        Args:
            sections: List of section numbers
            
        Returns:
            List of (SQL_clause, params) tuples
        """
        queries = []
        for section in sections:
            # Search in acts_referred field
            sql = """
            SELECT COUNT(*) as cnt FROM legal_cases
            WHERE acts_referred ILIKE %s OR constitutional_articles ILIKE %s
            """
            # Use LIKE pattern for partial matching
            pattern = f"%{section}%"
            queries.append((sql, (pattern, pattern)))
        return queries


# ═══════════════════════════════════════════════════════════════════════════════
# PHRASE MATCHER: Handle multi-word terms correctly
# ═══════════════════════════════════════════════════════════════════════════════

class PhraseMatcher:
    """Match phrases and abbreviations to canonical forms"""
    
    def __init__(self):
        self.phrases = LEGAL_PHRASES
        self.phrase_abbrev_map = PHRASE_BY_ABBREVIATION
    
    def normalize_query(self, query: str) -> str:
        """
        Normalize query by expanding abbreviations and fixing common variations.
        
        Args:
            query: Raw user query
            
        Returns:
            Normalized query
            
        Example:
            normalize_query("SC case") → "Supreme Court case"
        """
        query_lower = query.lower()
        normalized = query_lower
        
        # 1. Expand abbreviations first (highest priority)
        for abbrev, canonical_phrase in self.phrase_abbrev_map.items():
            # Match as whole word: " SC " or "SC," or start/end
            abbrev_pattern = r'\b' + re.escape(abbrev) + r'\b'
            if re.search(abbrev_pattern, normalized, re.IGNORECASE):
                # Replace abbreviation with canonical phrase
                phrase_info = self.phrases[canonical_phrase]
                normalized = re.sub(
                    abbrev_pattern,
                    phrase_info["canonical"].lower(),
                    normalized,
                    flags=re.IGNORECASE
                )
        
        return normalized
    
    def detect_phrases(self, query: str) -> List[Dict]:
        """
        Detect multi-word legal phrases in query.
        
        Args:
            query: User query
            
        Returns:
            List of detected phrases with metadata
            
        Example:
            detect_phrases("supreme court case") → 
            [{"phrase": "supreme court", "canonical": "Supreme Court of India", ...}]
        """
        query_lower = query.lower()
        detected = []
        used_chars = set()  # Track which characters are part of detected phrases
        
        # Build list of all phrases and variations to search for
        all_phrases = []
        for canonical_phrase, info in self.phrases.items():
            all_phrases.append((canonical_phrase, canonical_phrase, info))
            # Also add variations
            for variation in info.get("variations", []):
                if variation != canonical_phrase:
                    all_phrases.append((variation, canonical_phrase, info))
        
        # Sort by length (longest first) to avoid partial matches
        all_phrases.sort(key=lambda x: len(x[0]), reverse=True)
        
        for phrase_to_search, canonical_key, info in all_phrases:
            # Search for exact phrase
            pattern = r'\b' + re.escape(phrase_to_search) + r'\b'
            for match in re.finditer(pattern, query_lower, re.IGNORECASE):
                start, end = match.span()
                # Check if this phrase overlaps with already detected ones
                if not any(i in used_chars for i in range(start, end)):
                    detected.append({
                        "phrase": phrase_to_search,
                        "matched_text": match.group(),
                        "canonical": info["canonical"],
                        "type": info["type"],
                        "priority": info["priority"],
                        "abbreviations": info.get("abbreviations", []),
                        "position": start,
                    })
                    # Mark these characters as used
                    for i in range(start, end):
                        used_chars.add(i)
        
        # Sort by priority (higher priority first)
        detected.sort(key=lambda x: x["priority"], reverse=True)
        return detected
    
    def match_to_field(self, query: str, field: str) -> Optional[Tuple[str, float, str]]:
        """
        Match query to a specific database field with confidence.
        
        Args:
            query: User query
            field: Field name (court, outcome, etc.)
            
        Returns:
            Tuple of (matched_value, confidence, match_type) or None
            
        Examples:
            match_to_field("SC case", "court") → ("Supreme Court of India", 0.95, "abbreviation")
            match_to_field("supeme court", "court") → ("Supreme Court of India", 0.85, "fuzzy")
            match_to_field("supreme court", "court") → ("Supreme Court of India", 1.0, "exact")
        """
        query_lower = query.lower()
        
        # Check for abbreviations first (highest confidence)
        for abbrev, phrase in self.phrase_abbrev_map.items():
            abbrev_pattern = r'\b' + re.escape(abbrev) + r'\b'
            if re.search(abbrev_pattern, query_lower, re.IGNORECASE):
                phrase_info = self.phrases[phrase]
                if phrase_info["type"] == field or field == "any":
                    return (phrase_info["canonical"], 0.95, "abbreviation")
        
        # Then try direct phrase detection
        normalized = self.normalize_query(query)
        detected_phrases = self.detect_phrases(normalized)
        
        for phrase_info in detected_phrases:
            if phrase_info["type"] == field or field == "any":
                return (phrase_info["canonical"], 1.0, "exact_phrase")
        
        # Then try fuzzy matching on the query tokens
        query_tokens = query_lower.split()
        for token in query_tokens:
            for phrase in self.phrases:
                phrase_tokens = phrase.split()
                # Check if token fuzzy-matches any phrase token
                for phrase_token in phrase_tokens:
                    similarity = SequenceMatcher(None, token, phrase_token).ratio()
                    if similarity > 0.85:  # Good match
                        phrase_info = self.phrases[phrase]
                        if phrase_info["type"] == field or field == "any":
                            return (phrase_info["canonical"], similarity, "fuzzy_token")
        
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# RANKING SYSTEM: Match type determines result priority
# ═══════════════════════════════════════════════════════════════════════════════

class QueryRanker:
    """Rank queries by match type and confidence"""
    
    MATCH_TYPE_SCORES = {
        "exact_phrase": 100,      # Perfect match: "supreme court"
        "abbreviation": 95,        # Abbreviation: "SC"
        "exact_case": 90,          # Exact word match: "contract"
        "fuzzy_token": 85,         # Fuzzy token: "supeme" ~ "supreme"
        "section_match": 80,       # Section number: "370"
        "semantic": 50,            # Semantic search (fallback)
    }
    
    @staticmethod
    def rank_matches(matches: List[Dict]) -> List[Dict]:
        """
        Rank query matches by confidence and type.
        
        Args:
            matches: List of potential matches
            
        Returns:
            Sorted list (highest confidence first)
        """
        for match in matches:
            match_type = match.get("match_type", "semantic")
            confidence = match.get("confidence", 0.5)
            base_score = QueryRanker.MATCH_TYPE_SCORES.get(match_type, 50)
            match["rank_score"] = base_score * confidence
        
        return sorted(matches, key=lambda x: x["rank_score"], reverse=True)


# ═══════════════════════════════════════════════════════════════════════════════
# FIELD MATCHER: Multi-field aware query matching
# ═══════════════════════════════════════════════════════════════════════════════

class FieldAwareMatcher:
    """Match queries to database fields intelligently"""
    
    TYPE_TO_FIELDS = {
        "court": ["court", "court_type"],
        "act": ["acts_referred", "constitutional_articles"],
        "concept": ["subject_tags", "headnotes"],
    }
    
    def __init__(self):
        self.phrase_matcher = PhraseMatcher()
        self.section_detector = SectionDetector()
    
    def match_query_to_field(self, query: str) -> List[Tuple[str, str, float, str]]:
        """
        Match query to all relevant fields with confidence scores.
        
        Returns:
            List of (field, value, confidence, match_type) tuples
            Sorted by confidence descending
        """
        matches = []
        
        # 1. Try phrase matching
        for field_type, field_names in self.TYPE_TO_FIELDS.items():
            result = self.phrase_matcher.match_to_field(query, field_type)
            if result:
                matched_value, confidence, match_type = result
                for field_name in field_names:
                    matches.append((field_name, matched_value, confidence, match_type))
        
        # 2. Try section detection
        sections = self.section_detector.extract_sections(query)
        if sections:
            for section in sections:
                matches.append(("acts_referred", section, 0.90, "section_match"))
                matches.append(("constitutional_articles", section, 0.90, "section_match"))
        
        # 3. Rank and return
        matches_ranked = QueryRanker.rank_matches([
            {
                "field": m[0],
                "value": m[1],
                "confidence": m[2],
                "match_type": m[3],
            }
            for m in matches
        ])
        
        return [(m["field"], m["value"], m["confidence"], m["match_type"]) 
                for m in matches_ranked]


# ═══════════════════════════════════════════════════════════════════════════════
# TEST / DEMO
# ═══════════════════════════════════════════════════════════════════════════════

def test_phrase_matcher():
    """Demonstrate phrase matching capabilities"""
    print("\n" + "="*80)
    print("PHRASE MATCHER TEST - Consistent Results Across Variations")
    print("="*80)
    
    matcher = PhraseMatcher()
    detector = SectionDetector()
    field_matcher = FieldAwareMatcher()
    
    test_queries = [
        ("supreme court", "Should find Supreme Court of India"),
        ("SC", "Abbreviation for Supreme Court"),
        ("supeme court", "Typo in 'supreme'"),
        ("high court", "Should find High Court"),
        ("HC", "Abbreviation for High Court"),
        ("section 370", "Should detect article/section"),
        ("S. 498A", "Section format variation"),
        ("anticipatory bail", "Multi-word legal concept"),
        ("parliamentary commission case", "Parliamentary Commission"),
    ]
    
    for query, description in test_queries:
        print(f"\n📝 Query: '{query}' ({description})")
        
        # Normalize
        normalized = matcher.normalize_query(query)
        print(f"   ➜ Normalized: '{normalized}'")
        
        # Detect phrases
        phrases = matcher.detect_phrases(normalized)
        if phrases:
            for p in phrases:
                print(f"   ✅ Phrase: {p['phrase']} → {p['canonical']} "
                      f"(priority: {p['priority']}, type: {p['type']})")
        
        # Detect sections
        sections = detector.extract_sections(query)
        if sections:
            print(f"   ✅ Sections: {sections}")
        
        # Field matching
        field_matches = field_matcher.match_query_to_field(query)
        if field_matches:
            print(f"   ✅ Field Matches:")
            for field, value, conf, match_type in field_matches[:2]:
                print(f"      {field} = '{value}' (confidence: {conf:.0%}, type: {match_type})")


if __name__ == "__main__":
    test_phrase_matcher()
