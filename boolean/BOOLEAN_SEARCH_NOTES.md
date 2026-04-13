# Legal Boolean Search Tool — Complete Planning Notes
## Inspired by SCC Online & Manupatra

---

## 1. WHAT IS LEGAL BOOLEAN SEARCH?

Boolean search in legal databases (SCC Online, Manupatra, Westlaw, LexisNexis) allows:
- Combining keywords with operators to build precise queries
- Retrieving cases, statutes, judgments with surgical accuracy
- Avoiding irrelevant results from simple keyword search

---

## 2. BOOLEAN OPERATORS REQUIRED

### Standard Operators
| Operator | Symbol/Keyword | Meaning | Example |
|----------|----------------|---------|---------|
| AND | AND / & | Both terms must appear | murder AND intention |
| OR | OR / \| | Either term appears | murder OR culpable homicide |
| NOT | NOT / AND NOT | Exclude term | murder NOT attempt |
| NEAR/n | W/n, NEAR/n | Within n words of each other | "police" W/5 "custodial" |
| PHRASE | "..." | Exact phrase | "natural justice" |
| WITHIN SAME SENTENCE | /S | Both words in same sentence | negligence /S duty |
| WITHIN SAME PARAGRAPH | /P | Both words in same paragraph | negligence /P breach |
| WILDCARD (root) | * | Zero or more characters at end | constitu* → constitution, constitutional |
| WILDCARD (single) | ? | Exactly one character | wom?n → woman, women |
| WILDCARD (universal) | ! | Any number of characters (Manupatra style) | contract! |
| GROUPING | ( ) | Control operator precedence | (murder OR culpable) AND intention |
| ATLEAST | atleast5(term) | Term appears at least n times | atleast3(negligence) |

### Proximity Operators (Critical for legal)
- `W/n` — within n words, in order (e.g., "arrested W/3 without")
- `PRE/n` — first term precedes second within n words
- `NEAR/n` — within n words, any order
- `/S` — same sentence
- `/P` — same paragraph

---

## 3. SEARCH FIELDS / FILTERS (like SCC/Manupatra)

### Metadata Filters
- **Court**: Supreme Court, High Courts (state-wise), Tribunals, District Courts
- **Date Range**: From — To (dd/mm/yyyy)
- **Judge/Bench**: Name of judge(s)
- **Party Name**: Petitioner / Respondent
- **Case Number**: Civil Appeal No., Criminal Appeal No., Writ Petition, etc.
- **Citation**: AIR, SCC, SCR, etc.
- **Act/Legislation**: Search within specific Act
- **Section Number**: Specific section of an Act
- **Advocate**: Name of counsel appearing
- **Coram**: Number of judges (3-judge bench, Constitution bench, etc.)

### Document Type Filters
- Judgments
- Orders
- Notifications
- Statutes/Acts
- Rules & Regulations
- Articles / Headnotes

### Search Scope
- Full Text
- Headnote Only
- Catchwords
- Ratio Decidendi
- Cited Cases Only

---

## 4. QUERY BUILDER — UI COMPONENTS

### Visual Query Builder (like Manupatra's guided mode)
- Row-based builder: [Field ▼] [Operator ▼] [Value input]
- Add Row / Remove Row buttons
- Combine rows with AND/OR connector between rows
- Drag to reorder rows

### Raw Boolean Input (like SCC Online's expert mode)
- Large textarea for direct boolean string entry
- Syntax highlighting (operators colored differently)
- Real-time query validation
- Query parser showing tree structure
- Error highlighting with inline suggestions

### Modes Toggle
- **Guided Mode** (form-based) → auto-builds boolean string
- **Expert Mode** (raw boolean string)
- **Natural Language Mode** → converts plain English to boolean (AI-assisted)

---

## 5. SEARCH RESULT DISPLAY

### Result Card Must Show
- Case name (petitioner v. respondent) — bold
- Citation (AIR 1973 SC 1461 format)
- Court name
- Date of judgment
- Coram (bench strength & judge names)
- Headnote / Short summary
- Keyword hit count + KWIC (KeyWord In Context) snippet
- Boolean query terms highlighted in snippets
- Relevance score (percentage)
- Options: Full Text | Cited Cases | Citing Cases | Download | Save | Share

### Sorting Options
- Relevance (default)
- Date (newest first / oldest first)
- Court hierarchy
- Citation count

