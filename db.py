"""
db.py
=====
PostgreSQL connection manager for Madhav.AI
Uses a simple persistent connection (upgrade to pool in production)
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)

# Load .env file from database/ directory (same level as this Backend folder's parent)
env_path = Path(__file__).parent.parent / "database" / ".env"
if env_path.exists():
    load_dotenv(env_path)
    log.info(f"✅ Loaded .env from {env_path}")
else:
    # Fallback to project root .env
    load_dotenv()
    log.info("⚠️  Using .env from project root or environment variables")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — set these in your .env or environment
# ─────────────────────────────────────────────────────────────────────────────

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "database": os.getenv("DB_NAME", "legal_knowledge_graph"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),  # Changed default to "postgres"
}

_conn = None  # Single connection for MVP (use psycopg2 pool for production)


def get_connection():
    """Get (or create) the database connection"""
    global _conn
    try:
        # Reconnect if connection dropped
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(**DB_CONFIG)
            _conn.autocommit = True
            log.info("✅ DB connected: legal_knowledge_graph")
    except Exception as e:
        log.error(f"❌ DB connection failed: {e}")
        raise
    return _conn


def get_dict_cursor():
    """Returns a cursor that gives dict-like rows"""
    conn = get_connection()
    return conn.cursor(cursor_factory=RealDictCursor)


def close_connection():
    global _conn
    if _conn and not _conn.closed:
        _conn.close()
        log.info("DB connection closed")
