# lit-agent — BCC Literature Review Agent

A **standalone** Literature Review Agent for the Brenden-Colson Center for
Pancreatic Care. It:

1. **Emails a weekly digest** of newly published open-access PDAC literature to a
   5–20-person list, each item tagged with the BCC focus area it serves.
2. **Reports coverage analytics** — most-covered PDAC topics over week / month / year.
3. **Runs a Hugging Face Space chat** that answers follow-up questions grounded
   in the papers it ingested, with DOI citations.

It is a separate system from DecoupleRpy / biodata-registry / the Research
Coordinator (different data domain — published papers, not transcriptomics).
Those repos are **structural references only** (see [CLAUDE.md](CLAUDE.md)); this
agent does not import or depend on them.

## Architecture

Two surfaces over one durable corpus:

- **Offline pipeline** (scheduled job): `harvest → normalize → score → persist →
  digest → analytics`. Slow, rate-limited, failure-prone API work lives here.
- **Online Space** (Gradio chat): grounded Q&A + cached analytics, loading the
  corpus **read-only**. It never ingests.

```
config/   interest_profile.yaml · sources.yaml · recipients.yaml   (config, not code)
pipeline/ harvest · normalize · score · digest · analytics · run_weekly
store/    db (SQLite) · vectors (embedding index)   — persisted to a durable store
qa/       retrieve · answer (grounding guard)
eval/     groundedness + digest-relevance graders
app.py · ui.py   — HF Space (Phase 5)
```

## Status (by build phase)

| Phase | What | State |
|------|------|-------|
| 0 | Spike — multi-source harvest → `data/spike.json` | ✅ done |
| 1 | Corpus — store + normalize/dedup + embed (local BGE) → SQLite + index | ✅ done |
| 2 | Digest (dry-run) — interest model + per-area HTML | ✅ done (LLM-confirmed) |
| 3 | Deliver — Resend mailer + cron + `SEND_LIVE` gate | ✅ wired (live pending domain + recipients) |
| 4 | Analytics — windows/deltas + cache | ✅ done |
| 5 | Q&A Space — retrieve + grounded answer + guard (Gradio) | ✅ done |
| 6 | Eval & harden — groundedness + relevance | ✅ done (Q&A 8/8 · relevance P100/R89) |
| 7 | Optional — register as a Coordinator specialist | not started |

## Quickstart (Phase 0 spike)

```bash
# Python 3.10+ required (gradio 5). HF Space runs 3.11.
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pipeline.harvest -v        # Phase 0: writes data/spike.json
.venv/bin/python -m pipeline.run_weekly --from-spike --no-sync   # build corpus + digest
.venv/bin/python -m pipeline.backfill --years 5 -v               # coverage trend history
.venv/bin/python app.py                        # Phase 5: Q&A Space at localhost:7860
```

Sources: Europe PMC REST (primary), PubMed E-utilities (MeSH; set `NCBI_API_KEY`
to lift the rate limit), bioRxiv/medRxiv (preprints, keyword-filtered client-side).
The PDAC query and per-source params are version-controlled in
[config/sources.yaml](config/sources.yaml) so coverage is reproducible.

## Conventions & boundaries

See [CLAUDE.md](CLAUDE.md). Highlights: OA-only for v1; answer only from real
retrieved evidence (no fabricated methods); "new" := `first_seen_date`; email
stays in dry-run until `SEND_LIVE`; focus areas / query / recipients live in
`config/*.yaml` so they change without a redeploy; never scrape a library proxy.

## Decisions

- **Embedding model:** local BGE-small via `fastembed` (ONNX, no API key) — `EMBEDDING_MODEL` to override.
- **Persistence host:** HF Dataset repo (`CORPUS_HF_DATASET`); pipeline pushes, Space pulls read-only.
- **Email:** Resend by default (`EMAIL_PROVIDER=postmark` to swap), sent from a verified subdomain; gated by `SEND_LIVE`.
- **"new":** `first_seen_date`.

Still open: OHSU TDM access (Q&A scope) · exemplar DOIs per focus area (optional booster).

## Roadmap (post-v1)

Scale to a broad set of focus areas surfaced two ways: **one Space tab per focus
area** (recent papers + analytics + area-scoped Q&A) and **per-recipient
focus-area subscriptions** for the email. v1 is built to make this a config/UI
change, not a rewrite — see CLAUDE.md "Post-v1 roadmap".