### Pagination
- Results per page: 10 / 25 / 50
- Page navigation with ellipsis
- Total count shown ("1,243 results")

---

## 6. QUERY VALIDATION ENGINE (JavaScript)

### Must Validate:
- Unmatched parentheses → error highlight
- Operator at start/end → warning
- Two consecutive operators → error
- Empty parentheses `()` → error
- Wildcard at start `*term` → error (invalid in most legal DBs)
- Proximity number missing e.g., `W/` → error
- Quoted phrases missing closing quote → error
- `NOT` without preceding term → warning
- Reserved words used as search terms → warning

### Auto-suggestions:
- When typing, suggest operators
- Suggest field qualifiers: `title:`, `court:`, `judge:`, `section:`
- Suggest Acts/Statutes from a predefined list

---

## 7. QUERY SYNTAX HIGHLIGHTING

Color scheme for raw boolean input box:
- **Operators** (AND, OR, NOT, W/n, NEAR): colored (e.g., blue/orange)
- **Quoted phrases**: green
- **Wildcards** (*, ?, !): purple
- **Field qualifiers** (court:, judge:): teal
- **Parentheses**: yellow/gold
- **Regular terms**: white/default
- **Error tokens**: red underline

---

## 8. ADVANCED FEATURES

### Search History
- Last 20 searches stored (localStorage)
- Ability to re-run or edit past searches
- Timestamp shown

### Saved Searches
- Save with custom name
- Run saved searches later
- Export search string

### Query Preview Panel
- Shows parsed tree of boolean query
- Visual AND/OR/NOT tree diagram
- Helps user understand query logic

### Boolean String Auto-Generator
- User fills filters (guided mode) → system generates boolean string
- Shows generated string so user learns syntax

### Export Options
- Export results as PDF / Excel / CSV
- Export with citations
- Generate bibliography

### Clipboard
- Copy query string
- Copy citation list

---

## 9. KEYBOARD SHORTCUTS

| Shortcut | Action |
|----------|--------|
| Ctrl+Enter | Run search |
| Ctrl+Z | Undo in query box |
| Ctrl+S | Save search |
| Ctrl+H | Show history |
| Esc | Clear query |
| Tab | Autocomplete operator suggestion |

---

## 10. UI LAYOUT STRUCTURE

```
┌─────────────────────────────────────────────────────────────┐
│ HEADER: Logo | Database selector | My Account | Help        │
├─────────────────────────────────────────────────────────────┤
│ SEARCH BAR AREA                                             │
│  ┌─────────────────────────────────┐  [Guided|Expert|NL]   │
│  │ Boolean query input / builder   │  [Search Button]      │
│  └─────────────────────────────────┘                       │
│  Operator buttons: [AND] [OR] [NOT] [W/n] ["…"] [*] [()] │
├──────────────┬──────────────────────────────────────────────┤
│ FILTERS      │ RESULTS PANEL                               │
│ (left panel) │                                             │
│ - Court      │  Showing 1,243 results | Sort: [Relevance▼] │
│ - Date range │  ┌──────────────────────────────────────┐   │
│ - Judge      │  │ Case Card 1                         │   │
│ - Act        │  │ Case Card 2                         │   │
│ - Section    │  │ ...                                 │   │
│ - Doc type   │  └──────────────────────────────────────┘   │
│              │  Pagination: < 1 2 3 ... 50 >               │
└──────────────┴──────────────────────────────────────────────┘
```

---

## 11. SAMPLE QUERIES TO DEMO

```
"natural justice" AND "audi alteram partem" AND court:"Supreme Court"
(murder OR "culpable homicide") AND intention AND NOT attempt
"arrest" W/5 "without warrant" AND section:"section 41 CrPC"
constitu* AND "article 21" AND date:[2010-01-01 TO 2023-12-31]
atleast3(negligence) AND "duty of care" AND judge:"Y.V. Chandrachud"
("right to privacy" OR "right to life") AND NOT dismissed
```

---

## 12. MOCK DATA STRUCTURE

