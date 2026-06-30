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
- **Retention (PRO storage):** keep corpus snapshots across runs rather than minimizing
  them. PRO's 1 TB private tier removes the old pressure to prune for space, so corpus
  history — including the schema-v5 `excluded` / `excluded_reason` soft-flag state — stays
  recoverable. The persistence mechanism is unchanged (pipeline pushes; Space pulls
  read-only); only the posture changes: retain, don't trim. Aligns with the cross-repo
  private-by-default storage decision (ADR-0005 in the `DecoupleRpy_Agent` repo).

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

### Research to-do backlog (from PI list, 2026-06-26)

Forward-looking feature requests pulled from the PI's research to-do list. Design
toward these; don't build yet.

- **Identify more specific OHSU research targets** tied to the newly ingested
  papers (link papers → BCC/OHSU lab interests).
- **Infer correlations between OHSU research and the papers** (relate each new
  paper to active OHSU work).
- **Citation tracking** — surface anything that cites the BCC's key reference
  paper(s) (seed: PubMed 39636224 — the Loveless et al. PDAC single-cell atlas).
  NOTE: this seed is a **Steele-lab** paper (Loveless/…/Steele NG), NOT
  Sears/BCC-authored. It is a tracked reference of interest, not a BCC-owned
  paper — do not describe it as "our"/"the BCC's own" paper.
- **Cross-field correlation alert** — flag correlations that span fields (e.g., a
  PDAC finding relating to the nervous system). Overlaps ADR-0003 (paper↔paper
  relationship layer).
- *Already done (2026-06-25):* "RNA-binding proteins" is now a focus area
  (`hur_elavl1` broadened to "RNA-binding proteins & mRNA regulation").

## Decisions still open (ask before assuming)

- OHSU TDM/API access (OA-only vs near-comprehensive Q&A)
- The actual BCC focus areas + exemplar DOIs to seed `config/interest_profile.yaml`

### Decisions resolved (2026-06-26) — EMAIL IS NOW LIVE + ADR-0001 cutover complete

- **Email provider/sender RESOLVED + live send ENABLED.** Provider = Resend,
  sender `BCC PDAC Digest <digest@send.anne-voigt.com>`. `config/recipients.yaml`
  is no longer empty — the real 4-person list is in: anne@anne-voigt.com,
  searsr@ohsu.edu, pelzc@ohsu.edu, brodyj@ohsu.edu (display names for the OHSU 3
  are best-guess from the handles except Sears — confirm/refine). `SEND_LIVE=1`
  added to the pipeline `.env` (gitignored). A verification live send (mode=`live`,
  no banner) to anne@anne-voigt.com confirmed the clean email; the group has NOT
  been emailed yet — first real send is the Monday cron.
- **Misleading recall % pulled from the email.** `digest.provenance_sentence()`
  now always states sources qualitatively; the self-measured ~76% recall figure is
  gone (it read as a completeness guarantee it can't back — union denominator +
  precision-tuned query). The `provenance:` block in `config/sources.yaml` stays
  (auto-written by `coverage_check.py`) but is marked INTERNAL-ONLY, never surfaced.
- **ADR-0001 cutover COMPLETE.** Re-registered the weekly HF scheduled job so it
  carries `SEND_LIVE=1` + `SPACE_URL` (the old id 6a3d720b… lacked both; **new id
  `6a3f1f02…`**, same `0 13 * * 1`, first live run **Mon 2026-06-29 13:00 UTC**).
  Old schedule deleted. `.github/workflows/weekly.yml` `schedule:` block RETIRED
  (kept `workflow_dispatch` as manual fallback) to avoid a double Monday run
  (corpus-push race + duplicate live email). Note: re-registering a schedule
  re-captures secrets at submit time via the Python API (`create_scheduled_job`),
  since the `hf` CLI errors (ioctl) in non-interactive shells.

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

