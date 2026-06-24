# lit-agent — build conventions

A **standalone** Literature Review Agent for the BCC. It (1) emails a weekly digest of
new open-access PDAC literature to a 5–20-person list, (2) reports which topics were most
covered over week/month/year, and (3) runs as a Hugging Face Space chat that answers
follow-up questions grounded in the papers it ingested.

## Memory maintenance (after every commit)

This repo is **standalone**: it has no `memory.md`/`TODO.md` and is **not** part of
`SHOWCASE_STATUS.md`. So the only living doc to keep current is this file. After a
`git commit` that changes scope, settles an open question, or alters the build plan,
update the relevant section here (e.g. "Decisions resolved", "Decisions still open").
Skip it for routine code commits.

A global PostToolUse hook (`~/.claude/hooks/remind-memory-sync.py`, in
`~/.claude/settings.json`) prints this reminder automatically after each commit in this
repo. It only *reminds* — the edit is manual.

## Hard constraints (do not violate without being asked)

- **Standalone.** The repos under "Reference repositories" are STRUCTURAL REFERENCES
  ONLY. Do not depend on, import from, or list them in `requirements.txt`.
- **Offline pipeline vs. online Space.** Ingestion/scoring/emailing runs in a scheduled
  offline job. The Space only serves chat + cached analytics. Never ingest on-demand in
  the Space.
- **Answer only from real evidence.** Q&A answers strictly from retrieved passages with
  DOI citations. If a methodology question targets a paper with no OA full text, say the
  full text isn't available and summarize the abstract — never infer/fabricate methods.
