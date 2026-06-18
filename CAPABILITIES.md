# lit-agent — Capabilities Spec

**Audience:** the engineer building lit-agent. **Status:** design intent + build
direction, written 2026-06-18. Read alongside `CLAUDE.md` (hard constraints, project
structure) — this doc does not restate those, it defines *what the agent can do* and
*what to build next*. Where this conflicts with `CLAUDE.md`, `CLAUDE.md` wins.

---

## 0. The one correction that resolves most of the confusion

lit-agent has **no "datasets"** in the sense of the DecoupleRpy project's 16 GEO/TCGA
manifests. Its raw material is the **live literature**, which is effectively unlimited
(~10,600 PDAC papers/yr; Europe PMC indexes 40M+ records total). So "we only have a few
datasets" and "the metrics are starving for data" are the wrong diagnosis. Nothing here
is data-starved at the *source*.

The real issues are three, and they live on three different axes. Almost every
"capabilities feel thin" complaint is actually a statement about one of these axes being
underbuilt — not about the others. **Define the agent on all three axes separately and
the confusion dissolves:**

| Axis | Question it answers | Today |
|---|---|---|
| **1. Data held** | What do we store and query over? | Counts time-series (full 5yr) + a 138-paper curated corpus (1 week) |
| **2. Signals computed** | What do we tell the reader is *worth knowing*? | Volume / share-of-voice + keyword movers only |
| **3. Surfaces** | Where does it show up? | Dry-run digest + cached analytics; Q&A + per-area tabs planned |

The rest of this doc is organized by those three axes, then granularity (a sub-problem of
Axis 2), then effort/sequencing.

---

## 1. Axis 1 — Data held

### 1.1 Two tiers, deliberately separated

lit-agent should hold **two** bodies of data with different cost profiles. Today only the
second exists as records; the first exists only as aggregate counts.

| Tier | Contents | Powers | Cost | Status |
|---|---|---|---|---|
| **Census** | *Every* PDAC paper, metadata + abstract + embedding only (no full text) | Trends, novelty/emergence, the macro→micro bridge, coverage QA | Small (see §1.3) | **Not built** — only hit*counts* exist |
| **Curated corpus** | The scored subset; OA full text where available, chunked, LLM-classified | Weekly digest, grounded Q&A | Higher (full text + LLM) | Built (`papers` table, 138 rows) |

This mirrors the split already in the codebase — `coverage_counts`/`keyword_counts`
(the count tier) vs the `papers` table (the corpus) — but makes the census a deliberate,
*complete* body of records instead of an incidental by-product of one harvest week.

### 1.2 What exists today vs. what's new (be precise about this)

- `store/db.py` already defines a `papers` table with **every field a census needs**
  (doi, title, abstract, ids, authors, mesh, annotations, focus_areas, embedding_id,
  first_seen_date, …). The schema is not the gap.
- `pipeline/backfill.py` currently populates **only** `coverage_counts` (weekly per-area
  hitCounts) and `keyword_counts` (quarterly tracked-term hitCounts). Its own docstring
  says it right: *"COUNTS ONLY — no records, no embeddings, no LLM."*
- So the census = **run the harvest path in backfill mode to populate `papers` for all
  ~53k PDAC papers**, not a new schema or store. It reuses `harvest.py` →
  `normalize.py` → `score.py` (embed) → `store.db.upsert_papers` end-to-end.

### 1.3 Census sizing (so "incorporate all 10,000 papers" stops feeling heavy)

10,600/yr × 5 yr ≈ **53,000 papers**.

- Metadata + abstract ≈ ~3 KB/record → ~160 MB text; in SQLite with JSON columns +
  indexes call it **~250–400 MB**. Trivial; commits to the HF Dataset persistence store
  you already use.
- Embeddings: BGE-small is 384-dim float32 = 1.5 KB/vector → 53k ≈ **~80 MB**
  (`store/vectors.py` / `vectors.npz` already handles this).