This repo now keeps Architecture Decision Records in `docs/adr/`. Two are **Accepted**, and
**both implementations are in progress**. Both are unlocked by HF PRO (2026-06) and are
coupled (0002 runs inside 0001's Job). Per-action-item status lives in each ADR file, not here.

- **ADR-0001 — weekly offline pipeline → HF Jobs.** Move `pipeline.run_weekly` off the
  GitHub Actions cron onto **HF Jobs** (same `0 13 * * 1` cron, same entrypoint, CPU
  first / GPU only on demonstrated need). Runner+scheduler change, plus one scoped
  scoring-path fix (see Status); no change to harvest/normalize/digest, `config/*.yaml`,
  corpus schema, or the Space. `.github/workflows/weekly.yml` stays as documented fallback
  (dual-run during cutover).
  **Status (2026-06-25):** runner in `scripts/hf_job.sh`; the two throughput fixes are now
  **merged to `main`** (`8150495` — `build_corpus` embeds/classifies **only new papers**, the
  real multi-hour timeout cause, not transfer; `3489521` — **Xet** + `HF_XET_HIGH_PERFORMANCE`,
  not the deprecated `hf_transfer`, which also chunk-dedups the push, `vectors.npz` 72.8 MB →
  ~0.4 MB). A full dry-run COMPLETES in ~5 min on `cpu-basic` (validated, job `6a3d5782…`).
  **Weekly HF schedule now REGISTERED** (`scripts/hf_job.sh schedule`, 2026-06-25):
  scheduled-job id `6a3d720b81727949c74c224f`, cron `0 13 * * 1`, **first run
  `2026-06-29 13:00 UTC`**, dry-run (no `SEND_LIVE`). The GitHub Actions cron in `weekly.yml`
  stays ON until then (safe dual-run). Remaining = verify that first scheduled run COMPLETES
  cleanly, then remove `weekly.yml`'s `schedule:` block (keep `workflow_dispatch`) on a branch
  + merge with the lead.
  **Backlog backfill (companion to the "only new papers" fix — `scripts/classify_backfill.py`,
  `26e97a8`):** classifying only NEW papers each run leaves the census's already-ingested
  backlog unclassified — the census tagged the whole corpus with the EMBEDDING-ONLY path
  (`classify_and_score(client=None)`), so every row below the 0.68 floor sits at `focus_areas=[]`
  / `relevance_score=0.0`. This one-off re-runs JUST those rows through the cheap LLM (top-k +
  confirm — the real precision lever), reusing each paper's stored vector via `VectorIndex.get()`
  (**no re-embedding**), and persists `focus_areas` + `topic_tags`. Resumable via its own
  `classify_backfill_progress` table (an empty LLM result is valid, so neither `focus_areas` nor
  `relevance_score` can mark progress); WAL-checkpointed before each hub push; **gated out of the
  weekly cron** (own module, never imported by `run_weekly`; HF-Job mode is one-off `hf jobs run`
  / `hf_job.sh backfill`, never `scheduled run`). **Ran 2026-06-25: 13,304 processed → 1,715
  rescued with ≥1 area, 11,589 left empty (LLM confirmed none fit), pushed to
  `anne-voigt/bcc-lit-corpus`; re-run is a no-op (next-run todo = 0).** Why it matters: the
  post-v1 per-area Space tabs + analytics-by-area now see the whole corpus, not just the weekly
  trickle.
- **ADR-0002 — cheap classifier + relevance note → HF Inference Providers.** Add a
  config-overridable `LLM_PROVIDER` / `CLASSIFIER_MODEL` (mirroring `EMBEDDING_MODEL`)
  for the two cheap offline scoring steps, **eval-gated** on `relevance_set.json`;
  `ANTHROPIC_API_KEY` kept as one-switch fallback. The Q&A answer model (`qa/answer.py`)
  is **explicitly out of scope** — groundedness gets its own eval-gated ADR.
  **Status:** provider switch landed in `pipeline/llm.py` (`LLM_PROVIDER`, default
  `anthropic` = unchanged; `hf` routes classify + relevance/intro through HF Inference
  Providers via an Anthropic-shaped shim), wired at `run_weekly._maybe_client`. Remaining =
  eval candidate HF models on `relevance_set.json`, then flip the default only if precision
  holds. The cheap calls live in `score.py` + `digest.py` (not just `score.py`).

### Decisions resolved (2026-06-25) — digest tuning (lead feedback)

- **Share-of-voice leaderboard pulled from the EMAIL.** `analytics.footer_html`
  measured raw Europe PMC keyword-match VOLUME (per-area `count_query` hitCounts),
  not the curated digest, and read as a precision claim it couldn't back (keyword
  ambiguity inflates it). `make_digest` no longer renders it; the series is still
  computed + cached to `analytics.json` for the Space. The function stays in
  `analytics.py` (unused by the digest) for that future Space use.
