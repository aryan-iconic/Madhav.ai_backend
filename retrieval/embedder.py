"""
retrieval/embedder.py
=====================
Query embedding generator.
Uses sentence-transformers locally (free, no API key needed for MVP).
Model: all-MiniLM-L6-v2 → 384-dimensional vectors
(Same model used to generate your paragraph embeddings in the DB)

Switch to OpenAI/Cohere embeddings later for production.
"""

import logging
from typing import Optional, List
import warnings

# Suppress transformers warnings (optional, but cleaner logs)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*PyTorch.*")

log = logging.getLogger(__name__)

# Load model once at startup (not on every query)
_model = None
_model_failed = False  # Track if we already tried and failed


def _load_model():
    global _model, _model_failed
    
    # If already tried and failed, don't keep retrying
    if _model_failed:
        return None
    
    if _model is None:
        try:
            # Suppress transformers library warnings during import
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from sentence_transformers import SentenceTransformer
            
            log.info("📦 Loading embedding model: all-MiniLM-L6-v2...")
            _model = SentenceTransformer('all-MiniLM-L6-v2')
            log.info("✅ Embedding model loaded: all-MiniLM-L6-v2 (384-dim)")
        except ImportError as e:
            log.error(f"❌ sentence-transformers not installed. Run: pip install sentence-transformers")
            log.error(f"   Error: {e}")
            _model_failed = True
            _model = None
        except Exception as e:
            log.error(f"❌ Failed to load embedding model: {e}")
            log.warning("   Falling back to keyword search only (embeddings will be None)")
            _model_failed = True
            _model = None
    
    return _model


def embed_query(text: str) -> Optional[List[float]]:
    """
    Generate a 384-dim embedding for a query string.
    Returns None if model unavailable (graceful fallback to keyword search).

    Args:
        text: The query string (e.g. "bail conditions under NDPS Act")

    Returns:
        List of 384 floats, or None on failure
    """
    model = _load_model()
    if model is None:
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            embedding = model.encode(text, normalize_embeddings=True)
        return embedding.tolist()
    except Exception as e:
        log.error(f"[EMBED] Failed to embed query '{text[:50]}...': {e}")
        return None


def embed_texts_batch(texts: List[str]) -> Optional[List[List[float]]]:
    """
    Batch embed multiple texts (used during document upload).
    More efficient than calling embed_query() in a loop.
    """
    model = _load_model()
    if model is None:
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [e.tolist() for e in embeddings]
    except Exception as e:
        log.error(f"[EMBED] Batch embedding failed: {e}")
        return None
