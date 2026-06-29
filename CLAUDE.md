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
- **Citation tracking** — surface anything that cites the BCC's own paper(s)
  (seed: PubMed 39636224).
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
