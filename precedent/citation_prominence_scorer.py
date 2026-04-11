"""
citation_prominence_scorer.py
============================
Option 1B: Citation Frequency + Court-Aware Prominence Scoring

Calculates case prominence based on:
1. How many times cited (citation frequency)
2. Which court type cited it (court hierarchy)
3. How it was treated (relationship type)
4. Extraction quality (confidence)

Produces meaningful legal intelligence.
Requires parsing citation formats.
"""

import re
from typing import Dict, Tuple, List
import psycopg2
from psycopg2.extras import RealDictCursor


# Court type extraction patterns and weights
COURT_PATTERNS = {
    r'\bSCC\b': ('SCC', 10),           # Supreme Court Reports
    r'\bSCR\b': ('SCR', 10),           # Supreme Court Records
    r'\bAIR\b': ('AIR', 8),            # All India Reporter
    r'\b(Cal|Calcutta)\b': ('Cal', 6),      # Calcutta High Court
    r'\b(Mad|Madras)\b': ('Mad', 6),        # Madras High Court
    r'\b(All|Allahabad)\b': ('All', 6),    # Allahabad High Court
    r'\b(Patna)\b': ('Patna', 6),          # Patna High Court
    r'\b(Delhi|Del)\b': ('Delhi', 6),      # Delhi High Court
    r'\b(Bombay|Bom|Mumbai)\b': ('Mumbai', 6), # Mumbai High Court
    r'\b(AP|AP|Andhra)\b': ('AP', 6),      # Andhra Pradesh High Court
    r'\b(Punjab|Pb)\b': ('Punjab', 6),     # Punjab High Court
}

# Relationship type modifiers
RELATIONSHIP_MODIFIERS = {
    'cited': 1.0,          # Neutral citation
    'approved': 1.3,       # Positive, boosts score
    'followed': 1.2,       # Strong positive
    'affirmed': 1.25,      # Higher court confirmed
    'distinguished': 0.8,  # Limited applicability
    'doubted': 0.7,        # Authority questioned
    'overruled': 0.2,      # Strong negative (kept to allow detection)
}


def extract_court_type(citation_string: str) -> Tuple[str, int]:
    """
    Extract court type from citation string and return court weight.
    
    Args:
        citation_string (str): Citation like "(2008) 13 SCC 506"
        
    Returns:
        tuple: (court_type, weight)
    """
    if not citation_string:
        return ('Unknown', 2)
    
    citation_string = citation_string.upper()
    
    for pattern, (court_name, weight) in COURT_PATTERNS.items():
        if re.search(pattern, citation_string):
            return (court_name, weight)
    
    # Default for unknown courts (District/lower courts)
    return ('Other', 2)


def get_relationship_modifier(relationship: str) -> float:
    """
    Get modifier score for relationship type.
    
    Args:
        relationship (str): Type of citation relationship
        
    Returns:
        float: Modifier value (0.2-1.3)
    """
    if not relationship:
        return 1.0
    
    relationship = relationship.lower().strip()
    return RELATIONSHIP_MODIFIERS.get(relationship, 1.0)


def calculate_prominence_score(
    citation_count: int,
    court_weight: int = 6,
    relationship: str = 'cited',
    avg_confidence: float = 0.6,
    normalize: bool = True
) -> int:
    """
    Calculate prominence score for a case/citation.
    
    Formula:
        score = (citation_count/max_normalized) + 
                (court_weight * 2.0) +
                (relationship_modifier * 1.5) +
                (avg_confidence * 0.5)
    
    Args:
        citation_count (int): Number of times cited
        court_weight (int): Court hierarchy weight (2-10)
        relationship (str): Type of citation relationship
        avg_confidence (float): Average extraction confidence (0-1)
        normalize (bool): Normalize to 0-100 scale
        
    Returns:
        int: Prominence score (0-100)
    """
    if citation_count < 0:
        citation_count = 0
    
    # Normalize citation count
    # Assume max ~500 citations for any case
    max_citations = 500.0
    normalized_count = min((citation_count / max_citations) * 100, 100)
    
    # Get relationship modifier
    rel_modifier = get_relationship_modifier(relationship)
    
    # Confidence component
    confidence_component = (avg_confidence * 0.5) if avg_confidence else 0.25
    
    # Calculate raw score
    score = (
        (normalized_count * 1.0) +              # Citations (0-100)
        (court_weight * 2.0) +                  # Court hierarchy (2-20)
        (rel_modifier * 1.5) +                  # Relationship (0.3-1.95)
        confidence_component                     # Confidence (0-0.5)
    )
    
    # Normalize to 0-100 scale
    if normalize:
        # Max possible: 100 + 20 + 1.95 + 0.5 = 122.45
        max_score = 122.45
        score = (score / max_score) * 100
    
    return max(0, min(100, int(score)))


