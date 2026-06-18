# lit-agent — Implementation Plan

**Derived from:** [`CAPABILITIES.md`](CAPABILITIES.md) (design intent) — grounded against the
actual code on 2026-06-18. **Authority:** where this conflicts with
[`CLAUDE.md`](CLAUDE.md) (hard constraints) or `CAPABILITIES.md`, those win; this doc is the
*sequenced, code-aware build plan* under them.

**State at time of writing:** 138-paper curated corpus (`papers`), 262 weeks of coverage
counts (`coverage_counts`), 792 quarterly keyword counts (`keyword_counts`), 7 of 8 focus
areas tagged. Phases 0–4 of the `CLAUDE.md` build order are done; 5–6 scaffolded.

---

## Anchor: three independent axes (CAPABILITIES.md §0)

Keep these separate in code and in conversation — almost every "capabilities feel thin"
worry is one axis being underbuilt, not a data-source limit:

| Axis | Question | Today |
|---|---|---|
| **1. Data held** | What do we store/query over? | Counts time-series (5 yr) + 138-paper corpus (1 wk) |
| **2. Signals computed** | What's *worth knowing*? | Volume / share-of-voice + keyword movers |
| **3. Surfaces** | Where does it show up? | Dry-run digest + cached analytics; Q&A + tabs scaffolded |

The literature is effectively unlimited (~10,600 PDAC papers/yr; EPMC indexes 40M+). Nothing
is data-starved *at the source* — the gaps are the census (axis 1) and signal design (axis 2).

---

## Do-now quick wins (no dependencies; hours, not days)

1. **Qualitative provenance line** (CAPABILITIES.md §3.5). Add a `provenance:` block to
   [`config/sources.yaml`](config/sources.yaml) and render the *qualitative* source string at
   the top of [`pipeline/digest.py`](pipeline/digest.py) and [`ui.py`](ui.py). Do **not**
   hardcode "98%" — that number is swapped in by Phase B once measured. Config-driven so copy
   updates without a redeploy.
2. **Trim `early_detection_biomarkers.count_query`** (CAPABILITIES.md §4). In
   [`config/interest_profile.yaml`](config/interest_profile.yaml) drop bare `biomarker`,
   `screening`, `exosome`; keep entity terms (`cfDNA`, `ctDNA`, `"CA19-9"`, `IPMN`, `PanIN`,
   `"liquid biopsy"`, `"circulating tumor cell"`). The area's 64% share should fall to
   something informative — that drop *is* the validation.
   ⚠️ Changing a `count_query` invalidates that area's stored weekly history: re-run
   `python -m pipeline.backfill --force` afterward so `coverage_counts` stays consistent.

---

## Phases (recommended order A → B → C → D → E → F)

| # | Phase | New / changed | Effort | Depends on | Spin-off ready now? |
|---|---|---|---|---|---|
| A | **Census backfill** | `pipeline/census.py` (new); fix `store/vectors.py` | 1–2 d + a run | — | ✅ yes |
| B | **Coverage harness** | `scripts/coverage_check.py` (new); `provenance:` block | 1–2 d | gold DOIs (user) | ✅ OpenAlex half DONE (2026-06-18); gold-set test still needs DOIs |
| C | **Granularity pass** | `eval/` acceptance tests; interest_profile.yaml | 2–3 d | **A merged** | ⛔ wait for A |
| D | **Seed + relevance-to-BCC** | interest_profile.yaml `exemplar_dois`; EPMC citations | 2–3 d | seed DOIs (user) | ⛔ wait for DOIs |
| E | **Novelty + macro→micro bridge** | `pipeline/analytics.py` over `papers` | 3–4 d | **A merged** | ⛔ wait for A |
| F | **Translational motion** | ClinicalTrials.gov feed | 2–3 d | — | ✅ yes |

---

### Phase A — Census backfill (the keystone)

Populate `papers` for *every* PDAC paper over ~5 yr (~53k): metadata + abstract + embedding
only. **No full text, no LLM.** This is what makes novelty, the macro→micro bridge, and the
granularity acceptance tests answerable *now* instead of after months of weekly accumulation.

