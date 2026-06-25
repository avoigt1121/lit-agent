# ADR-0003: Add a Paper↔Paper Write-Back Relationship Layer (Agreement / Conflict / Gap)

**Status:** Proposed (2026-06-24)
**Date:** 2026-06-24
**Deciders:** Annie Voigt (project lead)
**Scope:** `lit-agent` offline corpus layer — a new inter-paper relationship store (`store/db.py`) and a new offline scoring step (`pipeline/relate.py`) wired into `pipeline/run_weekly.py`, reusing the existing `Embedder`/`VectorIndex` (`pipeline/score.py`, `store/vectors.py`) and the cheap, provider-switchable client from ADR-0002 (`pipeline/llm.py`). Read-side surfacing in `qa/` and the Space is **optional / phased** (see Action Items 6–7). The Q&A grounding guard's wording and model (`qa/answer.py`) are **out of scope**. Prompted by Dr. Rosalie Sears forwarding "Orthogy Pancreatic" (2026-06-22).

---

## Context

An external project, **Orthogy Pancreatic** (Orthogonal Data LLC), was flagged as
relevant to `lit-agent`. It is a read-only, AI-maintained PDAC research wiki built on
Andrej Karpathy's public **LLM-Wiki** pattern. A review of its public methodology
(orthogy.org/faq) against this codebase found that `lit-agent` already implements most
of the pattern and does several things Orthogy cannot:

- **Source-of-truth records** — the `papers` table (`store/db.py`, schema v5) is already
  one deduped row per paper, exactly the "primary research record."
- **Lane taxonomy + classification** — `config/interest_profile.yaml` (8 curated
  Sears/Brody focus areas) + `classify_and_score`/`confirm_with_llm` (`pipeline/score.py`).
- **Scheduled maintenance, dedup, comprehensive corpus, grounded interactive Q&A, an
  eval harness** — none of which Orthogy has.

The **one** capability Orthogy has that `lit-agent` lacks: **relationships *between*
papers.** Today `topic_tags` links paper→focus_area, but there are no paper↔paper edges.
`qa/retrieve.py` returns top-k passages independently; an answer cannot say "this finding
is corroborated by X and contested by a 2026 study Y." Orthogy's distinguishing move is
that on ingestion it classifies how a new paper relates to existing ones — **agreement,
conflict, or gap** — and writes those notes *back into the related records*, turning a
set of summaries into a connected knowledge graph (consensus, controversy, open questions).

That accumulation is the genuinely missing piece, and it fits the existing architecture
as an additive offline step — not a rewrite, and explicitly **not** a reason to fork an
LLM-Wiki template (that would discard `normalize.py`, `eval/`, the harvest layer, and the
~44.5k-paper corpus).

### Forces

- **Standalone must hold.** Implement natively; the LLM-Wiki templates and Orthogy are
  read-only references, never imports or dependencies (CLAUDE.md hard constraint).
- **Offline invariant must hold.** Edges are computed in the weekly Job and persisted to
  the durable corpus; the Space reads them read-only. Never compute relations on-demand
  in the Space.
