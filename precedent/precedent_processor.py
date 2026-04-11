"""
madhav.ai — Precedent Status Background Processor
Computes & caches precedent status for every case in the DB.

Run this:
  1. Once on deploy  → python -m Backend.precedent.precedent_processor --all
  2. Nightly cron    → python -m Backend.precedent.precedent_processor --since 24
  3. On new case add → python -m Backend.precedent.precedent_processor --case-id case_123

Why a separate job and not live?
  Scanning the full text of 200 citing cases per query takes 2-5 seconds.
  Pre-computing means badge lookups on search results are instant (<50ms).

DB Schema: legal_cases, legal_paragraphs, case_citations, precedent_status
"""

import asyncio
import argparse
import logging
from datetime import datetime, timedelta
from Backend.db import get_connection
from psycopg2.extras import RealDictCursor, Json

# Import Option 1B scoring (citation frequency + court weighting)
from Backend.precedent.citation_prominence_scorer import (
    calculate_prominence_for_citation,
    extract_court_type,
    get_relationship_modifier,
)

# Import detection logic from the router (no circular imports — pure functions)
from Backend.precedent.precedent_router import (
    detect_treatment_in_text,
    extract_context_window,
    determine_status_label,
    score_precedent_strength,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("precedent_processor")


async def compute_status_for_case(conn, case_id: str) -> dict:
    """
    Given a case, compute its precedent status using Option 1B scoring.
    
    Strategy: Analyze what citations THIS case contains
      - Count and weight citations
      - Analyze relationship types
      - Score based on citation frequency & quality
      - Determine if case is "well-reasoned" vs "problematic"
    
    Returns dict ready for upsert into precedent_status table.
    """
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # ── Fetch case info ──
        cursor.execute("""
            SELECT case_id, case_name FROM legal_cases WHERE case_id = %s
        """, (case_id,))
        
        case_row = cursor.fetchone()
        if not case_row:
            log.warning(f"Case {case_id} not found, skipping")
            return None

        # ── Get all citations THIS case makes ──
        cursor.execute("""
            SELECT 
                target_citation,
                relationship,
                confidence,
                COUNT(*) as occurrence_count
            FROM case_citations
            WHERE source_case_id = %s
            GROUP BY target_citation, relationship, confidence
        """, (case_id,))
        
        citations = cursor.fetchall()
        
        if not citations:
            # Case makes no citations - can't determine status
            return {
                "case_id": case_id,
                "status": "unknown",
                "strength": 50,
                "label": "unknown",
                "treatment_counts": {},
                "citing_count": 0,
                "updated_at": datetime.utcnow().isoformat(),
            }

        # ── Analyze citations this case makes ──
        total_citation_strength = 0
        treated_relationships = {}
        avg_confidence = 0
        total_citations = 0

        for citation in citations:
            target = citation['target_citation']
            rel = citation['relationship'] or 'cited'
            conf = citation['confidence'] or 0.6
            count = citation['occurrence_count'] or 1
            
            # Get citation prominence
            try:
                result = calculate_prominence_for_citation(conn, target)
                score = result['prominence_score']
            except:
                # Fallback: use simple court weighting
                _, weight = extract_court_type(target)
                score = (weight / 10.0) * 50  # Scale to 0-50
            
            # Apply relationship modifier
            modifier = get_relationship_modifier(rel)
            weighted_score = score * modifier * count
            
            total_citation_strength += weighted_score
            treated_relationships[rel] = treated_relationships.get(rel, 0) + count
            avg_confidence += conf * count
            total_citations += count

        # Normalize
        avg_confidence = avg_confidence / total_citations if total_citations > 0 else 0.5
        
        # Calculate final strength: how "well-reasoned" is this case?
        # Higher = cites better authorities more frequently
        precedent_strength = min(100, int((total_citation_strength / max(total_citations, 1)) + avg_confidence * 25))
        
        # Determine status based on citation pattern
        if treated_relationships.get('overruled', 0) > (total_citations * 0.5):
            # Majority of citations are overruled → problematic
            status = 'dubious'
            strength = max(20, precedent_strength - 30)
        elif treated_relationships.get('approved', 0) > (total_citations * 0.3) or treated_relationships.get('followed', 0) > (total_citations * 0.3):
            # Good portion are approved/followed
            status = 'reliable'
            strength = min(90, precedent_strength + 10)
        elif precedent_strength >= 75:
            status = 'active_authority'
            strength = precedent_strength
        elif precedent_strength >= 50:
            status = 'cited'
            strength = precedent_strength
        else:
            status = 'limited'
            strength = precedent_strength

        return {
            "case_id": case_id,
            "status": status,
            "strength": strength,
            "label": f"({status}) citing {len(citations)} unique authorities",
            "treatment_counts": treated_relationships,
            "citing_count": len(citations),
            "updated_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        log.error(f"Error computing status for {case_id}: {e}")
        return None
    finally:
        cursor.close()


async def process_batch(conn, case_ids: list) -> int:
    """Process a batch of case IDs. Returns count of successfully processed cases."""
    processed = 0

    for case_id in case_ids:
        try:
            result = await compute_status_for_case(conn, case_id)
            
            if not result:
                continue

            # ── Upsert result into precedent_status table ──
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO precedent_status
                    (case_id, status, strength, label, treatment_counts, citing_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (case_id) DO UPDATE SET
                    status           = EXCLUDED.status,
                    strength         = EXCLUDED.strength,
                    label            = EXCLUDED.label,
                    treatment_counts = EXCLUDED.treatment_counts,
                    citing_count     = EXCLUDED.citing_count,
                    updated_at       = EXCLUDED.updated_at
            """,
                (
                    result["case_id"],
                    result["status"],
                    result["strength"],
                    result["label"],
                    Json(result["treatment_counts"]),  # Wrap dict with Json() for JSONB
                    result["citing_count"],
                    result["updated_at"],
                )
            )
            conn.commit()
            cursor.close()

            processed += 1
            log.info(f"✓ [{case_id}] → {result['status']} "
                     f"(strength: {result['strength']}, citing: {result['citing_count']})")

        except Exception as e:
            log.error(f"✗ Failed to process {case_id}: {e}")
            conn.rollback()
            continue

    return processed


async def run_all(conn):
    """Process every case in the DB. Run once at deploy."""
    log.info("╔════════════════════════════════════════════════════════╗")
    log.info("║  Starting FULL precedent status computation...          ║")
    log.info("╚════════════════════════════════════════════════════════╝")

    cursor = conn.cursor()
    cursor.execute("SELECT case_id FROM legal_cases ORDER BY case_id")
    all_ids = [row[0] for row in cursor.fetchall()]
    cursor.close()

    log.info(f"Found {len(all_ids)} cases to process")

    # Process in batches to avoid memory issues
    BATCH = 100
    total_processed = 0
    
    for i in range(0, len(all_ids), BATCH):
        batch = all_ids[i:i + BATCH]
        count = await process_batch(conn, batch)
        total_processed += count
        progress = (i + len(batch)) / len(all_ids) * 100
        log.info(f"Progress: {progress:.1f}% ({total_processed}/{len(all_ids)})")

    log.info(f"✅ Done. Processed {total_processed}/{len(all_ids)} cases.")


async def run_since(conn, hours: int):
    """Process only cases that have NEW CITATIONS in the last N hours. For nightly cron."""
    since = datetime.utcnow() - timedelta(hours=hours)
    log.info(f"Processing cases with new citations since {since.isoformat()}")

    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT source_case_id
        FROM case_citations
        WHERE created_at >= %s
        AND source_case_id IS NOT NULL
    """, (since,))
    
    case_ids = [row[0] for row in cursor.fetchall()]
    cursor.close()

    log.info(f"Found {len(case_ids)} cases to update")
    count = await process_batch(conn, case_ids)
    log.info(f"✅ Done. Processed {count} cases.")


async def run_single(conn, case_id: str):
    """Process a single case. Call this when a new case is added."""
    log.info(f"Processing single case: {case_id}")
    count = await process_batch(conn, [case_id])
    if count:
        log.info(f"✅ Done. Case processed and cached.")
    else:
        log.warning(f"⚠️  Case not found or processing failed")


# ─────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Madhav.ai Precedent Status Processor")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all",      action="store_true",       help="Process all cases (first-time setup)")
    group.add_argument("--since",    type=int, metavar="HOURS", help="Process cases with citations added in last N hours")
    group.add_argument("--case-id",  type=str,                  help="Process a single case by ID")
    args = parser.parse_args()

    try:
        conn = get_connection()
        
        if args.all:
            asyncio.run(run_all(conn))
        elif args.since:
            asyncio.run(run_since(conn, args.since))
        elif args.case_id:
            asyncio.run(run_single(conn, args.case_id))
        
        conn.close()
        log.info("✅ Processor finished successfully")
        
    except Exception as e:
        log.error(f"❌ Processor failed: {e}")
        exit(1)
