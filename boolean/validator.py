"""
boolean/validator.py
====================
Validates raw Boolean query strings BEFORE parsing or DB touch.

Rules enforced:
  - Parentheses must be balanced
  - Quotes must be paired
  - No leading/trailing operators
  - No consecutive binary operators
  - No leading wildcard  (*word)
  - W/n / NEAR/n / PRE/n must have a positive integer n
  - atleast must have form atleast<n>(<term>)
  - Empty parentheses not allowed
  - Query must not be blank
  - Maximum query length enforced
  - NOT must be preceded by a term or closing paren
"""

from __future__ import annotations
import re
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MAX_QUERY_LENGTH   = 2000          # characters
MAX_PAREN_DEPTH    = 10            # nested parentheses limit

# Binary operators (require terms on BOTH sides)
_BINARY_OPS = {"AND", "OR"}

# All operator keywords
_ALL_OP_KEYWORDS = {"AND", "OR", "NOT"}

# Proximity pattern:  W/5  NEAR/3  PRE/10
_PROX_RE = re.compile(r"^(W|NEAR|PRE)/(\d+)$", re.IGNORECASE)

# atleast pattern:  atleast3(negligence)
_ATLEAST_RE = re.compile(r"^atleast(\d+)\((.+)\)$", re.IGNORECASE)

# Leading wildcard:  *word  or  ?word
_LEADING_WILD_RE = re.compile(r"(?<!\w)[*?!]\w")