- **Groundedness is sacred.** Hallucinated edges (asserting a conflict that isn't real)
  are the central risk and a direct threat to "answer only from real evidence." Every
  edge must be evidence-bound, verified against source text, and **eval-gated** before it
  reaches the digest or Q&A — the same bar ADR-0002 set for the cheap classifier.
- **Config-not-code.** Relation types, candidate caps, and floors live in
  `config/interest_profile.yaml`, mirroring the existing `classification:` block.
- **Cost / combinatorics.** Pairwise relation over ~44.5k papers is combinatorial; it
  must be bounded (within-lane, above the relevance floor, new-papers-only per run).
- **Respect de-pollution (2026-06-20).** Quarantined rows (`excluded=1`) keep their
  vectors but are dropped from `iter_papers(include_excluded=False)` and the retriever;
  the relate step must skip them too.
- **Fit the post-v1 roadmap.** Edges should be **per-focus-area filterable** so they
  drop straight into the planned per-area Space tabs.

---

## Decision

Add a paper↔paper **relationship layer** computed offline and surfaced read-only.

- **Schema (v6, additive).** New `relations(src_paper_id, dst_paper_id, rel_type, note,
  focus_area, confidence, evidence, created_at)` table in `store/db.py`, with an
  idempotent migration in the spirit of `_migrate_excluded_columns`. `rel_type ∈
  {agreement, conflict, gap}`. Edges are stored bidirectionally (write-back into *both*
  records). No change to `papers`/`topic_tags`.
- **New step `pipeline/relate.py`** (mirrors `score.py`'s shape): for each **newly
  ingested, non-excluded** paper, retrieve candidate neighbors via the existing
  `VectorIndex`, **bounded to same focus area + above a relevance floor**; classify each
  pair into `{agreement, conflict, gap, none}` with a one-sentence, citation-bearing
  `note` via the cheap client; **verify** the cited claim against source text; persist
  edges with `confidence` + `evidence`, dropping low-confidence/unverifiable ones.
- **Reuse ADR-0002's client.** Relation classification is a cheap, offline, batch,
  latency-insensitive call — route it through `cheap_client()` / `_maybe_client()`
  (`LLM_PROVIDER` switch), keeping `ANTHROPIC_API_KEY` as the one-switch fallback.
- **Wire into `run_weekly`** after `classify_and_score`, offline only, persisted to the
  HF Dataset alongside the SQLite + vectors.
- **Config block `relate:`** in `interest_profile.yaml` — `candidate_top_k`,
  `min_relevance_floor`, `confidence_threshold`, `per_run_scope: new_only`,
  `rel_types`. Defaults conservative so the feature is off/empty until tuned.
- **Eval-gate it.** Add an edge-accuracy grader + a small labeled set
  (`eval/relations_set.json`); promote the step only when precision holds and the
  hallucinated-edge rate is ≈0.
- **Read side (phased, optional):** make `qa/retrieve.py` graph-aware (attach connected
  edges to top-k so `qa/answer.py` can surface conflict/gap context with its existing
  DOI citations — guard wording unchanged), and add **"Controversies"** (conflict-edge
  clusters) / **"Open Questions"** (gap edges) views as per-area Space tabs + digest
  sections.

## Consequences

**Easier**
- Cross-paper context (consensus / controversy / open questions) becomes first-class,
  not re-derived per query — the core LLM-Wiki advantage over RAG.
- Q&A can cite agreement/conflict, making it strictly better than Orthogy's static wiki.
- Controversies / Open-Questions views fall out of the edges — directly useful for grant
  aims and hypothesis generation, a deliverable Orthogy's read-only format can't produce.
- Reuses existing infra (embedder, vector index, ADR-0002 client, eval harness, per-area
  filtering) — small surface area.

**Harder / risks**
- **Hallucinated edges** are the main risk. Mitigation: evidence-bound notes, source
  verification, a confidence threshold, and eval-gating before the digest/Space — never
  fabricate an edge (parity with `confirm_with_llm` returning `{}` rather than guessing).
- **Combinatorial cost.** Mitigation: within-lane + floor + new-papers-only; backfill a
  lane at a time; watch Jobs billing (ADR-0001).
- **Schema migration** on a 44.5k corpus. Mitigation: additive table only; no
  `vectors.npz` rebuild; reversible.
- **Edge staleness** as the corpus grows. Mitigation: recompute edges touching a record
  when it changes; idempotent upsert keyed on `(src, dst, rel_type)`.
- **Two more config knobs** + a second cheap-call site. Mitigation: keep them in the
  single `relate:` block; document in `.env.example` if any new secret is needed (none
  expected — reuses ADR-0002's client).

**Non-goal:** No change to `harvest`/`normalize`/`digest` core logic, to the corpus
`papers`/`topic_tags` schema, to what counts as "new" (`first_seen_date`), or to the
Q&A grounding guard's wording/model. Not building a full atomic-claim graph over the
entire corpus in one shot.

## Alternatives considered

- **Do nothing (keep papers isolated).** Simplest; forgoes the single capability Orthogy
  has that we don't. Rejected — the relationship web is the high-value idea worth adopting.
- **Fork an LLM-Wiki template** (tonbistudio/llm-wiki, lucasastorian/llmwiki, etc.).
  Rejected — violates standalone, and would throw away dedup, eval, the multi-source
  harvest, and the existing corpus. Read for ideas only.
- **Compute relations on-demand in the Space** (RAG-style at query time). Rejected —
  violates the offline-pipeline-vs-online-Space invariant, is expensive, and is
  non-deterministic. Precompute offline; serve read-only.
- **Relate across the whole corpus, no lane bound.** Rejected — combinatorial and
  low-precision (all PDAC text clusters; §9.3). Bound within-lane + floor.
- **Heavyweight graph DB (Neo4j, etc.).** Rejected for v1 — a SQLite `relations` table
  plus the existing `VectorIndex` cover candidate-finding and traversal at this scale.
  Revisit only if multi-hop traversal needs outgrow SQL.
- **Atomic-claim extraction first** (compare claims, not abstracts). Deferred — start
  with abstract-level comparison to keep scope tight; add claim extraction in `score.py`
  only if edge precision demands it.

## Action Items

1. [ ] Add `relations` table + **schema v6** idempotent migration in `store/db.py`
   (mirror `_migrate_excluded_columns`); add read accessors (`iter_relations`,
   `relations_for_paper`). Bump `SCHEMA_VERSION`.
2. [ ] New module `pipeline/relate.py`: candidate retrieval via `VectorIndex`
   (within-lane, above `min_relevance_floor`, skip `excluded=1`); pairwise
   `{agreement|conflict|gap|none}` classification via `cheap_client()` with a
   one-sentence evidence note; source-verify; bidirectional persist with
   `confidence` + `evidence`.
3. [ ] Add a `relate:` block to `config/interest_profile.yaml`
   (`candidate_top_k`, `min_relevance_floor`, `confidence_threshold`,
   `per_run_scope: new_only`, `rel_types`) — config-not-code; conservative defaults.
4. [ ] Wire a `relate` step into `pipeline/run_weekly.py` after `classify_and_score`,
   offline only, reusing `_maybe_client()`; persist edges to the HF Dataset with the
   SQLite + vectors.
5. [ ] **(eval)** Add an edge-accuracy grader to `eval/run_eval.py` + a small labeled
   `eval/relations_set.json`; gate rollout on precision and a near-zero hallucinated-edge
   rate (same discipline as ADR-0002 §4). Needs the candidate LLM provider.
6. [ ] **(optional, phased)** Graph-aware `qa/retrieve.py`: attach connected edges to the
   top-k so `qa/answer.py` surfaces conflict/gap context with its existing DOI citations —
   grounding guard wording unchanged.
7. [ ] **(optional, phased)** "Controversies" (conflict-edge clusters) and "Open
   Questions" (gap-edge) views as per-area Space tabs + digest sections, reusing the
   per-focus-area filtering in `analytics.py` / `digest.py`.
8. [ ] **(operator)** Backfill edges for the existing corpus **one lane at a time** (start
   `early_detection_biomarkers`); eyeball edge quality before scaling; watch Jobs billing.
9. [ ] Document: add a "Decisions resolved" note in `CLAUDE.md` and update `DEPLOYMENT.md`
   if any new step/secret is introduced (none expected — reuses ADR-0002's client).

## References

- Orthogy public methodology (FAQ — source-of-truth records, write-back of
  agreement/conflict/gap, scheduled updates): <https://www.orthogy.org/faq>
- Andrej Karpathy — LLM-Wiki gist (the public pattern Orthogy is built on):
  <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
- Internal: `lit-agent/CLAUDE.md` (offline pipeline vs online Space; "answer only from
  real evidence"; multi-label classifier; post-v1 per-area roadmap; de-pollution
  2026-06-20); `store/db.py` (schema v5, `papers`/`topic_tags`, `_migrate_excluded_columns`);
  `pipeline/score.py` (`classify_and_score`/`confirm_with_llm`, `Embedder`/`VectorIndex`);
  `qa/retrieve.py` + `qa/answer.py` (grounded Q&A + anti-fabrication guard); `eval/`
  (graders + labeled sets); ADR-0002 (the cheap, provider-switchable client this reuses);
  ADR-0001 (the HF Job this step runs inside).