- **"What's heating up" (keyword movers) KEPT, with a noise floor.** It's
  verifiable (each term links to the EPMC papers behind it), so it stays — but
  `analytics.keyword_movers` now requires a prior-year baseline ≥ `MOVER_MIN_PRIOR`
  (8) before assigning a %, so a small-base 2→8 blip no longer surfaces as "+300%".
- **Two too-specific focus areas broadened** (`config/interest_profile.yaml`); the
  other 6 unchanged. `id`s KEPT for corpus + `coverage_counts` continuity — only
  name/keywords/`count_query`/`audience_note` widened:
  - `myc`: "MYC" → **"Oncogenic drivers & gene regulation"** (MYC now a sub-topic).
  - `hur_elavl1`: "HuR (ELAVL1)" → **"RNA-binding proteins & mRNA regulation"**.
- **Per-area section now opens with a real OVERVIEW.** `digest.topic_intro` went
  from a one-sentence caption to a 2–3 sentence LLM synthesis of the week's papers
  in the area (themes/threads/where activity concentrates), grounded only in the
  shown titles+abstracts, rendered as a styled "This week" block. Designed as the
  hook for the post-v1 **cross-pollination with OHSU areas of interest** (a later
  prompt extension, not a structural change). Falls back to the static
  `audience_note` with no LLM key.

### Decisions resolved (2026-06-26) — full trend lists hosted on the Space

Follow-up to 2026-06-25: the lead likes BOTH trend blocks ("What's heating up" +
"Translational motion") but they were truncated (movers top 8, trials top 6) with
no "see all". Resolution = host the FULL lists on the Space + link from the email.

- **New Space tab "Trends & Translational Motion"** (`ui.py`): renders the full
  keyword-trend table (`analytics.movers_full_html`) and the full new-trials list
  (`clinicaltrials.translational_motion_full_html`) read-only from the offline
  caches (`data/analytics.json`, `data/translational_motion.json`) — the Space
  still never recomputes/ingests. Q&A moved under a sibling tab.
- **Caches now carry the full lists:** `run_weekly.make_digest` caches movers at
  `top_n=50` (all tracked terms per area; email re-caps to its top slice);
  `clinicaltrials.translational_motion` adds an `all` key (full compact list)
  alongside the email's `top`.
- **`SPACE_URL` (config, not code, env/.env):** when set, the email's two trend
  blocks append a "See all … on the site →" link to the Space tab; omitted
  gracefully when unset (local dry-runs show no dead link). The trials block keeps
  its existing ClinicalTrials.gov deep link regardless.