- **Classification cost is avoidable for the census.** Tag census papers by the existing
  `count_query` booleans or nearest-area embedding similarity (no LLM). Reserve the
  LLM confirm step (`classification.candidate_top_k`) for the curated digest subset only.
  So the census adds **~0 LLM cost**.

Harvest itself: Europe PMC `search` with `cursorMark`, `pageSize=1000`,
`resultType=core` → ~53 pages → minutes of wall-clock at fair-use pacing. Embedding 53k
abstracts locally is tens of minutes on CPU. **Engineering effort ≈ 1–2 days**: add a
date-range backfill mode to `harvest.py` that writes records (not counts), reusing the
existing normalize/score/upsert path; make it resumable like the count backfill already is.

### 1.4 Coverage — "how do we know we have all the PDAC papers?" (answer to Q1)

Two things are bundled here; only one is about Europe PMC.

**EPMC holdings are not the bottleneck.** Europe PMC is a *superset* of PubMed/MEDLINE
(mirrored daily) plus 31 preprint servers, PMC full text, Agricola, and patents. In
biomedical coverage studies the open aggregators sit at ~98% recall of relevant papers
(OpenAlex 98.6%, Semantic Scholar 98.3%) vs PubMed ~93%, with ~90% findable in *all* of
them. EPMC is in that top tier and additionally over-indexes European-funded work. Fine
as the primary.

**The query is the real recall ceiling:** `coverage = database completeness × query
recall`. A paper EPMC holds is invisible if `config/sources.yaml`'s query doesn't match
it — and that second term is where papers are actually lost. So make coverage a
**measured** property, not an assumption. Build a small coverage harness (`eval/` or
`scripts/`) that:

1. **Triangulates.** Run the same PDAC query against EPMC and against **OpenAlex** (free
   API, returns DOIs). Diff DOI sets; report overlap and what's unique to each. A
   non-trivial OpenAlex-only set means EPMC *or* (more likely) the query is leaking.
2. **Tests query recall against a gold set.** The Sears/Brody lab's own PDAC pubs + their
   reference lists, or the last N papers from a key journal. Confirm the query retrieves
   them; misses tell you which synonyms/MeSH terms to add.
3. **Records the number, with its denominator.** Persist the measured recall so the
   provenance disclaimer (§3.5) can cite a real, self-measured figure — and so coverage
   regressions are visible when the query changes.

OpenAlex doubles as the citation/baseline backstop for Axis 2 (it has the most complete
open citation graph), so wiring it in here pays off twice.

---

## 2. Axis 2 — Signals computed (volume → signal)

This is where "thin" is *correct*. The honest framing: the share-of-voice fix made the
volume metrics **accurate**, but volume has a **low ceiling of importance** for this
audience. Across 10k papers/yr, aggregates move 1–4 pp/yr and tell a PI the field is
busy — not that anything happened they should act on. The things a PI reacts to (a KRAS
degrader that works, a biomarker that validates) are **individual papers**. So keep the
volume metrics, demote them, and add signal-class metrics.

### 2.1 Keep (built) — demote to context

- **Share of voice + per-area sparkline** (`analytics.footer_html`) — lag-robust,
  correct; good as a quiet footer, not a headline.
- **Keyword movers** (`analytics.keyword_movers_html`) — already the best existing
  signal *and already links each term to the Europe PMC papers behind it*
  (`_epmc_link`). This is the template for everything else: a number that lands you on
  papers.

### 2.2 Build — the high-value signals

- **Novelty / emergence.** "New to the PDAC literature" — first appearance of an entity
  or a gene×drug / gene×modality pairing — instead of "more of the same." **The census
  (§1.3) is what makes this work now**: it provides 262 weeks of baseline instantly, so
  novelty is answerable *today* rather than after months of weekly accumulation. This is
  the single strongest argument for doing the census.
- **Relevance to the BCC.** Tie each paper to the lab's signatures (MYC, HuR, KRAS) and
  **seed DOIs**, and use Europe PMC citation links for *"N papers this week cite / build
  on your work."* Note: `config/interest_profile.yaml` `exemplar_dois` are **empty** —
  populating them with Sears/Brody seed papers is the highest-leverage single input, both
  for this signal and for classifier tuning.
