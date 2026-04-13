"""
boolean/parser.py
=================
Parses a validated Boolean query string into an Abstract Syntax Tree (AST).

Supported syntax:
  - AND / OR / NOT
  - W/n   NEAR/n   PRE/n   (proximity operators)
  - /S    /P       (same-sentence / same-paragraph)
  - "phrase"       (exact phrase)
  - term*  term?   term!   (wildcards)
  - atleast3(term) (minimum occurrence count)
  - (...)          (grouping / precedence)
  - field:value    (field-qualified search)
      court:  judge:  act:  section:  title:  year:
      petitioner:  respondent:  article:  keyword:  citation:

Operator precedence (high → low):
  1. NOT          (unary, right-binding)
  2. Proximity    (W/n, NEAR/n, PRE/n, /S, /P)
  3. AND
  4. OR

AST Node types (dataclasses):
  TermNode        — single keyword or wildcard
  PhraseNode      — exact quoted phrase
  WildcardNode    — term with * ? ! suffix
  AtleastNode     — atleast<n>(<term>)
  FieldNode       — field:value  (wraps any node)
  NotNode         — unary NOT
  AndNode         — binary AND
  OrNode          — binary OR
  ProximityNode   — W/n | NEAR/n | PRE/n | /S | /P
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# AST Node definitions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TermNode:
    value: str                    # normalised lowercase term

    def __repr__(self) -> str:
        return f"Term({self.value!r})"


@dataclass
class PhraseNode:
    value: str                    # phrase without quotes, lowercase

    def __repr__(self) -> str:
        return f"Phrase({self.value!r})"


@dataclass
class WildcardNode:
    root:   str                   # term root before wildcard char
    wc:     str                   # wildcard char: * ? !

    def __repr__(self) -> str:
        return f"Wildcard({self.root!r}{self.wc})"


@dataclass
class AtleastNode:
    n:    int                     # minimum occurrences
    term: str                     # term to count (lowercase)

    def __repr__(self) -> str:
        return f"Atleast({self.n}, {self.term!r})"


@dataclass
class FieldNode:
    field_name: str               # normalised field name
    operand:    Any               # any Node

    def __repr__(self) -> str:
        return f"Field({self.field_name!r}, {self.operand!r})"


@dataclass
class NotNode:
    operand: Any

    def __repr__(self) -> str:
        return f"NOT({self.operand!r})"


@dataclass
class AndNode:
    left:  Any
    right: Any

    def __repr__(self) -> str:
        return f"AND({self.left!r}, {self.right!r})"


@dataclass
class OrNode:
    left:  Any
    right: Any

    def __repr__(self) -> str:
        return f"OR({self.left!r}, {self.right!r})"


@dataclass
class ProximityNode:
    left:     Any
    right:    Any
    op_type:  str        # "W", "NEAR", "PRE", "S", "P"
    distance: int | None # word distance for W/NEAR/PRE; None for /S /P

    def __repr__(self) -> str:
        d = f"/{self.distance}" if self.distance else ""
        return f"Prox({self.op_type}{d}, {self.left!r}, {self.right!r})"


# ─────────────────────────────────────────────────────────────────────────────
# Token types
# ─────────────────────────────────────────────────────────────────────────────

TK_TERM    = "TERM"
TK_PHRASE  = "PHRASE"
TK_AND     = "AND"
TK_OR      = "OR"
TK_NOT     = "NOT"
TK_PROX    = "PROX"       # W/n  NEAR/n  PRE/n  /S  /P
TK_LPAREN  = "LPAREN"
TK_RPAREN  = "RPAREN"
TK_EOF     = "EOF"


@dataclass
class Token:
    type:  str
    value: str
    # Extra metadata for proximity tokens
    op_type:  str | None = None
    distance: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Regex helpers
# ─────────────────────────────────────────────────────────────────────────────

_PROX_RE     = re.compile(r"^(W|NEAR|PRE)/(\d+)$", re.IGNORECASE)
_SENT_PARA   = re.compile(r"^/(S|P)$", re.IGNORECASE)
_ATLEAST_RE  = re.compile(r"^atleast(\d+)\((.+)\)$", re.IGNORECASE)
_WILDCARD_RE  = re.compile(r"^(.+?)([*!])$")       # * and ! — must be at end of token
_MIDWILD_RE   = re.compile(r"^(.+?)[?](.*)$")       # ? anywhere in token (wom?n, judg?ment)
_FIELD_RE    = re.compile(r"^([a-z_]+):(.*)$", re.IGNORECASE)

# Known field name aliases → canonical names
_FIELD_ALIASES: dict[str, str] = {
    "court":       "court",
    "judge":       "judges",
    "judges":      "judges",
    "act":         "act",
    "section":     "section",
    "title":       "case_name",
    "case":        "case_name",
    "case_name":   "case_name",
    "year":        "year",
    "citation":    "citation",
    "petitioner":  "petitioner",
    "respondent":  "respondent",
    "article":     "constitutional_articles",
    "keyword":     "subject_tags",
    "keywords":    "subject_tags",
    "subject":     "subject_tags",
}


# ─────────────────────────────────────────────────────────────────────────────
# Lexer
# ─────────────────────────────────────────────────────────────────────────────

def _lex(query: str) -> list[Token]:
    """
    Convert raw query string to a list of Token objects.
    Handles:
      - quoted phrases → TK_PHRASE
      - parentheses    → TK_LPAREN / TK_RPAREN
      - operators      → TK_AND / TK_OR / TK_NOT
      - proximity ops  → TK_PROX
      - everything else → TK_TERM (incl. wildcards, field:value, atleast)
    """
    tokens: list[Token] = []
    i = 0
    raw = query.strip()
    n = len(raw)

    while i < n:
        # ── whitespace
        if raw[i].isspace():
            i += 1
            continue

        # ── quoted phrase
        if raw[i] == '"':
            j = i + 1
            while j < n and raw[j] != '"':
                j += 1
            phrase = raw[i + 1 : j].strip().lower()
            tokens.append(Token(TK_PHRASE, phrase))
            i = j + 1
            continue

        # ── opening paren
        if raw[i] == "(":
            tokens.append(Token(TK_LPAREN, "("))
            i += 1
            continue

        # ── closing paren — but first strip trailing parens off current word
        if raw[i] == ")":
            tokens.append(Token(TK_RPAREN, ")"))
            i += 1
            continue

        # ── read word token (stops at whitespace, but NOT at ( or ) or " yet —
        #    special look-ahead handles atleast and field:"quoted" tokens)
        j = i
        while j < n and not raw[j].isspace() and raw[j] not in ('"', "(", ")"):
            j += 1
        word = raw[i:j]

        if not word:
            i = j
            continue

        upper = word.upper()

        # ── AND / OR / NOT keywords
        if upper == "AND":
            tokens.append(Token(TK_AND, "AND"))
            i = j
            continue
        if upper == "OR":
            tokens.append(Token(TK_OR, "OR"))
            i = j
            continue
        if upper == "NOT":
            tokens.append(Token(TK_NOT, "NOT"))
            i = j
            continue

        # ── /S  /P  (same-sentence / same-paragraph)
        if _SENT_PARA.match(word):
            sp = word.upper().lstrip("/")
            tokens.append(Token(TK_PROX, word.upper(), op_type=sp, distance=None))
            i = j
            continue

        # ── W/n  NEAR/n  PRE/n
        pm = _PROX_RE.match(word)
        if pm:
            op_type  = pm.group(1).upper()
            distance = int(pm.group(2))
            tokens.append(Token(TK_PROX, word.upper(), op_type=op_type, distance=distance))
            i = j
            continue

        # ── atleast3(term)  — lexer stopped before '(' so word = "atleast3"
        #    look ahead: if next char is '(' read the full atleast3(...) token
        if word.lower().startswith("atleast") and j < n and raw[j] == "(":
            # Consume everything up to and including the matching ')'
            depth = 0
            k = j
            while k < n:
                if raw[k] == "(":
                    depth += 1
                elif raw[k] == ")":
                    depth -= 1
                    if depth == 0:
                        k += 1   # include the closing )
                        break
                k += 1
            full_token = raw[i:k]
            am = _ATLEAST_RE.match(full_token)
            if am:
                tokens.append(Token(TK_TERM, full_token.lower()))
                i = k
                continue
            # Not a valid atleast — fall through and emit word as plain term
            i = j

        # ── field:"quoted value"  — lexer stopped before '"' so word = "court:"
        #    look ahead: if word ends with ':' and next char is '"', read quoted value
        elif word.endswith(":") and j < n and raw[j] == '"':
            # Read quoted phrase
            k = j + 1
            while k < n and raw[k] != '"':
                k += 1
            # k points to closing quote (or end of string)
            full_token = raw[i : k + 1]   # e.g.  court:"Supreme Court"
            tokens.append(Token(TK_TERM, full_token))
            i = k + 1
            continue

        else:
            i = j

        # ── Everything else → TERM (may contain field:value or wildcard suffix)
        tokens.append(Token(TK_TERM, word))

    tokens.append(Token(TK_EOF, ""))
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# Parser (recursive-descent, operator precedence)
# ─────────────────────────────────────────────────────────────────────────────

class BooleanParser:
    """
    Recursive-descent parser implementing operator precedence:
      parse_expr   → OR  (lowest)
      parse_and    → AND
      parse_prox   → W/n NEAR/n PRE/n /S /P
      parse_not    → NOT (unary)
      parse_primary→ term / phrase / ( expr )
    """

    def __init__(self, tokens: list[Token]):
        self._tokens = tokens
        self._pos    = 0

    # ── Token navigation ──────────────────────────────────────────────────

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, ttype: str) -> Token:
        tok = self._advance()
        if tok.type != ttype:
            raise ParseError(
                f"Expected {ttype} but got {tok.type} ({tok.value!r})"
            )
        return tok

    # ── Grammar rules ─────────────────────────────────────────────────────

    def parse(self) -> Any:
        """Entry point."""
        node = self._parse_expr()
        if self._peek().type != TK_EOF:
            raise ParseError(
                f"Unexpected token: {self._peek().value!r}"
            )
        return node

    def _parse_expr(self) -> Any:
        """OR — lowest precedence."""
        left = self._parse_and()

        while self._peek().type == TK_OR:
            self._advance()
            right = self._parse_and()
            left  = OrNode(left, right)

        return left

    def _parse_and(self) -> Any:
        """AND — explicit AND keyword, or implicit AND between adjacent terms."""
        left = self._parse_prox()

        while True:
            tok = self._peek()

            # Explicit AND
            if tok.type == TK_AND:
                self._advance()
                # AND NOT → treat the NOT as unary on the right
                right = self._parse_prox()
                left  = AndNode(left, right)

            # Implicit AND: two terms/phrases adjacent with no operator
            # (only if next token could start a primary and isn't OR/EOF/RPAREN)
            elif tok.type in (TK_TERM, TK_PHRASE, TK_NOT, TK_LPAREN):
                right = self._parse_prox()
                left  = AndNode(left, right)

            else:
                break

        return left

    def _parse_prox(self) -> Any:
        """Proximity operators: W/n  NEAR/n  PRE/n  /S  /P."""
        left = self._parse_not()

        while self._peek().type == TK_PROX:
            prox_tok = self._advance()
            right    = self._parse_not()
            left     = ProximityNode(
                left     = left,
                right    = right,
                op_type  = prox_tok.op_type,
                distance = prox_tok.distance,
            )

        return left

    def _parse_not(self) -> Any:
        """Unary NOT — right-binding."""
        if self._peek().type == TK_NOT:
            self._advance()
            operand = self._parse_not()   # right-associative
            return NotNode(operand)
        return self._parse_primary()

    def _parse_primary(self) -> Any:
        """Leaf nodes: term, phrase, (group)."""
        tok = self._peek()

        # ── Grouped expression
        if tok.type == TK_LPAREN:
            self._advance()
            node = self._parse_expr()
            self._expect(TK_RPAREN)
            return node

        # ── Quoted phrase
        if tok.type == TK_PHRASE:
            self._advance()
            return PhraseNode(tok.value)

        # ── Term (keyword / wildcard / field:value / atleast)
        if tok.type == TK_TERM:
            self._advance()
            return _classify_term(tok.value)

        raise ParseError(
            f"Expected a search term, phrase, or '(' but got {tok.type!r} ({tok.value!r})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Term classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_term(raw: str) -> Any:
    """
    Given a raw term token string, return the appropriate AST node:
      - atleast3(negligence) → AtleastNode
      - court:"Supreme Court" or court:SCC → FieldNode(...)
      - constitu* / wom?n / contract! → WildcardNode
      - anything else → TermNode
    """
    # ── atleast
    am = _ATLEAST_RE.match(raw)
    if am:
        return AtleastNode(n=int(am.group(1)), term=am.group(2).strip().lower())

    # ── field:value  (e.g.  court:"Supreme Court"  or  judge:Chandrachud)
    fm = _FIELD_RE.match(raw)
    if fm:
        field_raw = fm.group(1).lower()
        value_raw = fm.group(2)

        canonical = _FIELD_ALIASES.get(field_raw, field_raw)

        # value might be empty if it's a split token like  judge: "name"
        # in that case return a FieldNode with TermNode("") — executor will handle
        if not value_raw:
            return FieldNode(field_name=canonical, operand=TermNode(""))

        # value might itself be quoted
        if value_raw.startswith('"') and value_raw.endswith('"'):
            inner = value_raw[1:-1].strip().lower()
            return FieldNode(field_name=canonical, operand=PhraseNode(inner))

        # value might have wildcard
        wm = _WILDCARD_RE.match(value_raw)
        if wm:
            return FieldNode(
                field_name=canonical,
                operand=WildcardNode(root=wm.group(1).lower(), wc=wm.group(2))
            )

        return FieldNode(field_name=canonical, operand=TermNode(value_raw.lower()))

    # ── wildcard: * or ! at end  (constitu*  contract!)
    wm = _WILDCARD_RE.match(raw)
    if wm:
        return WildcardNode(root=wm.group(1).lower(), wc=wm.group(2))

    # ── wildcard: ? anywhere in token  (wom?n  judg?ment)
    mw = _MIDWILD_RE.match(raw)
    if mw:
        # root = everything before the first ?; executor uses LIKE pattern
        return WildcardNode(root=mw.group(1).lower(), wc="?")

    # ── plain term
    return TermNode(raw.lower())


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class ParseError(Exception):
    """Raised when the query cannot be parsed into a valid AST."""


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_boolean_query(query: str) -> Any:
    """
    Parse a validated Boolean query string into an AST.

    Args:
        query: Raw query string (should already be validated by validator.py)

    Returns:
        Root AST node (AndNode | OrNode | NotNode | ProximityNode |
                       TermNode | PhraseNode | WildcardNode |
                       AtleastNode | FieldNode)

    Raises:
        ParseError: If the query cannot be parsed (should not happen
                    if validator passed — but belt-and-suspenders)

    Example:
        ast = parse_boolean_query('"natural justice" AND article:21')
        # → AndNode(
        #       PhraseNode('natural justice'),
        #       FieldNode('constitutional_articles', TermNode('21'))
        #   )
    """
    tokens = _lex(query)
    parser = BooleanParser(tokens)
    return parser.parse()


def ast_to_dict(node: Any) -> dict:
    """
    Convert AST to a JSON-serialisable dict.
    Useful for the /boolean/parse debug endpoint and frontend query tree display.
    """
    if isinstance(node, TermNode):
        return {"type": "term", "value": node.value}
    if isinstance(node, PhraseNode):
        return {"type": "phrase", "value": node.value}
    if isinstance(node, WildcardNode):
        return {"type": "wildcard", "root": node.root, "wc": node.wc}
    if isinstance(node, AtleastNode):
        return {"type": "atleast", "n": node.n, "term": node.term}
    if isinstance(node, FieldNode):
        return {"type": "field", "field": node.field_name, "operand": ast_to_dict(node.operand)}
    if isinstance(node, NotNode):
        return {"type": "NOT", "operand": ast_to_dict(node.operand)}
    if isinstance(node, AndNode):
        return {"type": "AND", "left": ast_to_dict(node.left), "right": ast_to_dict(node.right)}
    if isinstance(node, OrNode):
        return {"type": "OR", "left": ast_to_dict(node.left), "right": ast_to_dict(node.right)}
    if isinstance(node, ProximityNode):
        d = node.distance
        return {
            "type":     "proximity",
            "op":       node.op_type,
            "distance": d,
            "left":     ast_to_dict(node.left),
            "right":    ast_to_dict(node.right),
        }
    return {"type": "unknown", "repr": repr(node)}