```javascript
const mockResults = [
  {
    id: "SC2017001",
    title: "Justice K.S. Puttaswamy (Retd.) v. Union of India",
    citation: "(2017) 10 SCC 1",
    court: "Supreme Court of India",
    date: "24-08-2017",
    bench: "9-Judge Constitution Bench",
    judges: ["J.S. Khehar CJI", "J. Chelameswar J.", "S.A. Bobde J.", ...],
    headnote: "Right to Privacy — Held to be a fundamental right under Article 21...",
    keywords: ["privacy", "fundamental right", "article 21", "dignity"],
    relevanceScore: 98,
    citedBy: 2341,
    snippet: "...the right to privacy is protected as an intrinsic part of the right to life...",
    actsReferred: ["Constitution of India - Art. 21"],
    overruled: false,
  },
  // more...
]
```

---

## 13. OPERATOR INSERTION BUTTONS

Quick-insert buttons shown below query box:
- `AND` — insert AND
- `OR` — insert OR  
- `NOT` — insert NOT
- `W/5` — proximity (prompt for n)
- `" "` — wrap selection in quotes
- `( )` — wrap selection in parentheses
- `*` — append wildcard
- `?` — single char wildcard
- `atleast` — insert atleast template

---

## 14. RESPONSIVENESS

- Desktop first (legal professionals use desktop)
- Tablet: filter panel collapses to drawer
- Mobile: simplified single-column view
- Print stylesheet for results

---

## 15. ACCESSIBILITY

- ARIA labels on all inputs
- Keyboard navigable results
- High contrast mode
- Screen reader announcements for result count

---

## 16. POTENTIAL BUGS TO PREVENT

1. **Wildcard at position 0** — must block `*term` but allow `term*`
2. **Unclosed quotes** — parser must flag and refuse to run
3. **Nested parentheses depth** — limit to 10 levels, warn beyond 5
4. **Empty search** — disable search button if query is empty
5. **Whitespace-only query** — treat as empty
6. **Case sensitivity** — operators (AND/OR/NOT) must be case-insensitive in input but displayed UPPERCASE
7. **Field qualifier typos** — suggest closest match (court: vs cours:)
8. **Date range validation** — from date must be ≤ to date
9. **Proximity operator** — n must be positive integer, max 100
10. **Special characters** — escape or reject chars like <, >, {, }
11. **XSS in query display** — sanitize before rendering highlighted HTML
12. **Infinite scroll vs pagination** — use pagination with explicit page numbers (legal standard)
13. **Debounce** on autocomplete — 300ms to avoid flickering
14. **Result highlight** — highlight all instances of search terms in snippet, not just first

---

## 17. TECHNOLOGIES TO USE

- **HTML + CSS + Vanilla JS** (single file artifact)
- **No external libraries** except Google Fonts
- **Custom CSS** for syntax highlighting in textarea (overlay technique)
- **CSS Grid + Flexbox** for layout
- **localStorage** for history and saved searches
- Monospace font for query input (Fira Code or JetBrains Mono via Google Fonts)

---

## 18. AESTHETIC DIRECTION

- **Theme**: Deep navy/dark blue — like a serious legal research database
- **Typography**: 
  - Display: "Playfair Display" (serif, authoritative, legal feel)
  - UI: "IBM Plex Sans" (clean, professional)
  - Code/Query: "JetBrains Mono" (for boolean input)
- **Colors**:
  - Background: #0a0f1e (deep navy)
  - Surface: #111827
  - Accent: #c9a84c (legal gold — like law book spines)
  - Operator highlight: #4dabf7 (bright blue)
  - Phrase highlight: #69db7c (green)
  - Error: #ff6b6b (red)
  - Text: #e8e8e8
- **Feel**: Premium, authoritative, like SCC Online's professional interface

---

## CHECKLIST BEFORE FINALIZING

- [ ] All boolean operators work correctly in parser
- [ ] Parentheses matching/highlighting works
- [ ] Wildcard validation (no leading wildcard)
- [ ] Proximity W/n operator parses n correctly  
- [ ] Guided mode generates correct boolean string
- [ ] Expert mode validates syntax in real-time
- [ ] Error messages are specific and actionable
- [ ] All filter dropdowns have real legal options
- [ ] Mock search returns results and highlights query terms
- [ ] Search history saves and loads correctly
- [ ] Sort and filter work on mock results
- [ ] Operator insertion buttons insert at cursor position
- [ ] Keyboard shortcut Ctrl+Enter triggers search
- [ ] Pagination works correctly
- [ ] Copy query to clipboard works
- [ ] No console errors on load or interaction
- [ ] Layout doesn't break on resize
- [ ] Empty/invalid state UI shown properly
