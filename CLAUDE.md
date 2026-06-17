# lit-agent — build conventions

A **standalone** Literature Review Agent for the BCC. It (1) emails a weekly digest of
new open-access PDAC literature to a 5–20-person list, (2) reports which topics were most
covered over week/month/year, and (3) runs as a Hugging Face Space chat that answers
follow-up questions grounded in the papers it ingested.

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
