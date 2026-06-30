# ADR-0004: Corpus-Side Relationship DATA Layer (mentions / citations / derived edges / OHSU map)

**Status:** Accepted (2026-06-29) — implemented on branch `feat/relationship-layer`
**Date:** 2026-06-29
**Deciders:** Annie Voigt (project lead)
**Scope:** `lit-agent` offline corpus layer only — new tables in `store/db.py`
(schema **v6**) and four offline populators (`pipeline/mentions.py`,
`pipeline/citations.py`, `pipeline/relationships.py`, `pipeline/ohsu_map.py`),
wired into `pipeline/run_weekly.py`. **No change to the Space / chat (`app.py`,
`ui.py`, `qa/`).**

---

## Context

This is **Tier 3 (the DATA layer)** of a 3-tier roadmap: build the corpus-side
relationship data that future cross-paper-inference tools will consume. It is the
long-pole, independent workstream and is built ahead of, and separate from, the
chat-facing tools that will read it.

Today the corpus has only paper→focus_area edges (`topic_tags`) and no inter-paper
or literal-entity structure. Three concrete gaps motivated this:

1. **No literal mention index.** `focus_areas`/`topic_tags` are CLASSIFIER labels:
   the `myc` tag denotes the broad "Oncogenic drivers & gene regulation" area, so
   "which papers MENTION MYC?" is unanswerable from it. The normalized record
   reserves `annotations.genes` / `annotations.diseases` (CLAUDE.md schema) but the
   harvest never populates them, so even that path is empty in the current corpus.
2. **No citation graph.** The PI backlog asks for **citation tracking** — surface
   papers citing the BCC's key references (seed: PMID 39636224, Loveless et al.
   PDAC single-cell atlas — a *Steele-lab* reference of interest, **not**
   BCC/Sears-authored). There is no paper↔paper citation store.
3. **No derived relationships / OHSU mapping.** The backlog's "related papers",
   **cross-field correlation alert** (e.g. a PDAC finding relating to the nervous
   system), and "identify OHSU research targets / correlate with active OHSU work"
   all need a paper↔paper edge table and a paper↔OHSU-interest table to build on.

### Relationship to ADR-0003