def score_to_status(score: int, relationship: str = None) -> str:
    """
    Map prominence score to precedent status.
    
    Args:
        score (int): Prominence score (0-100)
        relationship (str): Citation relationship (for overruled handling)
        
    Returns:
        str: Precedent status value
    """
    # Special handling for overruled
    if relationship and relationship.lower() == 'overruled':
        return 'overruled'
    
    # Score-based mapping
    if score >= 80:
        return 'good_law'
    elif score >= 60:
        return 'active_authority'
    elif score >= 40:
        return 'limited_precedent'
    elif score >= 20:
        return 'background'
    else:
        return 'minimal_precedent'


def calculate_citation_stats(conn, target_citation: str) -> Dict:
    """
    Get citation statistics for a target_citation.
    
    Args:
        conn: Database connection
        target_citation (str): Citation string to analyze
        
    Returns:
        dict: Citation statistics
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get basic counts
        cur.execute("""
            SELECT 
                COUNT(*) as total_citations,
                COUNT(DISTINCT source_case_id) as unique_sources,
                AVG(confidence) as avg_confidence,
                COUNT(CASE WHEN relationship = 'overruled' THEN 1 END) as overruled_count,
                COUNT(CASE WHEN relationship = 'approved' THEN 1 END) as approved_count,
                COUNT(CASE WHEN relationship = 'followed' THEN 1 END) as followed_count
            FROM case_citations
            WHERE target_citation = %s
        """, (target_citation,))
        
        stats = cur.fetchone()
        
        # Get most common relationship
        cur.execute("""
            SELECT relationship, COUNT(*) as count
            FROM case_citations
            WHERE target_citation = %s
            GROUP BY relationship
            ORDER BY count DESC
            LIMIT 1
        """, (target_citation,))
        
        rel_result = cur.fetchone()
        primary_relationship = rel_result['relationship'] if rel_result else 'cited'
        
        return {
            'total_citations': stats['total_citations'] or 0,
            'unique_sources': stats['unique_sources'] or 0,
            'avg_confidence': stats['avg_confidence'] or 0.5,
            'overruled_count': stats['overruled_count'] or 0,
            'approved_count': stats['approved_count'] or 0,
            'followed_count': stats['followed_count'] or 0,
            'primary_relationship': primary_relationship,
        }
    
    finally:
        cur.close()


def calculate_prominence_for_citation(
    conn,
    target_citation: str
) -> Dict:
    """
    Full prominence calculation for a citation.
    
    Args:
        conn: Database connection
        target_citation (str): Citation string
        
    Returns:
        dict: Complete prominence analysis
    """
    # Get statistics
    stats = calculate_citation_stats(conn, target_citation)
    
    # Extract court type
    court_name, court_weight = extract_court_type(target_citation)
    
    # Calculate score
    score = calculate_prominence_score(
        citation_count=stats['total_citations'],
        court_weight=court_weight,
        relationship=stats['primary_relationship'],
        avg_confidence=stats['avg_confidence'],
        normalize=True
    )
    
    # Convert score to status
    status = score_to_status(score, stats['primary_relationship'])
    
    return {
        'target_citation': target_citation,
        'prominence_score': score,
        'status': status,
        'strength': score,
        'method': 'frequency_and_court_scoring',
        'citation_count': stats['total_citations'],
        'court_type': court_name,
        'court_weight': court_weight,
        'avg_confidence': round(stats['avg_confidence'], 2),
        'relationship': stats['primary_relationship'],
        'details': {
            'unique_sources': stats['unique_sources'],
            'overruled_count': stats['overruled_count'],
            'approved_count': stats['approved_count'],
            'followed_count': stats['followed_count'],
        }
    }


if __name__ == '__main__':
    # Test examples
    print("Option 1B: Citation Frequency + Court Scoring")
    print("=" * 80)
    
    test_citations = [
        "(2008) 13 SCC 506",
        "AIR 1957 Cal 283",
        "[2011] 10 SCR 557",
        "(1999) 6 SCC 464",
        "Unknown Citation 123",
    ]
    
    for citation in test_citations:
        court_name, weight = extract_court_type(citation)
        score = calculate_prominence_score(
            citation_count=50,
            court_weight=weight,
            relationship='cited',
            avg_confidence=0.6
        )
        status = score_to_status(score)
        
        print(f"\n{citation}")
        print(f"  Court: {court_name} (weight: {weight})")
        print(f"  Score: {score}/100 → Status: {status}")