- **Translational motion.** It is a Center for *Care*: ClinicalTrials.gov registrations
  and first-in-human signals matter more than publication counts. EPMC tags trial
  references; ClinicalTrials.gov has a clean API. Make this a first-class signal.

### 2.3 Build — the macro→micro bridge (the structural fix)

Today trends come from `coverage_counts` (keyword hitCounts) and the digest body comes
from the `papers` corpus — **they share no IDs**, so "ctDNA is heating up" can't land on
the ctDNA papers in this week's digest. Once trends are computed **over the census
`papers` table** (same objects, same IDs), every macro number links to its papers. This
is a bigger lever than any single new metric — it's what turns two parallel artifacts
into one system. (Keyword movers already prove the pattern by deep-linking to EPMC; the
bridge makes those links resolve to *local, curated* papers.)

---

## 3. Axis 3 — Surfaces

- **Weekly digest** (`pipeline/digest.py`) — per-area sections (already structured for
  the post-v1 per-recipient filtering); stays in dry-run behind `SEND_LIVE`.
- **Cached analytics** (`data/analytics.json`) — the Space renders it; never recomputed
  online (offline-pipeline constraint).
- **Q&A Space** (`app.py` / `ui.py` / `qa/`) — grounded answers with the anti-fabrication
  guard.
- **Per-area tabs** (post-v1) — each focus area gets its papers + coverage + scoped Q&A.
  Keep analytics and retrieval filterable by focus area so this is a UI change, not a
  rewrite (already the roadmap intent).

### 3.5 Provenance & coverage disclaimer (requested)

Add a short data-source line at the **top of the email and the Space UI**. This is good
practice for a scientific audience — but **the number must be honest**, or it will draw
exactly the skeptical question a PI asks ("98% of *what*?").

**The rule for the coder:** do **not** hardcode "98%". That figure comes from a published
study about *OpenAlex's* coverage of *guideline-cited* papers — not this system, not this
query, not EPMC. Use the figure your own coverage harness (§1.4) measures, and state its
denominator. Until that number exists, ship the qualitative version.

Recommended copy:

- **Before a measured number exists (ship this now):**
  > *Sources: Europe PMC (primary; mirrors PubMed/MEDLINE + bioRxiv/medRxiv preprints),
  > with PubMed and bioRxiv/medRxiv as secondary feeds. Open-access full text where
  > available; abstracts otherwise.*

- **After the harness measures recall (swap in the number + denominator):**
  > *Coverage: retrieved from Europe PMC; our PDAC query recovers ~XX% of papers found by
  > a cross-check against OpenAlex (measured {date}). Open-access full text where
  > available; abstracts otherwise.*

Drive both strings from `config/sources.yaml` (e.g. a `provenance:` block carrying the
measured recall, denominator source, and as-of date) so the copy updates without a code
change — consistent with the "config, not code" constraint.

---

## 4. Granularity strategy (answer to Q3)

The diagnosis is right: 8 areas, shares summing to ~278% (avg ~2.8 areas/paper), top-3
each covering most of the field, early-detection at 64% because its `count_query`
includes the bare term **`biomarker`**. That is a *category-definition* problem, fixable
without more data. Treat granularity as a **method with acceptance tests**, not a better
guess at the list:

1. **Target a measurable share distribution.** Define areas so the *median* area holds a
   minority share (~5–20%). **Hard rule: no area exceeds ~40%** of the literature — above
   that it can't discriminate and must be split. This is an automatable test against the
   census.
2. **Drive labels-per-paper toward ~1.3–1.6** (down from 2.8). Build a **co-occurrence
   matrix** over the census; **merge/split any pair co-occurring >~60%** of the time —
   those aren't two areas. Keep multi-label (the roadmap needs it), just stop near-total
   overlap.
3. **Define areas by entities, not concepts.** "ctDNA-based early detection" or "KRAS
   G12 inhibitors" discriminate; "biomarkers" or "stroma" don't. **Concrete first fix:**
   remove bare `biomarker` (and likely bare `screening`/`exosome`) from
   `early_detection_biomarkers.count_query`, or require PDAC-contextual co-occurrence;
   the area's 64% share should fall to something informative, which validates the change.