- **DEPLOYED (2026-06-26).** Space **`anne-voigt/bcc-lit-agent`** (PUBLIC, gradio
  sdk) is live + RUNNING at https://anne-voigt-bcc-lit-agent.hf.space (both tabs
  confirmed). `git remote space` = the Space; deploy is `git push space main`
  (force was used for the first push over HF's empty-repo seed). Space pulls the
  corpus + the two trend JSON caches from `anne-voigt/bcc-lit-corpus`
  (`pull_from_hub` / `sync_to_hub` now carry `analytics.json` +
  `translational_motion.json`; uploaded once manually for this first deploy).
  Space secrets: `HF_TOKEN`, `ANTHROPIC_API_KEY`; `CORPUS_HF_DATASET` is a Space
  **variable** (not secret). `SPACE_URL` set in the pipeline `.env` so the email's
  two trend blocks now render "See all … on the site →" (verified: 33 rising
  terms / 48 trials).
  **Gotcha:** the Space first showed `CONFIG_ERROR` "Collision on variables and
  secrets names" — a name set as BOTH a Space variable and a secret. Fix = pick
  one (deleted the `CORPUS_HF_DATASET` secret, kept the variable) and factory-reboot.

### Decisions resolved (2026-06-29) — "What's heating up" is now MONTHLY in the email

Lead feedback: the keyword-movers ("What's heating up") block analyzes a rolling
12-month window, so its week-over-week delta is tiny — surfacing it in every Monday
email is noise. Resolution = render it in the email **monthly only**, keep it on the
Space weekly.

- **`run_weekly._is_monthly_email(window)`** — True when the window end falls in days
  1–7 of the month (the month's first weekly cron run). `make_digest` renders the
  `keyword_movers_html` block only then; other weeks the email omits it.
- **No loss of access.** The FULL movers list is still cached to `analytics.json`
  **unconditionally** every run (`keyword_movers(top_n=50)` is unchanged), so the
  Space's "Trends & Translational Motion" tab stays current weekly. Only the inbox
  copy goes monthly.
- **Translational-motion (trials) block is UNCHANGED** — it stays weekly (it's a
  genuine new-registrations feed, not a 12-month rollup).

### Decisions resolved (2026-06-29) — corpus-side relationship DATA layer (ADR-0004)

Tier-3 (DATA) of the 3-tier cross-paper-inference roadmap. Built **offline-only**
on branch `feat/relationship-layer` — adds the corpus-side relationship substrate
future inference tools will read; **does NOT build the chat-facing tools** and does
**not touch the Space/chat (`app.py`, `ui.py`, `qa/`)**. See `docs/adr/ADR-0004`.
Complements (does not replace) ADR-0003: this layer is **structural/literal,
no-LLM, every edge a verifiable fact**; ADR-0003's `agreement/conflict/gap`
semantic edges are a separate later table.

- **Schema v6** (`store/db.py`, `SCHEMA_VERSION` 5→6, additive `CREATE IF NOT
  EXISTS` — no `papers`/`topic_tags` change, no vectors rebuild). Four tables +
  accessors:
  - `mentions` — **literal** entity index (`entity_type, entity, method, count`),
    DISTINCT from `topic_tags`: the `myc` focus tag is a classifier label, while a
    mention is the literal "MYC" in title+abstract. Makes "papers that MENTION MYC"
    answerable (`db.papers_mentioning`). `method ∈ {literal_scan, epmc_annotation}`.
  - `citation_edges` — directed citation graph keyed by EPMC `(source, ext_id)` so
    an edge survives an out-of-corpus endpoint; nullable `*_paper_id` resolve via
    `resolve_citation_endpoints` (pmid→paper_id) as papers are ingested.
  - `paper_relations` — derived undirected edges (`src<dst` canonical),
    `rel_type ∈ {shared_genes, shared_focus, citation}`, with `evidence` JSON.
  - `ohsu_interest_links` — paper↔OHSU-interest map (`seed_author|lab|focus_area`).
  - `relationship_progress (layer, paper_id)` — one resumable high-water table for
    all four populators (mirrors `census_progress`).
- **Four populators** (`pipeline/`), offline, **new-papers-only + resumable**:
  - `mentions.py` — literal-scan against a config lexicon (`tracked_keywords` +
    focus `keywords` + `extra_genes`). **Case-SENSITIVE for ≤4-char all-caps
    symbols** (MYC/KRAS/ATR) so English words (MAX/ARE/CAR) don't false-positive;
    word-boundary regex. Tested: `car`/`max` do NOT match.
  - `citations.py` — EPMC **sanctioned** citations/references REST (NOT scraping);
    tracks papers citing config `track_targets` — seed PMID **39636224** (Loveless
    et al. atlas, a **Steele-lab** reference of interest, NOT BCC-authored — never
    call it "ours"). Live-verified: 31 citers page 1. Corpus-refs pass opt-in/off.
  - `relationships.py` — deterministic edges from mentions+focus+citations,
    **bounded** (shared-gene candidates only, `max_neighbors` cap). The **cross-field
    correlation** signal falls out of `shared_genes` + differing `focus_areas`
    (tested: PDAC↔neuroscience paper via shared MYC/KRAS).
  - `ohsu_map.py` — STUB: surname-match vs `config/seed_authors.yaml` →
    `seed_author` links. Richer mappings add new `interest_kind` rows, no schema change.
- **Config** (`config/interest_profile.yaml` `relationships:` block) — lexicon,
  citation targets, edge thresholds; conservative defaults; no redeploy to tune.
- **Wiring**: `run_weekly._relationship_step` runs after classify (gated by
  `--no-relationships`, skipped under `--no-embed`), each layer isolated in
  try/except. Tables live in `corpus.sqlite` so the **existing** HF Dataset
  push/pull carries them — `sync_to_hub`/`pull_from_hub` unchanged.
- **Tests**: `tests/test_relationship_layer.py` (6 pass — schema, mention index +
  false-positive guard, resumability, cross-field edge, citation resolution +
  idempotency, OHSU mapping). **MERGED to `main` + deployed** (2026-06-29, alongside
  the EPMC annotation enrichment below — annotations depend on this layer's schema v6).
- **Next (operator)**: backfill the existing corpus a layer at a time
  (`python -m pipeline.mentions --all`, etc.). Read-side surfacing (qa/ + Space
  tabs) is deliberately deferred (Tier 1/2).

### Decisions resolved (2026-06-29) — EPMC annotation enrichment of the mention index

Follow-up to ADR-0004 on branch `feat/epmc-annotations` (off `feat/relationship-layer`).
The harvest only fetches `resultType=core` search fields, so `annotations.genes/diseases`
is empty on every corpus row — the literal index therefore saw only the ~30-80 curated
lexicon terms. This adds broad-recall coverage. **MERGED to `main` + deployed
(flag ON); corpus backfill running as a one-off HF Job.**

- **`pipeline/annotate.py`** — a SEPARATE, resumable populator (NOT folded into the
  weekly literal scan) that pulls Europe PMC's **text-mined annotations** for new,
  EPMC-addressable papers and writes them as `method='epmc_annotation'` rows in the
  same `mentions` table — every gene/disease/chemical EPMC's NLP tagged, on top of
  the curated `literal_scan` rows.
- **Sanctioned API only** (`Access ≠ scraping`): the EPMC **Annotations API**
  (`annotations_api/annotationsByArticleIds?articleIds=MED:<pmid>&type=…`), a public
  TDM service — NOT the core search endpoint. Reuses harvest's polite retrying session;
  batches ≤8 articleIds/request (the API cap). Live shape confirmed once vs pmid 39636224.
- **Non-destructive merge** via new `db.set_mentions_for_method(...)` — replaces only
  the paper's `epmc_annotation` rows, so `literal_scan` rows survive (and vice-versa);
  the two populators run independently, in either order.
- **Resumable**: new `relationship_progress` layer `'annotations'`; new-papers-only,
  per-run cap (`per_run_cap`, default 2000); non-EPMC papers marked done without a fetch.
- **Mapping**: EPMC `type`→`entity_type` (`Gene_Proteins`→gene, `Diseases`→disease,
  `Chemicals`→chemical, `Organisms`→organism; config-overridable), `entity` = literal
  `exact` span (grounded default) or preferred tag name (`entity_source: preferred`),
  `count` = #occurrences.
- **Config flag now ON**: `relationships.mentions.use_epmc_annotations: true` (plus
  `annotations.{types,entity_source,batch_size,per_run_cap}`). `run_weekly.
  _relationship_step` runs it **after `mentions`, before `relations`** (so
  `shared_genes` edges see the broader entity set), same per-layer try/except
  isolation. No `sync_to_hub` change.
- **Corpus backfill**: `scripts/annotate_backfill.py` (ONE-OFF; pull → loop
  `enrich_annotations` in `--push-every` chunks → push; resumable, HF-Job-timeout-safe,
  KEYLESS — only HF_TOKEN + CORPUS_HF_DATASET) + `scripts/hf_job.sh annotate` mode
  (one-off `hf jobs run`, NEVER scheduled, 4h timeout, skips the Anthropic preflight).
  Mirrors the `classify_backfill` / `hf_job.sh backfill` pattern.
- **NOTE: merging this also landed `feat/relationship-layer` (97172f6) on `main`** —
  the whole ADR-0004 DATA layer (mentions/citations/relations/ohsu) is now deployed,
  not just the annotation enrichment (annotations depend on its schema v6).
- **Tests**: `tests/test_epmc_annotations.py` (6 pass, network-free — mapping +
  occurrence counts + type filter + exact/preferred + merge writer preserves
  literal_scan + resumability + `papers_mentioning` on annotation-only entities);
  `test_relationship_layer.py` still 6/6.
- **Backfill trigger**: `scripts/hf_job.sh annotate` (watch Jobs billing + EPMC
  politeness; resumable so a timeout-killed Job just re-runs).
- **Backfill RAN 2026-06-29**: HF Job enriched 43,141/43,335 EPMC-addressable papers,
  **3,074,666 `epmc_annotation` mentions** written (~71/paper), pushed to
  `anne-voigt/bcc-lit-corpus`. The whole corpus now has broad-recall entity coverage.

### Decisions resolved (2026-06-30) — read-side entity capabilities (ADR-0004 Tier 1, first slice)

First read-side surfacing of the mentions index — the DATA layer now feeds the chat
+ Space. **Branch `feat/entity-read-side`; touches `qa/` + `ui.py` so it needs a Space
deploy (`git push space main`), unlike the offline-only layers.** Two SEPARATE capabilities:

- **(1) Entity-mention lookup in chat** — new planner tool `find_papers_mentioning`
  (`qa/planner.py`) wrapping `qa/corpus_qa.papers_mentioning_text`, which queries the
  `mentions` table (literal_scan ∪ epmc_annotation) for papers that LITERALLY mention a
  named gene/drug/disease, returns the total count + most-recent matches with DOIs.
  Distinct from `search_corpus` (semantic) and focus-area labels (classifier). Planner
  preamble updated to route "which/how many papers mention X" → this tool, conceptual
  "how does X work" → `search_corpus`. Optional `entity_type` enum (gene|disease|
  chemical|organism). Key-free degradation unchanged (planner only built with a key).
- **(2) Most-mentioned-entity leaderboards** — `pipeline/analytics.entity_leaderboards
  (conn, top_n)` + `entity_leaderboards_html()` (top genes/diseases/drugs by DISTINCT
  papers, via `db.mention_counts`). Rendered on the Space **Trends** tab. Computed ONCE
  at Space startup from the read-only corpus (same pattern as the retriever loading
  `_papers` — not a per-request recompute, so the "Space never recomputes" invariant
  holds) so it's **live the moment the Space deploys, no pipeline rerun needed**; ALSO
  cached into `analytics.json` by `run_weekly.make_digest` for the offline path.
- **Tests**: `tests/test_entity_capabilities.py` (5 pass, network/key-free — mention
  lookup incl. annotation-only entity + type filter + empty, planner tool exposure +
  dispatch, leaderboard distinct-paper counts + HTML). Full suite green.
- **Not yet built (still deferred)**: per-area Space tabs, ADR-0003 semantic edges,
  citation/cross-field/OHSU-map read surfaces.

### Decisions resolved (2026-06-29) — Q&A handles corpus/meta questions, not just topical

The Space chat only did semantic vector retrieval, so non-topical questions
("What are the new papers this week?", "how many papers?", "what topics are
covered?", "what can you do?") had no single matching abstract, fell below the
`RETRIEVAL_MIN_SCORE` floor, and hit the generic orientation message. Fixed by a
deterministic meta-intent router that runs BEFORE retrieval.

- **New module `qa/corpus_qa.py`** — rule-based, key-free `answer_meta()` answers
  four intents straight from SQLite (real rows, real DOIs — still grounded, no
  fabrication): `list_recent` (`first_seen_date` window, optional focus-area
  scope), `corpus_size` (`COUNT(excluded=0)` + 7/30-day deltas), `topic_breakdown`
  (`topic_tags` by area), `help`. Anything unmatched returns `None` → falls
  through to the existing grounded synthesis (the topical deep-dive path,
  unchanged).
- **Wired into `ui.py`** — both `_respond` (streaming chat) and `ask` (the
  `/ask` agent API) short-circuit to `answer_meta()` first; example questions +
  fall-through orientation updated to advertise the new abilities.
- **Disambiguation rule:** a bare "new" never triggers a listing ("any new
  liquid-biopsy *studies*?" is topical → retrieval); only an explicit time window
  or a listing verb (show/list/latest/newest/what's-new) does. Respects corpus
  invariants (`excluded=0`, "new" = `first_seen_date`); points users to the
  Trends tab for keyword movers. Verified against the live 44.6k corpus.
- **Gotcha (fixed):** `answer_meta` must open its OWN short-lived SQLite
  connection from `retriever.db_path`, not reuse `retriever.conn`. The retriever's
  connection is created at app startup; Gradio runs each request in a worker
  thread, and SQLite forbids cross-thread connection use — so the first version
  raised `ProgrammingError` on every meta question. (Topical retrieval never hit
  it: it reads an in-memory dict + the vector index, not the DB.) `ui.py` also
  wraps the call so a meta failure degrades to retrieval instead of crashing.
- **Gotcha (fixed) — `launch(ssr_mode=False)` in `app.py`:** Gradio 5.50
  auto-enables experimental SSR on Spaces; it left the chat Textbox value stale
  on the client, so a second submit re-sent the FIRST question — every follow-up
  returned the same answer regardless of input. Server routing was correct on all
  paths (verified the live `/ask` AND `_respond` endpoints return distinct
  answers); the bug was purely the SSR client. Disabling SSR restores the stable
  client-rendered path. Debug lever: drive the Space's real fns over raw HTTP —
  `GET /config` lists `dependencies[].api_name` (e.g. `_respond`), then
  `POST /gradio_api/call/<api_name>` + stream the result.

### Decisions resolved (2026-06-29) — LLM query planner replaces the brittle intent router (Tier 1)

The rule-based router (`qa/corpus_qa.py`) maps a question to ONE intent and treats
"listing" and "topical" as **mutually exclusive**, so a hybrid like "What new papers
this week mention MYC?" listed the whole week and ignored MYC (also: "MYC" is 3 chars,
below `_detect_area`'s ≥4 keyword threshold, and the `myc` area means "Oncogenic
drivers", not a literal mention). Fixed with an LLM **query planner** that composes
filters (topic + window + focus area + count) via Anthropic tool-use.

- **New module `qa/planner.py` (`QueryPlanner`).** Tool-use loop whose tools wrap
  EXISTING capabilities only (nothing new touches the corpus; Space still never
  ingests): `search_corpus`→`Retriever.retrieve` (semantic + `since` first_seen
  filter — this is what fixes the MYC hybrid: `search_corpus(query="MYC",
  since_days=7)`), `list_recent`/`corpus_stats`/`topic_breakdown`→the new
  `corpus_qa.*_text` wrappers, `get_paper`→`Retriever.retrieve(paper_id=…)`. The model
  plans calls, then answers ONLY from tool results under the SAME guard as
  `qa/answer.py` (`PLANNER_SYSTEM` = tool-planning preamble **+** verbatim
  `SYSTEM_GUARD`); `render_citations` runs over the passages the tools actually
  returned, so DOI/author/date are still never model-written. `focus_area` enum is
  built from the live profile so the model can only name real area ids.
- **Regex stays the cheap first pass.** `corpus_qa.answer_meta` still answers clear
  meta intents deterministically (key-free). NEW: `corpus_qa.is_hybrid()` —
  `answer_meta` now **defers** (returns `None`) a `LIST_RECENT` question that also
  carries a topical-constraint connective ("about/mentioning/involving/…"), so the
  hybrid routes to the planner instead of a bare list. `ui.py` builds a
  `QueryPlanner` only when BOTH a corpus and `ANTHROPIC_API_KEY` are present;
  **no-key degradation = unchanged** (deterministic meta + direct retrieval + raw
  passages). Both `_respond` and `ask` route topical/hybrid → planner, with a
  try/except fall-through to the old direct-retrieval path on any planner error.
- **Tier 2 (per-chat memory) hedge:** `QueryPlanner.run(messages)` already takes a
  messages/history list; v1 passes a single turn. Adding memory later = passing more
  turns, not a rewrite.
- **Streaming trade-off:** tool-use needs full assistant turns, so the planner
  resolves to a COMPLETE answer (a brief "Planning…" note shows while it works)
  rather than token-streaming. Groundedness > the streaming nicety; the `/ask` API
  was non-streaming already.
- **Verified** (mock client + fake retriever — no local corpus, gradio is Space-only):
  routing table (10 cases) passes; the MYC hybrid calls `search_corpus` WITH the
  `since` filter and renders a real DOI link; the iteration cap (`MAX_TOOL_ITERS=5`)
  forces a tool-free final answer; eval harness unaffected (it tests the answer path
  the planner reuses). NOT yet deployed to the Space (awaiting say-so).
