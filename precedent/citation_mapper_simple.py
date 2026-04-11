"""
citation_mapper_simple.py
========================
Option 1A: Simple Relationship-Based Status Mapping

Maps relationship field directly to precedent status.
Uses confidence score as strength metric.

No citation counting or court parsing needed.
Fastest implementation (5 min to deploy).
"""

def map_relationship_to_status(relationship):
    """
    Map citation relationship to precedent status.
    
    Args:
        relationship (str): Citation relationship type
        
    Returns:
        str: Precedent status value
    """
    mapping = {
        'cited': 'cited',
        'approved': 'approved',
        'overruled': 'overruled',
        'distinguished': 'distinguished',
        'affirmed': 'affirmed',
        'doubted': 'doubted',
        'followed': 'followed',
    }
    
    return mapping.get(relationship, 'unknown')


def confidence_to_strength(confidence):
    """
    Convert confidence score (0.0-1.0) to strength (0-100).
    
    Args:
        confidence (float): Confidence score 0.0-1.0
        
    Returns:
        int: Strength value 0-100
    """
    if confidence is None:
        return 50
    
    return max(0, min(100, int(confidence * 100)))


def calculate_simple_status(relationship, confidence):
    """
    Calculate precedent status using simple mapping.
    
    Args:
        relationship (str): Citation relationship
        confidence (float): Extraction confidence (0.0-1.0)
        
    Returns:
        dict: {status, strength, method}
    """
    status = map_relationship_to_status(relationship)
    strength = confidence_to_strength(confidence)
    
    return {
        'status': status,
        'strength': strength,
        'method': 'relationship_mapping',
        'citing_count': 0,  # Not applicable in simple mapping
    }


if __name__ == '__main__':
    # Test examples
    test_cases = [
        ('cited', 0.6),
        ('approved', 0.75),
        ('overruled', 0.5),
        ('distinguished', 0.65),
        (None, 0.6),
    ]
    
    print("Option 1A: Simple Relationship Mapping")
    print("=" * 60)
    
    for rel, conf in test_cases:
        result = calculate_simple_status(rel, conf)
        print(f"\nInput: relationship={rel}, confidence={conf}")
        print(f"Output: {result}")