4. **Two-level taxonomy.** A few broad themes for navigation; narrower sub-areas where the
   *signal* lives. Compute trends/movers at the **sub-area** level. This also matches the
   roadmap: a recipient subscribes at the level they'd actually want in their inbox.
5. **Let the data nominate areas.** Don't hand-pick from intuition alone. Cluster census
   embeddings and tabulate MeSH + EPMC text-mined annotation (gene/disease/chemical)
   frequencies → bottom-up candidate areas grounded in what PDAC literature contains.
   Human curates; then re-run tests 1–2 before committing.
6. **Govern it.** Taxonomy stays in `config/interest_profile.yaml`; each area carries a
   per-area precision check; watch per-area precision as the count grows (the classifier
   is already multi-label, so growth is config, not plumbing).

Note the config already separates the loose `keywords` (embedding/classifier) from the
tight `count_query` (analytics) — good design; the fix is tightening `count_query`
definitions and adding the acceptance tests above, not restructuring.

---

## 5. Effort & sequencing (answer to Q2, made concrete)

Each phase is independently demoable and slots into the existing build order in
`CLAUDE.md`.

| # | Phase | What | Effort | Unlocks |
|---|---|---|---|---|
| A | **Census backfill** | Date-range record mode in `harvest.py` → `papers` for all ~53k (reuse normalize/score/upsert) | ~1–2 days + a minutes-long run | Instant novelty baseline; macro→micro bridge; coverage QA |
| B | **Coverage harness** | EPMC × OpenAlex triangulation + gold-set recall test; persist measured recall | ~1–2 days | The honest disclaimer number (§3.5); query regression guard |
| C | **Granularity pass** | Tighten `count_query`s (start with `biomarker`); add share + co-occurrence acceptance tests; re-derive areas data-first | ~2–3 days | Metrics that discriminate |
| D | **Seed + relevance-to-BCC** | Populate `exemplar_dois`; wire EPMC citations; "cites your work" signal | ~2–3 days | The signal a PI opens the email for |
| E | **Novelty + bridge** | First-appearance / new-pairing detection over census; trends computed over `papers` so numbers link to papers | ~3–4 days | Volume → signal; one coherent system |
| F | **Translational motion** | ClinicalTrials.gov feed; trial/first-in-human signal | ~2–3 days | Care-relevant headline |

Recommended order: **A → B → C → D → E → F**. A unblocks E and feeds C; B produces the
disclaimer number and protects C; D and F are independent value-adds once the data layer
is honest. None of this requires full text for the whole corpus or any new store — the
expensive tier (OA full text + LLM) stays scoped to the curated subset.

---

## 6. First-PR checklist for the coder

- [ ] Add `--records`/date-range mode to `pipeline/backfill.py` (or a new `pipeline/
      census.py`) that populates `papers` for all PDAC papers over N years; resumable;
      tag areas via `count_query`/embedding (no LLM).
- [ ] Confirm `store.db.upsert_papers` + `vectors.py` scale to ~53k (batch commits).
- [ ] Add `scripts/coverage_check.py`: EPMC vs OpenAlex DOI diff + gold-set recall;
      write the measured recall + denominator + date to a `provenance:` block in
      `config/sources.yaml`.
- [ ] Ship the qualitative provenance line in `digest.py` + `ui.py` now; switch to the
      measured-number copy once `coverage_check` has run.
- [ ] Tighten `early_detection_biomarkers.count_query` (drop bare `biomarker`); add a
      test asserting no area > 40% share and avg areas/paper < ~1.8 over the census.
- [ ] Populate `interest_profile.yaml` `exemplar_dois` with Sears/Brody seed papers.

---

*This spec is intentionally organized so future questions resolve to an axis: a "we don't
have enough data" worry is almost always Axis-1 census or Axis-2 signal design, not a
source limitation. Keep the three axes separate in conversation and in the code.*