**Reuses the existing path** — `harvest_europepmc()` → `normalize_records()` →
`embed_corpus()` → `db.upsert_papers()` — as a **new orchestration entry**, not a flag on
`harvest_all()`, because:

- `harvest_all()` is window-day-based and runs all three sources; the census wants
  **EPMC-only** (it's a superset per §1.4), **explicit date ranges**, and **resumability per
  window** (mirror `backfill.coverage_periods_present`).
- `max_pages` defaults to **10** ([`sources.yaml:27`](config/sources.yaml)) — 53k/1000 ≈ 53
  pages. Loop month/quarter windows so each stays under the cap and is independently resumable.
- Tag with `classify_and_score(..., client=None)` — the existing key-free path keeps the single
  best area by embedding similarity, **~0 added cost** (CAPABILITIES.md §1.3).

**Two code-level gotchas the spec glosses over — both real, fix before the run:**

- 🔴 **`first_seen_date` must be historical, not `today()`.**
  [`harvest.py:72`](pipeline/harvest.py) stamps `first_seen_date = date.today()`. If the census
  reuses `blank_record()` naively, all 53k papers become "new today" and **novelty (Phase E) is
  meaningless**. Stamp `first_seen_date` from `published_date` / the harvest window instead.
- 🔴 **`VectorIndex` can't append after load.** [`vectors.py:76`](store/vectors.py) `load()`
  fills `_matrix` + `_ids` but **not** `_rows`; `add()` appends to `_rows` and `_finalize()`
  rebuilds `_matrix` **from `_rows` only** → a resumable census that loads-then-appends
  **silently drops the previously saved vectors**. Fix: on `load()`, seed `_rows` from
  `_matrix` (or add an explicit concat/extend path).

Sizing is a non-issue: ~250–400 MB SQLite + ~80 MB vectors; commit per-window. Confirm
`upsert_papers` batches acceptably (currently one transaction at the loop end — per-window
writes handle this). **Unlocks:** novelty baseline, the bridge, the Phase C tests.

### Phase B — Coverage harness

New `scripts/coverage_check.py`: run the saved PDAC query against **EPMC and OpenAlex** (free
API, returns DOIs), diff the DOI sets (report overlap + each side's unique set), and run a
**gold-set recall test** against known Sears/Brody papers. Persist measured recall +
denominator + as-of date into the `provenance:` block → swap digest/UI copy from qualitative to
measured (§3.5). OpenAlex doubles as the citation/baseline backstop for Phase D, so wiring it in
here pays off twice. **The OpenAlex triangulation half is independent; the gold-set test needs
user DOIs.**

**Status — OpenAlex half DONE (2026-06-18).** `scripts/coverage_check.py` + the importable
`pipeline/openalex.py` client ship the EPMC×OpenAlex triangulation; `pipeline/harvest.py` gained
a polite `request_json` retry helper (shared by both). It writes a managed `provenance:` block to
[`config/sources.yaml`](config/sources.yaml) (tunable via a new `openalex:` config section).
Measured **recall ≈ 76%** over a settled 12-mo window, denominator = *EPMC ∪ OpenAlex DOI union*.
Methodology notes baked into the harness, learned by spot-checking the diff:
- Compare like-for-like: OpenAlex `title_and_abstract.search` (not `default.search`, which
  over-counts ~3× on full-text mentions) restricted to `type:article|review|preprint`.
- Use a **settled** window (default ends 30 d before today) — recent windows understate recall
  because EPMC `FIRST_PDATE` and OpenAlex `publication_date` disagree near the edges.
- The 76% is a **conservative lower bound**: the EPMC-absent remainder is ~89% genuine non-MEDLINE
  content (conference abstracts, Figshare/Zenodo deposits OpenAlex types as "article"), not query
  leaks. So `measured_recall` ≠ "we miss 24% of PDAC papers"; the `note` field says so.
**Still TODO:** the gold-set recall test is a clearly-marked stub (`gold_set_recall()`), gated on
user-supplied Sears/Brody seed DOIs; rendering the provenance line in `digest.py`/`ui.py` is the
separate do-now quick win.

### Phase C — Granularity pass

The `biomarker` trim is the do-now item; this phase adds the **method with acceptance tests**
(§4) over the census: assert **no area > 40% share**, **avg areas/paper < ~1.8** (down from
2.8), and build a **co-occurrence matrix** flagging any pair co-occurring > 60% (merge/split
candidates). Optionally let the data nominate areas (cluster census embeddings + tabulate
MeSH/EPMC annotations). These tests are meaningless on 138 papers — **they need the Phase A
census as their corpus.** Put them in [`eval/`](eval/run_eval.py) so they run like the existing
graders. Taxonomy stays in `interest_profile.yaml` (config, not code).

### Phase D — Seed + relevance-to-BCC

Populate the empty `exemplar_dois` ([`interest_profile.yaml`](config/interest_profile.yaml))
with Sears/Brody seed papers — per §2.2 the **single highest-leverage input**, for both
classifier anchoring and the *"N papers this week cite your work"* signal via EPMC's citations
endpoint. This is the signal a PI actually opens the email for. **Blocked on:** user-supplied
seed DOIs (same set as the Phase B gold test).

### Phase E — Novelty + macro→micro bridge

Two things, both computed **over the census `papers` table** so macro numbers share IDs with
curated papers:

- **Novelty:** first-appearance of an entity / gene×drug / gene×modality pairing (needs A's
  *historical* `first_seen_date` — see the Phase A gotcha).
- **Bridge:** compute trends over `papers` (not only `coverage_counts`), so "ctDNA is heating
  up" lands on *this week's ctDNA papers*. The in-flight [`pipeline/analytics.py`](pipeline/analytics.py)
  change (deep-linking keyword movers to EPMC via `_epmc_link` / `pdac_query`) is exactly this
  pattern — E generalizes it to resolve to *local, curated* papers.

Biggest lever, most code. **Depends on A.**

### Phase F — Translational motion

ClinicalTrials.gov v2 API as a first-class signal (new registrations, first-in-human) — a
care-relevant headline for a Center for *Care*, where trial motion matters more than publication
counts. EPMC also tags trial references. Structurally independent; slot in anytime.

---

## Dependency DAG / parallelization

```
quick wins ──(provenance)──► B (swaps in measured number)
            └─(biomarker trim)─► C

A (census) ──► C (acceptance tests)
           └──► E (novelty + bridge)

B ── independent (OpenAlex), gold-set gated on user DOIs
D ── gated on user DOIs
F ── independent
```

- **Parallelizable right now:** the two quick wins, **A**, **F**, and the OpenAlex half of **B**.
- **Must wait for A to merge:** **C** and **E** (the census is their working corpus; building
  them against today's 138-row DB bakes in wrong assumptions).
- **Gated on user input:** **D** and the gold-set half of **B** (Sears/Brody DOIs).

---

## Spinning off phases as separate tasks

Each phase is written to be **independently demoable** (per `CLAUDE.md` build order) and can run
as its own session + worktree. Two rules make that clean:

1. **Respect the DAG above.** Spin off A, B, F (and the quick wins) now; hold C/D/E until their
   gate clears. Six parallel worktrees would have C and E editing against a repo with no census.
2. **A spun-off session starts cold** — no memory of the conversation that produced this plan.
   That's fine *because this file exists*: each task prompt points the cold session at its phase
   section here (file paths + gotchas are all inline), so it can act self-sufficiently.

---

## Needs user input (gates B-goldset, C-tuning, D)

- **Sears/Brody seed / gold DOIs** — unblocks D, the B gold-set, and classifier tuning. Highest
  leverage single input.
- **Email provider + sender domain** (SPF/DKIM) — open from `CLAUDE.md`; gates live send, not A–F.
- **OHSU TDM/API access** — scopes full-text Q&A breadth; not blocking A–F.

---

## Recommended first move

Land the two quick wins today, then build **Phase A** — the keystone that unblocks C and E.