# Field qualifier:   title:  court:  judge:  act:  section:
_FIELD_RE = re.compile(r"^[a-z_]+:$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid:   bool
    error:   str | None = None   # human-readable message for API response
    details: str | None = None   # optional technical detail

    def __bool__(self) -> bool:
        return self.valid


# ─────────────────────────────────────────────────────────────────────────────
# Tokenizer (lightweight — just for validation, not full AST)
# ─────────────────────────────────────────────────────────────────────────────

def _lex(query: str) -> list[str]:
    """
    Split query into tokens, respecting quoted phrases.
    Returns a flat list of string tokens.
    Quotes are returned as single tokens including their content: '"natural justice"'
    """
    tokens: list[str] = []
    i = 0
    n = len(query)

    while i < n:
        # Skip whitespace
        if query[i].isspace():
            i += 1
            continue

        # Quoted phrase — collect until closing quote
        if query[i] == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 1
            # j now points to closing quote (or end of string if unclosed)
            tokens.append(query[i : j + 1])
            i = j + 1
            continue

        # Normal token — collect until whitespace or quote
        j = i
        while j < n and not query[j].isspace() and query[j] != '"':
            j += 1
        token = query[i:j]
        if token:
            # Do NOT split parens from atleast tokens — atleast3(term) must stay whole
            # Do not split atleast3(term) — parens are part of the token
            if _ATLEAST_RE.match(token):
                tokens.append(token)
            else:
                # Split parentheses off edges of tokens  e.g.  "(murder"  →  ["(", "murder"]
                sub = _split_parens(token)
                tokens.extend(sub)
        i = j

    return tokens


def _split_parens(token: str) -> list[str]:
    """
    Split parentheses from the edges of a token.
    e.g.  "(murder)"  →  ["(", "murder", ")"]
          "((art"     →  ["(", "(", "art"]
    """
    result: list[str] = []
    # Leading parens
    while token.startswith("("):
        result.append("(")
        token = token[1:]
    # Trailing parens — collect them, append after
    trailing: list[str] = []
    while token.endswith(")"):
        trailing.append(")")
        token = token[:-1]
    if token:
        result.append(token)
    result.extend(trailing)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_length(query: str) -> ValidationResult | None:
    if not query.strip():
        return ValidationResult(False, "Query cannot be empty")
    if len(query) > MAX_QUERY_LENGTH:
        return ValidationResult(
            False,
            f"Query too long ({len(query)} chars). Maximum is {MAX_QUERY_LENGTH}."
        )
    return None


def _check_quotes(query: str) -> ValidationResult | None:
    """Quotes must appear in pairs."""
    count = query.count('"')
    if count % 2 != 0:
        return ValidationResult(
            False,
            'Unclosed quotation mark — every opening " must have a matching closing "'
        )
    return None


def _check_parens(tokens: list[str]) -> ValidationResult | None:
    """
    Parentheses must be balanced and non-empty.
    Also enforces max nesting depth.
    """
    depth = 0
    prev = None

    for tok in tokens:
        if tok == "(":
            depth += 1
            if depth > MAX_PAREN_DEPTH:
                return ValidationResult(
                    False,
                    f"Parentheses nested too deeply (max {MAX_PAREN_DEPTH} levels)"
                )
        elif tok == ")":
            # Empty group: ) immediately follows ( with no terms inside
            if prev == "(":
                return ValidationResult(
                    False,
                    'Empty parentheses "()" are not allowed — add search terms inside'
                )
            if depth == 0:
                return ValidationResult(
                    False,
                    'Unexpected closing parenthesis ")" — no matching opening "("'
                )
            depth -= 1
        prev = tok

    if depth != 0:
        plural = "parenthesis" if depth == 1 else "parentheses"
        return ValidationResult(
            False,
            f'{depth} unclosed {plural} — add {depth} closing ")" to your query'
        )
    return None


def _check_operator_position(tokens: list[str]) -> ValidationResult | None:
    """
    - Query cannot start with a binary operator (AND / OR)
    - Query cannot end with any operator
    - Two consecutive binary operators are not allowed
    - NOT cannot appear after a binary operator without a term between them
      (AND NOT is legal as a phrase, but AND AND is not)
    """
    if not tokens:
        return ValidationResult(False, "Query contains no terms")

    # Filter out parens for positional checks
    meaningful = [t for t in tokens if t not in ("(", ")")]

    if not meaningful:
        return ValidationResult(False, "Query contains only parentheses with no terms")

    first = meaningful[0].upper()
    last  = meaningful[-1].upper()

    if first in _BINARY_OPS:
        return ValidationResult(
            False,
            f'Query cannot start with operator "{meaningful[0]}" — add a search term before it'
        )

    if last in _ALL_OP_KEYWORDS or _PROX_RE.match(last):
        return ValidationResult(
            False,
            f'Query cannot end with operator "{meaningful[-1]}" — add a search term after it'
        )

    # Consecutive binary operators
    for i in range(len(meaningful) - 1):
        curr = meaningful[i].upper()
        nxt  = meaningful[i + 1].upper()
        if curr in _BINARY_OPS and nxt in _BINARY_OPS:
            return ValidationResult(
                False,
                f'Two consecutive operators "{meaningful[i]} {meaningful[i+1]}" — remove one'
            )

    return None


def _check_proximity(tokens: list[str]) -> ValidationResult | None:
    """
    W/n, NEAR/n, PRE/n must:
    - Have a valid positive integer n  (1–100)
    - Not appear at start or end of meaningful token list
    """
    meaningful = [t for t in tokens if t not in ("(", ")")]

    for i, tok in enumerate(meaningful):
        m = _PROX_RE.match(tok)
        if not m:
            continue
        op_type = m.group(1).upper()
        n_str   = m.group(2)
        n       = int(n_str)

        if n < 1:
            return ValidationResult(
                False,
                f'Proximity distance in "{tok}" must be at least 1'
            )
        if n > 100:
            return ValidationResult(
                False,
                f'Proximity distance in "{tok}" is too large (max 100 words)'
            )
        if i == 0:
            return ValidationResult(
                False,
                f'"{tok}" cannot be at the start — it needs a term on the left'
            )
        if i == len(meaningful) - 1:
            return ValidationResult(
                False,
                f'"{tok}" cannot be at the end — it needs a term on the right'
            )

    return None


def _check_wildcards(query: str) -> ValidationResult | None:
    """
    Leading wildcards are illegal: *word  ?word
    Wildcard-only tokens are illegal: bare * or ?
    """
    if _LEADING_WILD_RE.search(query):
        return ValidationResult(
            False,
            "Leading wildcard (*word or ?word) is not allowed — "
            "place the wildcard AFTER the term root (e.g. constitu*)"
        )

    # Bare wildcard token
    tokens = _lex(query)
    for tok in tokens:
        if tok in ("*", "?", "!"):
            return ValidationResult(
                False,
                f'Bare wildcard "{tok}" is not valid — attach it to a term (e.g. judg*)'
            )
    return None


def _check_atleast(tokens: list[str]) -> ValidationResult | None:
    """
    atleast must match:  atleast<n>(<term>)
    where n is a positive integer and <term> is non-empty.
    """
    for tok in tokens:
        lower = tok.lower()
        if not lower.startswith("atleast"):
            continue
        m = _ATLEAST_RE.match(tok)
        if not m:
            return ValidationResult(
                False,
                f'Invalid atleast syntax: "{tok}" — correct form is atleast3(term), e.g. atleast3(negligence)'
            )
        n    = int(m.group(1))
        term = m.group(2).strip()
        if n < 1:
            return ValidationResult(
                False,
                f'atleast count must be at least 1 (got atleast{n})'
            )
        if n > 50:
            return ValidationResult(
                False,
                f'atleast count {n} is unreasonably high (max 50)'
            )
        if not term:
            return ValidationResult(
                False,
                f'atleast has empty term: "{tok}" — provide a search term inside the parentheses'
            )
    return None


def _check_field_qualifiers(tokens: list[str]) -> ValidationResult | None:
    """
    Field qualifiers (court:, judge:, act:, section:, title:) must:
    - Be followed by a value token (not an operator or paren)
    - Use known field names only
    """
    known_fields = {
        "court", "judge", "judges", "act", "section",
        "title", "case", "year", "citation", "petitioner",
        "respondent", "article", "keyword"
    }

    meaningful = [t for t in tokens if t not in ("(", ")")]

    for i, tok in enumerate(meaningful):
        # Token contains colon and does not start with a quote → field qualifier candidate
        if ":" in tok and not tok.startswith('"'):
            # Extract field name — everything before the first colon
            field = tok.split(":")[0].lower()

            # Only check tokens where the field part is purely alphabetic/underscore
            # (avoids flagging URLs, citations like "Civil:123", etc.)
            if not field.replace("_", "").isalpha():
                continue

            # Unknown field name check — applies to BOTH "field:" and "field:value" forms
            if field not in known_fields:
                return ValidationResult(
                    False,
                    f'Unknown field qualifier "{field}:" — '
                    f'valid fields: {", ".join(sorted(known_fields))}'
                )

            # For standalone "field:" (no value attached), check the next token
            if tok.endswith(":"):
                if i == len(meaningful) - 1:
                    return ValidationResult(
                        False,
                        f'Field qualifier "{tok}" has no value after it'
                    )
                nxt = meaningful[i + 1].upper()
                if nxt in _ALL_OP_KEYWORDS or nxt in ("(", ")"):
                    return ValidationResult(
                        False,
                        f'Field qualifier "{tok}" must be followed by a search value, not "{meaningful[i+1]}"'
                    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────────────────────────────────────

def validate_boolean_query(query: str) -> ValidationResult:
    """
    Run all validation checks on a raw Boolean query string.

    Returns ValidationResult(valid=True) if query is safe to parse.
    Returns ValidationResult(valid=False, error=<message>) on first failure.

    Checks are ordered from cheapest to most expensive.
    Stops at the first failure (fail-fast).

    IMPORTANT — why we use  (fn()) is not None  instead of  if err := fn():
      Each check returns either None (passed) or ValidationResult(valid=False, ...).
      ValidationResult.__bool__ returns self.valid, so a failing result is falsy.
      The naive walrus pattern  if err := fn():  would never trigger on failures
      because  bool(ValidationResult(False, ...)) == False.
      Using  is not None  correctly detects any returned ValidationResult object.

    Usage:
        result = validate_boolean_query(user_input)
        if not result:
            raise HTTPException(status_code=422, detail=result.error)
    """
    # ① Length / empty
    if (err := _check_length(query)) is not None:
        return err

    # ② Quote pairing (before tokenising — avoids confusing the lexer)
    if (err := _check_quotes(query)) is not None:
        return err

    # ③ Leading wildcards (regex on raw string — fast)
    if (err := _check_wildcards(query)) is not None:
        return err

    # ④ Tokenise
    tokens = _lex(query)

    # ⑤ Parentheses balance and depth
    if (err := _check_parens(tokens)) is not None:
        return err

    # ⑥ Operator position (start / end / consecutive)
    if (err := _check_operator_position(tokens)) is not None:
        return err

    # ⑦ Proximity operator validity
    if (err := _check_proximity(tokens)) is not None:
        return err

    # ⑧ atleast syntax
    if (err := _check_atleast(tokens)) is not None:
        return err

    # ⑨ Field qualifier validity
    if (err := _check_field_qualifiers(tokens)) is not None:
        return err

    return ValidationResult(valid=True)
