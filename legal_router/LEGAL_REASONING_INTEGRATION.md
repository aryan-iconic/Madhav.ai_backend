# Legal Reasoning Router — Integration Notes

## Setup (1 line in main.py)

```python
from Backend.retrieval.legal_reasoning_router import router as reasoning_router
app.include_router(reasoning_router)
```

New cache table created automatically on first request:
```sql
case_reasoning_cache (
    cache_key     TEXT PRIMARY KEY,   -- "counter:{id}", "strategy:{id}:{side}", "factlaw:{id}"
    data_json     JSONB NOT NULL,
    generated_at  TIMESTAMPTZ DEFAULT NOW(),
    model_used    TEXT
)
```

Reads from `case_arguments_cache` (from arguments_router) and `case_brief_cache`.
No schema changes to existing tables.

---

## Endpoint Reference

### GET `/api/cases/{case_id}/reasoning-full`
**Call this first on every case page load.**
Returns everything that's already cached — instant, no LLM.
Tells client exactly which endpoints to POST to for missing data.

```json
{
  "arguments":           {...} or null,
  "counter_arguments":   {...} or null,
  "strategy_petitioner": {...} or null,
  "strategy_respondent": {...} or null,
  "fact_law":            {...} or null,
  "quick_summary":       {...} or null,
  "pending": ["counter_arguments", "fact_law_separation"],
  "all_cached": false
}
```

**Frontend pattern:**
```javascript
const bundle = await fetch(`/api/cases/${id}/reasoning-full`).then(r => r.json());

// Render what's cached immediately
if (bundle.arguments)        renderArgumentsTab(bundle.arguments);
if (bundle.counter_arguments) renderWeaknessTab(bundle.counter_arguments);
if (bundle.fact_law)         renderFactLawTab(bundle.fact_law);

// Queue generation for anything missing
for (const item of bundle.pending) {
  generateInBackground(id, item);  // POST to the relevant endpoint
}
```

---

### POST `/api/cases/{case_id}/counter-arguments`
First call: ~60-90s. Every subsequent call: instant from cache.
Optional: `?force_regenerate=true`

**Response:**
```json
{
  "petitioner_weaknesses": [
    {
      "argument": "Arrest was without valid warrant",
      "weakness": "DK Basu guidelines allow arrest without warrant for cognisable offences under Section 41 CrPC",
      "counter": "Respondent should cite Section 41(1)(ba) CrPC — reasonable complaint of cognisable offence sufficient",
      "severity": "serious"
    }
  ],
  "respondent_weaknesses": [
    {
      "argument": "Detention was within permissible limit",
      "weakness": "Para 14 of the judgment records detention exceeded 24 hours without magistrate production",
      "counter": "Petitioner should press on Article 22(2) — the constitutional mandate is absolute, not directory",
      "severity": "fatal"
    }
  ],
  "overall_assessment": {
    "stronger_side": "petitioner",
    "decisive_issue": "Whether the 24-hour rule under Article 22(2) was breached",
    "swing_factor": "If the FIR shows a cognisable offence, the respondent's position strengthens significantly"
  },
  "cached": false,
  "done": true
}
```

---

### POST `/api/cases/{case_id}/strategy`
Body: `{ "side": "petitioner" }` or `{ "side": "respondent" }`
First call: ~60-90s. Cached permanently per side.

**Response:**
```json
{
  "side": "petitioner",
  "win_probability": "medium",
  "win_probability_reason": "Strong constitutional violation but procedural gaps may hurt",
  "primary_strategy": "Lead with the constitutional violation under Article 22(2). Frame as non-derogable fundamental right — no exception applies.",
  "strongest_arguments": [
    {
      "argument": "Article 22(2) violation — absolute right, no derogation",
      "why_strong": "Court in DK Basu held this is a non-negotiable safeguard",
      "how_to_present": "Open with the constitutional text, then cite DK Basu para 34 directly",
      "supporting_law": "Article 22(2) Constitution + DK Basu v. State of WB (1997)"
    }
  ],
  "arguments_to_avoid": [
    {
      "argument": "Malicious prosecution claim",
      "reason": "No evidence in record — making it weakens credibility on the stronger points"
    }
  ],
  "how_to_counter_opposition": [
    {
      "their_point": "Arrest was for cognisable offence under Section 41 CrPC",
      "your_response": "Section 41 permits arrest but does not suspend Article 22(2). Production to magistrate is constitutionally mandatory regardless of offence type."
    }
  ],
  "evidence_to_establish": [
    "Timestamp of arrest from FIR and station diary",
    "Timestamp of first magistrate production — establish the gap exceeds 24 hours"
  ],
  "reliefs_to_claim": [
    "Declaration that detention violated Article 22(2)",
    "Compensation under Nilabati Behera — constitutional tort remedy"
  ],
  "risk_factors": [
    {
      "risk": "Court may treat as infructuous if petitioner already released",
      "mitigation": "Press for compensation remedy — mootness does not extinguish constitutional tort claim"
    }
  ],
  "alternative_routes": [
    "If habeas corpus fails — file Section 482 CrPC application for quashing of arrest",
    "File complaint before NHRC under Section 12(a) Protection of Human Rights Act"
  ],
  "cached": false,
  "done": true
}
```

