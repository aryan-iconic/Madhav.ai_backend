"""
boolean/exceptions.py
=====================
Centralised exception hierarchy for the Boolean search module.

All exceptions map to specific HTTP status codes via the FastAPI
exception handler registered in router.py.

Hierarchy:
    BooleanSearchError          (base)
    ├── ValidationError         422  — bad query syntax (user error)
    ├── ParseError              422  — AST construction failed (user error)
    ├── ExecutorError           500  — SQL build failed (code error)
    ├── DatabaseError           503  — DB unreachable / query timeout
    ├── SnippetError            200  — non-fatal, snippet silently omitted
    └── IndexError              500  — missing GIN index detected
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────

class BooleanSearchError(Exception):
    """Base class for all boolean search exceptions."""

    http_status: int = 500
    error_code:  str = "BOOLEAN_SEARCH_ERROR"

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail  = detail or message

    def to_dict(self) -> dict:
        return {
            "error":      self.error_code,
            "message":    self.message,
            "detail":     self.detail,
            "http_status": self.http_status,
        }


# ─────────────────────────────────────────────────────────────────────────────
# User-facing errors (4xx)
# ─────────────────────────────────────────────────────────────────────────────

class QueryValidationError(BooleanSearchError):
    """
    Raised when the raw query string fails syntax validation.
    This is a USER error — return 422 with the human-readable message.

    Examples:
        Unclosed parenthesis
        Leading wildcard (*term)
        Consecutive operators (AND AND)
    """
    http_status = 422
    error_code  = "QUERY_VALIDATION_ERROR"


class QueryParseError(BooleanSearchError):
    """
    Raised when a validated query fails to parse into an AST.
    Should rarely happen if validator is correct — indicates an
    edge case in the grammar.

    Returns 422 (still a query issue, not a server issue).
    """
    http_status = 422
    error_code  = "QUERY_PARSE_ERROR"


class InvalidFilterError(BooleanSearchError):
    """
    Raised when filter parameters are semantically invalid.
    e.g. year_from > year_to, negative page number, etc.
    """
    http_status = 422
    error_code  = "INVALID_FILTER_ERROR"


class CaseNotFoundError(BooleanSearchError):
    """Raised when a specific case_id is requested but does not exist."""
    http_status = 404
    error_code  = "CASE_NOT_FOUND"

    def __init__(self, case_id: str):
        super().__init__(
            message=f"Case '{case_id}' not found in the database",
            detail=f"No record with case_id = {case_id!r}",
        )
        self.case_id = case_id


# ─────────────────────────────────────────────────────────────────────────────
# Server-side errors (5xx)
# ─────────────────────────────────────────────────────────────────────────────

class QueryExecutorError(BooleanSearchError):
    """
    Raised when the executor cannot translate an AST node to SQL.
    This indicates a code bug (unhandled node type) not a user error.
    """
    http_status = 500
    error_code  = "QUERY_EXECUTOR_ERROR"


class DatabaseQueryError(BooleanSearchError):
    """
    Raised when the DB returns an error executing a search query.
    Wraps psycopg2 exceptions with a sanitised user-facing message.
    """
    http_status = 503
    error_code  = "DATABASE_QUERY_ERROR"

    def __init__(self, original_error: Exception, query_context: str = ""):
        super().__init__(
            message="Database query failed — please try again",
            detail=f"DB error during {query_context}: {type(original_error).__name__}",
        )
        self.original_error  = original_error
        self.query_context   = query_context


class DatabaseConnectionError(BooleanSearchError):
    """Raised when the DB connection cannot be established or has dropped."""
    http_status = 503
    error_code  = "DATABASE_CONNECTION_ERROR"

    def __init__(self, original_error: Exception):
        super().__init__(
            message="Database connection unavailable",
            detail=str(original_error),
        )
        self.original_error = original_error


class MissingIndexError(BooleanSearchError):
    """
    Raised when a required GIN or HNSW index is not found.
    Detected at startup by index_setup.py.
    """
    http_status = 500
    error_code  = "MISSING_INDEX_ERROR"

    def __init__(self, index_name: str, table: str, column: str):
        super().__init__(
            message=f"Required index '{index_name}' is missing on {table}.{column}",
            detail=(
                f"Run boolean/index_setup.sql to create the missing index. "
                f"Boolean search will be extremely slow without it."
            ),
        )
        self.index_name = index_name
        self.table      = table
        self.column     = column


# ─────────────────────────────────────────────────────────────────────────────
# Non-fatal (logged, not raised to user)
# ─────────────────────────────────────────────────────────────────────────────

class SnippetFetchError(BooleanSearchError):
    """
    Raised when KWIC snippet generation fails for one case.
    Non-fatal — the search result is returned without a snippet.
    Caught and logged silently in router._fetch_snippet().
    """
    http_status = 200    # not an HTTP error — internal signal
    error_code  = "SNIPPET_FETCH_ERROR"


class RankingError(BooleanSearchError):
    """
    Raised when relevance scoring fails.
    Non-fatal — falls back to authority_score ordering.
    """
    http_status = 200
    error_code  = "RANKING_ERROR"