ADR-0003 proposes **semantic** edges (`agreement` / `conflict` / `gap`) computed by
an LLM. ADR-0004 is the **structural / literal** substrate underneath it: mentions,
citations, and deterministic derived edges — **no LLM, no inference, every edge is
a verifiable fact**. The two are complementary and use different tables
(`paper_relations` here vs. ADR-0003's planned `relations`); this ADR claims
schema **v6** with the structural tables, and ADR-0003's semantic table is a later
additive bump.

### Forces

- **Standalone** (CLAUDE.md): native implementation; Orthogy / LLM-Wiki are
  read-only references, never imports.
- **Offline invariant**: populate in the weekly Job, persist to the durable corpus,
  Space reads read-only. Never compute on-demand in the Space.
- **Answer only from real evidence**: the literal layers must not fabricate. Every
  mention/edge is grounded in source text or a sanctioned EPMC link.
- **Access ≠ scraping**: citations come from EuropePMC's public REST
  citations/references endpoints only — never a library proxy.
- **Config-not-code**: the lexicon, citation targets, and edge thresholds live in
  `config/interest_profile.yaml` (`relationships:` block).
- **Bounded combinatorics**: pairwise work over ~44.5k papers must be bounded —
  within shared-gene candidates, capped neighbors, new-papers-only per run.
- **Fit the post-v1 roadmap**: edges are focus-area-filterable so they drop into the
  planned per-area Space tabs without a rewrite.

---

## Decision

Add four additive tables (schema **v6**, `store/db.py`) + four offline populators.

### Schema (v6, additive — no change to `papers` / `topic_tags`)
- **`mentions`** `(paper_id, entity_type, entity, method, count)` — literal
  entity-mention index. `method ∈ {literal_scan, epmc_annotation}`.
- **`citation_edges`** `(citing_src, citing_ext_id, cited_src, cited_ext_id,
  citing_paper_id?, cited_paper_id?, source, created_at)` — directed citation graph,
  keyed by EPMC `(source, ext_id)` so an edge survives when one endpoint is outside
  the corpus; nullable `*_paper_id` resolve as papers are ingested.
- **`paper_relations`** `(src_paper_id, dst_paper_id, rel_type, weight, evidence,
  created_at)` — derived undirected edges, `src < dst` canonical;
  `rel_type ∈ {shared_genes, shared_focus, citation}`.
- **`ohsu_interest_links`** `(paper_id, interest_id, interest_kind, score,
  evidence, created_at)` — paper↔OHSU-interest map (`interest_kind ∈ {seed_author,
  lab, focus_area}`); v1 populates `seed_author` only.
- **`relationship_progress`** `(layer, paper_id, done_at)` — one resumable
  high-water table for all four populators (mirrors `census_progress`).

### Populators (offline, new-papers-only, resumable)
- **`pipeline/mentions.py`** — literal-scan title+abstract against a config lexicon
  (built from `tracked_keywords` + focus-area `keywords` + `extra_genes`).
  Case-SENSITIVE for short all-caps symbols (≤4 chars: MYC, KRAS, ATR) to avoid
  English-word false positives (MAX, ARE, CAR); plus any existing EPMC annotations.
- **`pipeline/citations.py`** — fetch papers citing configured `track_targets` from
  EPMC (seed: the Loveless atlas); optional (off by default) references-for-corpus
  pass. Rate-limited + retrying via the harvest session.
- **`pipeline/relationships.py`** — deterministic derived edges from mentions +
  focus areas + citations. Bounded: a paper is compared only to its shared-gene
  co-mention candidates (capped at `max_neighbors`) and its resolved citation
  neighbors. `shared_focus` is emitted only on that bounded neighbor set, never over
  all co-classified pairs.
- **`pipeline/ohsu_map.py`** — STUB: surname-match against `config/seed_authors.yaml`
  → `seed_author` links. Richer mappings slot in as new `interest_kind` rows.

### Wiring + persistence
- A `_relationship_step` runs in `run_weekly` after classification (gated by
  `--no-relationships` and skipped under `--no-embed`), each layer isolated in
  try/except so a hiccup never aborts the digest/persist. Order: mentions →
  citations → relations → ohsu.
- The tables live in `corpus.sqlite`, so the **existing** HF Dataset push/pull
  carries them — **no change to `sync_to_hub`/`pull_from_hub`**.

---

## Consequences

**Easier**
- "Papers that mention MYC" (and any tracked gene) is now a literal query —
  decoupled from classifier labels.
- Citation tracking for BCC reference papers is first-class (the Loveless seed live:
  31 citers on page 1 at authoring time).
- "Related papers" and the **cross-field correlation alert** fall straight out of
  `shared_genes` + differing `focus_areas` (verified: a PDAC paper links to a
  neuroscience paper via shared MYC/KRAS in tests).
- OHSU-interest mapping has a real table + accessors for the PI-backlog tools.
- Reuses existing infra (EPMC session, vector/mention store, config pattern); small,
  additive surface.

**Harder / risks**
- **Lexicon false positives.** Mitigated by case-sensitive short-symbol matching and
  word-boundary regex; tunable in config. (Tested: `car`/`max` do not match.)
- **Citation-fetch cost / politeness.** Bounded by `max_pages` per target, the polite
  retrying session, and targets-only by default (corpus-refs pass is opt-in).
- **Combinatorial derived edges.** Bounded to gene-candidate neighbors + caps +
  new-papers-only; backfill a layer at a time.
- **Schema migration on 44.5k corpus.** Additive tables only (`CREATE IF NOT
  EXISTS`); no `vectors.npz` rebuild; reversible.

**Non-goal:** the chat-facing inference tools (Tier 1/2) — this ADR ships only the
DATA layer + offline population. No change to harvest/normalize/digest core logic,
to `papers`/`topic_tags`, to "new" (`first_seen_date`), or to the Space/Q&A.

## Alternatives considered
- **Reuse `topic_tags` for mentions.** Rejected — conflates classifier labels with
  literal terms; the whole point is to separate them.
- **LLM-extract entities/edges now.** Deferred to ADR-0003 — start with deterministic,
  zero-hallucination literal structure; layer semantics on top later.
- **Graph DB (Neo4j).** Rejected for v1 — SQLite tables + the existing vector index
  cover candidate-finding and traversal at this scale.
- **Citation source = OpenAlex.** EPMC chosen as primary (already the harvest
  backbone, sanctioned TDM); OpenAlex remains a possible backstop.

## Action Items
1. [x] Schema v6 tables + accessors in `store/db.py`; `SCHEMA_VERSION` bump.
2. [x] `pipeline/mentions.py` — literal mention index (+ EPMC annotations passthrough).
2b. [x] `pipeline/annotate.py` — **broad-recall enrichment via the EPMC Annotations
   API** (see "EPMC annotation enrichment" below). Done 2026-06-29 on branch
   `feat/epmc-annotations`.
3. [x] `pipeline/citations.py` — EPMC citation graph; Loveless seed target.
4. [x] `pipeline/relationships.py` — derived `shared_genes` / `shared_focus` / `citation` edges.
5. [x] `pipeline/ohsu_map.py` — seed-author OHSU-interest stub.
6. [x] `relationships:` config block in `config/interest_profile.yaml`.
7. [x] Wire `_relationship_step` into `run_weekly` (offline, `--no-relationships` gate).
8. [x] Tests (`tests/test_relationship_layer.py`); ADR + CLAUDE.md note.
9. [ ] **(operator)** Backfill the existing corpus one layer at a time
   (`python -m pipeline.mentions --all`, etc.); eyeball quality; watch Jobs billing.
10. [~] **(later, separate)** Read-side surfacing in `qa/` + per-area Space views.
    First two read-side capabilities landed 2026-06-30 (branch `feat/entity-read-side`):
    (1) a `find_papers_mentioning` tool in the Q&A planner (`qa/planner.py` +
    `qa/corpus_qa.papers_mentioning_text`) so chat answers "which/how many papers
    mention <gene/drug/disease>?" from the mentions index; (2) most-mentioned-entity
    leaderboards (`pipeline/analytics.entity_leaderboards[_html]`) rendered on the
    Space 'Trends' tab (computed once at startup from the read-only corpus + cached to
    `analytics.json`). Per-area tabs + semantic edges still pending.

## EPMC annotation enrichment (2026-06-29, branch `feat/epmc-annotations`)

The deferred item — "the harvest never populates `annotations.genes/diseases`, so
the literal index relies only on the curated lexicon" — is now addressed with a
broad-recall enrichment pass.

- **`pipeline/annotate.py`** — a SEPARATE, resumable populator (not folded into the
  weekly literal scan) that pulls Europe PMC's text-mined annotations for new,
  EPMC-addressable papers and writes them as `method='epmc_annotation'` rows in the
  same `mentions` table. This adds every gene/disease/chemical EPMC's NLP tagged on
  top of the ~30-80 curated literal terms.
- **Sanctioned API:** the Europe PMC **Annotations API**
  (`annotations_api/annotationsByArticleIds?articleIds=MED:<pmid>&type=…`) — a public
  TDM service, NOT the core search endpoint, NOT scraping. Reuses harvest's polite
  retrying session (`_session`/`request_json`/`POLITE_PAUSE`); batches up to 8
  articleIds/request (the API cap). Live shape confirmed once against pmid 39636224.
- **Merge-aware, non-destructive:** writes via the new
  `db.set_mentions_for_method(...)`, which replaces only the paper's
  `epmc_annotation` rows — the independently-run `literal_scan` rows are preserved.
  The two populators run in either order.
- **Resumability:** new `relationship_progress` layer `'annotations'`. New-papers-only,
  cap-limited per run (`per_run_cap`, default 2000); papers with no EPMC id are
  marked done without a fetch.
- **Mapping:** EPMC annotation `type` → our `entity_type` (`Gene_Proteins`→gene,
  `Diseases`→disease, `Chemicals`→chemical, `Organisms`→organism; config-overridable),
  `entity` = the literal `exact` span (grounded default) or EPMC's preferred tag name
  (`entity_source: preferred`), `count` = #occurrences EPMC tagged.
- **Config-gated, default OFF:** lives under the existing
  `relationships.mentions.use_epmc_annotations` flag (plus `annotations.{types,
  entity_source, batch_size, per_run_cap}`). `run_weekly._relationship_step` runs it
  after `mentions` and before `relations` (so `shared_genes` edges can benefit),
  isolated in the same per-layer try/except, only when the flag is on. No
  `sync_to_hub` change — the rows ride the existing corpus push/pull.
- **Tests:** `tests/test_epmc_annotations.py` (network-free; mapping + merge writer +
  resumability + `papers_mentioning` on annotation-only entities). 6/6 pass;
  `test_relationship_layer.py` still 6/6.

## References
- Internal: `store/db.py` (schema v6, `_migrate_excluded_columns` pattern);
  `pipeline/harvest.py` (`_session`, `request_json`); `config/seed_authors.yaml`,
  `config/interest_profile.yaml`; ADR-0003 (semantic edges this underpins); CLAUDE.md
  ("Research to-do backlog": citation tracking, cross-field correlation alert, OHSU
  research targets).
- EuropePMC citations API: `…/webservices/rest/{source}/{id}/citations` and
  `/references` (sanctioned public REST; not scraping).