---

### POST `/api/cases/{case_id}/fact-law-separation`
First call: ~60-90s. Cached permanently.

**Response:**
```json
{
  "classifications": [
    {
      "para_number": 3,
      "type": "fact",
      "sub_type": "finding_of_fact",
      "summary": "Petitioner arrested on 14 March without warrant",
      "burden_of_proof": {
        "present": false,
        "party": null,
        "on_issue": null
      }
    },
    {
      "para_number": 12,
      "type": "law",
      "sub_type": "question_of_law",
      "summary": "Whether Article 22(2) admits any exception",
      "burden_of_proof": {
        "present": true,
        "party": "respondent",
        "on_issue": "Respondent must justify deviation from 24-hour rule"
      }
    },
    {
      "para_number": 18,
      "type": "ratio",
      "sub_type": "ratio_decidendi",
      "summary": "Article 22(2) is absolute — no exception for cognisable offences",
      "burden_of_proof": { "present": false, "party": null, "on_issue": null }
    }
  ],
  "fact_law_summary": {
    "key_facts_established": [
      "Arrest on 14 March without warrant — Para 3",
      "Detention for 38 hours without magistrate production — Para 7"
    ],
    "key_legal_questions": [
      "Whether Article 22(2) admits exception for cognisable offences — Para 12"
    ],
    "burden_summary": "Respondent bears burden to justify the detention. Petitioner only needs to establish the fact of detention.",
    "contested_facts": [
      "Whether the FIR was registered before or after arrest"
    ]
  },
  "cached": false,
  "done": true
}
```

---

## For the Frontend Person

### Case Viewer — new tabs / sections

**"Weaknesses" tab** (counter-arguments):
Show two columns: Petitioner Weaknesses | Respondent Weaknesses.
Each weakness card has: argument heading, weakness text, counter-argument, severity badge (🔴 Fatal / 🟠 Serious / 🟡 Minor).
Below: Overall Assessment box — stronger side, decisive issue, swing factor.

**"Strategy" tab**:
Show a side selector: `[For Petitioner]  [For Respondent]`
On select: win probability pill (🟢 High / 🟡 Medium / 🔴 Low) + reason.
Then sections: Primary Strategy → Strongest Arguments → Arguments to Avoid → How to Counter → Evidence Needed → Reliefs → Risks → Alternatives.
Each section is a collapsible accordion.

**"Fact / Law" tab**:
Show paragraph list with type badges: `[FACT]` `[LAW]` `[RATIO]` `[MIXED]` `[ORDER]`.
Colour-code: FACT = blue, LAW = purple, RATIO = gold, MIXED = teal, ORDER = grey.
Below paragraph list: summary boxes for Key Facts | Key Legal Questions | Burden of Proof | Contested Facts.
Paragraphs with `burden_of_proof.present = true` get a ⚖️ icon.

**Loading pattern** (critical for UX):
```
1. On case page load → GET /api/cases/{id}/reasoning-full  (instant)
2. For each item in response.pending → show skeleton/spinner in that tab
3. POST to generate in sequence: arguments → counter → strategy → fact-law
4. Each completes → update the tab live
```
Never block the page waiting for generation. Everything loads progressively.

---

## Generation Time Estimates (llama3.1:8b on local Ollama)

| Endpoint | First call | Cached |
|----------|-----------|--------|
| `/arguments` | 60-90s | instant |
| `/counter-arguments` | 50-70s | instant |
| `/strategy` (per side) | 60-80s | instant |
| `/fact-law-separation` | 70-90s | instant |
| `/reasoning-full` (GET) | instant | instant |

**Recommendation:** Pre-generate for top 500 most-searched cases during off-peak hours.
Add a background job that calls POST on popular cases after indexing.
Once cached, the entire Legal Reasoning Engine is instant for those cases.