- **OA-only for v1.** Always have metadata + abstracts; full text only for open-access
  papers. (If OHSU TDM/API access is later provisioned, that expands — but access ≠
  redistribution: don't embed licensed full text in emails; link out + short snippets.)
- **Access ≠ scraping.** Never crawl a library proxy (EZproxy/OpenAthens/Shibboleth).
  Use sanctioned publisher TDM/API tokens only.
- **Config, not code.** BCC focus areas, the PDAC query, and the recipient list live in
  `config/*.yaml` so they change without a redeploy.
- **Define "new" as `first_seen_date`** and apply it consistently.
- **Email stays in dry-run** until explicitly enabled (a `SEND_LIVE` env flag).

## Reference repositories (read-only; mirror structure, don't import)

- `../research-coordinator` — **primary template.** Mirror:
  - `app.py`, `gradio_ui.py` — HF Space + streaming chat shell + transparency panels
  - `eval/run_eval.py` — eval harness + trace-aware LLM judge + anti-fabrication backstop
  - `.github/workflows/sync-to-hf-space.yml` — deploy/sync workflow (dev/prod, retries)
  - `prompts.yaml`, `agents.yaml` — config-driven design
- `../biodata-registry` — **data-layer template.** Model `store/` on its package
  structure: manifests + loaders + a `*_list_available`-style accessor.
- `../DecoupleRpy_Agent` — **groundedness discipline ONLY.** Study how it answers from
  real evidence, shows its trace, and refuses to fabricate. DO NOT copy its domain logic
  (decoupleR/scanpy computation, dataset-selection heuristics) — different data domain.

## Project structure

```
lit-agent/
  app.py                  # Gradio entrypoint (HF Space)
  ui.py                   # chat UI (mirror research-coordinator/gradio_ui.py)
  config/
    interest_profile.yaml # BCC focus areas (keywords, exemplar DOIs, audience note)
    sources.yaml          # PDAC query string + per-source params
    recipients.yaml       # digest distribution list
  pipeline/
    harvest.py            # Europe PMC / PubMed / bioRxiv / medRxiv clients
    normalize.py          # schema mapping + dedup + preprint->published linkage
    score.py              # embed + classify into focus areas + relevance score
    digest.py             # compose + render HTML + send (--dry-run flag)
    analytics.py          # week/month/year aggregations
    clinicaltrials.py     # ClinicalTrials.gov v2 feed — "translational motion" signal (Phase F)
    run_weekly.py         # orchestrates harvest -> ... -> digest -> analytics
  store/
    db.py                 # SQLite schema + read/write
    vectors.py            # embedding index read/write
  qa/
    retrieve.py           # top-k retrieval over the corpus
    answer.py             # grounded answer + guards
  eval/
    questions.json        # Q&A bank
    relevance_set.json    # labeled digest items
    run_eval.py           # mirror research-coordinator/eval design
  .github/workflows/
    weekly.yml            # cron trigger for pipeline/run_weekly.py
  requirements.txt  README.md  DEPLOYMENT.md  CLAUDE.md
```

## Data sources

- **Europe PMC REST** — PRIMARY (all PubMed + OA full text + text-mined annotations).
- **PubMed E-utilities** — secondary; MeSH tags. Cap 3 req/s (10 with a free API key).
- **bioRxiv / medRxiv API** — REQUIRED for recency; `details` endpoint, paginated.
- **ClinicalTrials.gov v2 REST** — a SEPARATE "translational motion" signal, NOT a
  literature source: new PDAC trial registrations + early-phase / first-in-human
  activity (a care-relevant headline for a Center for *Care*). Offline pipeline only;
  has its own `clinicaltrials:` query block in `config/sources.yaml` and deliberately
  does not touch the Europe PMC literature query.

Use one saved, version-controlled PDAC query in `config/sources.yaml` so coverage is
reproducible.

## Normalized paper record (every source maps to this before storage)

```json
{
  "doi": "10.x/...",
  "ids": {"pmid": "...", "pmcid": "...", "preprint_doi": "..."},
  "title": "...", "abstract": "...",
  "authors": ["..."], "journal_or_server": "...",
  "published_date": "YYYY-MM-DD", "first_seen_date": "YYYY-MM-DD",
  "is_oa": true, "oa_fulltext_url": "... | null",
  "source": "europepmc|pubmed|biorxiv|medrxiv",
  "is_preprint": false, "linked_published_doi": "... | null",
  "mesh": ["..."], "annotations": {"genes": [], "diseases": []},
  "focus_areas": ["..."], "relevance_score": 0.0,
  "embedding_id": "..."
}
```

## Corpus store

- `papers` (PK = normalized DOI, fallback synthetic id) — one row per deduped record
- `topic_tags` (paper_id, focus_area, score) — multi-label + analytics
- `runs` (run_date, window, n_harvested, n_new, n_emailed) — audit + deltas
- `vectors` — embedding per paper (abstract; + chunked OA full text where available)
- **Persistence:** HF Space storage is ephemeral — commit the SQLite file + index to a
  durable store (HF Dataset repo or external DB) at the end of each run; the Space loads
  it read-only at startup.

## Prompt skeletons

- **Classify (cheap model):** "Given this title+abstract and these focus-area
  descriptors, return matching area ids with a 0–1 confidence each; [] if none. JSON only."
- **Relevance note:** "In one sentence, explain why this paper matters to <audience_note>,
  using only claims supported by the abstract. No overstatement."
- **Q&A grounding (guard):** "Answer ONLY from the retrieved passages, citing DOIs. If the
  question asks methodology and only an abstract is present, say full text isn't available
  and summarize the abstract — do not infer methods."

## Build order (each phase independently demoable; dev surface first)

0. **Spike** — `harvest.py` for all sources; pull last 7 days → `data/spike.json`.
1. **Corpus** — `store/` + `normalize.py` (DOI norm, fuzzy-title dedup, preprint linkage)
   + `score.py` (embed abstracts); persist to durable store.
2. **Digest (dry-run)** — interest model + `digest.py` → `out/digest_<date>.html`, no send.
3. **Deliver** — transactional email provider + `recipients.yaml` + `weekly.yml` cron;
   gate live send behind `SEND_LIVE`.
4. **Analytics** — `analytics.py` windows/deltas; cache for Space + email footer.
5. **Q&A Space** — `app.py`/`ui.py` + `qa/retrieve.py` + `qa/answer.py` with the guard.
6. **Eval & harden** — groundedness + digest-relevance graders; iterate.
7. **(Optional)** — register as a specialist in research-coordinator `agents.yaml`
   (one entry, no `router.py` changes).

## Post-v1 roadmap (design for it now; don't build it yet)

The end-state is a **broad set of focus areas** (eventually most of PDAC research,
not a hand-picked few), surfaced two ways:

- **Per-focus-area tabs in the Space** — each focus area becomes its own tab:
  that area's recent papers, its coverage analytics, and Q&A scoped to it.
- **Per-recipient focus-area subscriptions** — each person subscribes to the
  areas they care about and the weekly email is filtered to those; v1 sends the
  whole digest to everyone.

Build v1 so this is a later config/UI change, not a rewrite:
- `recipients.yaml` carries an **optional per-recipient `focus_areas`** list
  (omitted / empty ⇒ all areas). v1 ignores it (sends everyone everything); the
  field exists so Phase 3+ can segment without a schema change.
- `digest.py` composes the digest as **independent per-area sections**, so a
  per-recipient email is just a filtered subset of sections.
- `analytics.py` and `qa/retrieve.py` stay **filterable by focus area**, so the
  Space can render one tab per area off the same cache/index.
- The classifier is **multi-label** already, so growing the area count is fine —
  watch per-area precision (§9.3), not plumbing.

## Decisions still open (ask before assuming)

- OHSU TDM/API access (OA-only vs near-comprehensive Q&A)
- The actual BCC focus areas + exemplar DOIs to seed `config/interest_profile.yaml`
- Email provider + sender domain (SPF/DKIM)

### Decisions resolved (2026-06-17)

- **Persistence host** → HF Dataset repo (`CORPUS_HF_DATASET`); pipeline pushes, Space pulls read-only.
- **Embedding model** → local BGE-small via `fastembed` (ONNX, no API key); `EMBEDDING_MODEL` overrides.
- **Definition of "new"** → `first_seen_date`.

### Decisions resolved (2026-06-20) — corpus de-pollution

The Phase A census ingested whatever the saved EPMC query returned; that query was
unrestricted-field (matched title, abstract, full text AND references), so it swept
in off-topic papers and abstract-less meta records.

- **Root cause = the query, not the census.** `census.py` has no relevance gate by
  design; it faithfully ingests EPMC results. Fixed at the source.
- **Query → title/abstract anchor (`config/sources.yaml`).** Kept the topical OR
  vocabulary, ANDed a `(TITLE:pancrea* OR ABSTRACT:pancrea* OR TITLE:PDAC OR
  ABSTRACT:PDAC)` anchor so PDAC must be the subject, not merely cited. Live 2025
  hitCount 10,618 → 5,053 (−52%). (`MESH:` disjunct dropped — adds ~0 and breaks the
  OR parse when mixed with wildcards.) `openalex.search` pinned to the bare terms
  since EPMC field syntax isn't OpenAlex syntax. **Re-run `scripts/coverage_check.py`
  to refresh the provenance block.**
- **Retroactive cleanup = SOFT-FLAG, not delete.** Schema v5 adds `excluded` +
  `excluded_reason` to `papers` (migration in `db.init_schema`). Rows + vectors stay
  (reversible; no vectors.npz rebuild). `iter_papers(include_excluded=False)` drops
  them from the digest + Q&A retriever; the retriever's `_papers.get(pid)` miss skips
  their still-present vectors. Run via `scripts/cleanup_corpus.py`.
- **Scope = abstract-less only (conservative, zero false positive).** 2,037 flagged
  (1,979 abstract-less + 58 meta-title-with-abstract); corpus 46,570 → 44,533 active.
  Off-topic-WITH-abstract papers are left to the tightened query + the Q&A floor
  (`RETRIEVAL_MIN_SCORE`) — embedding similarity does NOT separate them cleanly from
  legitimate mechanism reviews, so no embedding cull.
- **Analytics is query-driven, not papers-driven.** `analytics.json`
  (coverage/share-of-voice) comes from `coverage_counts` (EPMC hitCounts in
  `backfill.py`), not the `papers` table — so flagging rows doesn't change it; the
  tighter query does. Re-ran `backfill --force` (topic + keywords) to recompute the
  series; share-of-voice ratios are ~stable (numerator and `_total` shrink together),
  the absolute "All PDAC papers" headline drops with the noise.

### Decisions resolved (2026-06-24) — ADRs (introduces `docs/adr/`)

This repo now keeps Architecture Decision Records in `docs/adr/`. Two are **Accepted**
(decision made; action items **not yet executed** — implementation pending). Both are
unlocked by HF PRO (2026-06) and are coupled (0002 runs inside 0001's Job).

- **ADR-0001 — weekly offline pipeline → HF Jobs.** Move `pipeline.run_weekly` off the
  GitHub Actions cron onto **HF Jobs** (same `0 13 * * 1` cron, same entrypoint, CPU
  first / GPU only on demonstrated need). Runner+scheduler change only — no change to
  harvest/normalize/score/digest, `config/*.yaml`, corpus schema, or the Space.
  `.github/workflows/weekly.yml` stays as documented fallback (dual-run during cutover).
- **ADR-0002 — cheap classifier + relevance note → HF Inference Providers.** Add a
  config-overridable `LLM_PROVIDER` / `CLASSIFIER_MODEL` (mirroring `EMBEDDING_MODEL`)
  for the two cheap offline scoring steps, **eval-gated** on `relevance_set.json`;
  `ANTHROPIC_API_KEY` kept as one-switch fallback. The Q&A answer model (`qa/answer.py`)
  is **explicitly out of scope** — groundedness gets its own eval-gated ADR.
