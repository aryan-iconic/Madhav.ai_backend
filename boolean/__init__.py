"""
boolean/
========
Boolean search module for Madhav.AI legal database.

Mount in your main FastAPI app:

    from boolean.router import router as boolean_router
    app.include_router(boolean_router)

Endpoints registered:
  POST /boolean/search
  POST /boolean/validate
  POST /boolean/parse
  GET  /boolean/case/{case_id}
  GET  /boolean/suggestions
  GET  /boolean/health
"""

from .router      import router
from .validator   import validate_boolean_query, ValidationResult
from .parser      import parse_boolean_query, ParseError, ast_to_dict
from .executor    import BooleanExecutor, extract_search_terms, ExecutorError
from .filters     import normalise_filters, NormalisedFilters, describe_filters
from .ranker      import rerank_results, compute_relevance_score
from .highlighter import build_case_snippet, snippet_to_dict
from .exceptions  import (
    BooleanSearchError,
    QueryValidationError,
    QueryParseError,
    QueryExecutorError,
    DatabaseQueryError,
    DatabaseConnectionError,
    CaseNotFoundError,
    InvalidFilterError,
)

__all__ = [
    # Router
    "router",
    # Validator
    "validate_boolean_query",
    "ValidationResult",
    # Parser
    "parse_boolean_query",
    "ParseError",
    "ast_to_dict",
    # Executor
    "BooleanExecutor",
    "extract_search_terms",
    "ExecutorError",
    # Filters
    "normalise_filters",
    "NormalisedFilters",
    "describe_filters",
    # Ranker
    "rerank_results",
    "compute_relevance_score",
    # Highlighter
    "build_case_snippet",
    "snippet_to_dict",
    # Exceptions
    "BooleanSearchError",
    "QueryValidationError",
    "QueryParseError",
    "QueryExecutorError",
    "DatabaseQueryError",
    "DatabaseConnectionError",
    "CaseNotFoundError",
    "InvalidFilterError",
]
