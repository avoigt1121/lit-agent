# Deployment

Two deployable surfaces; mirrors how the Research Coordinator / DecoupleRpy were
hardened (dev surface first, dry-run email, then promote).

## Walkthrough (ordered)

You drive the account-level steps (HF/GitHub dashboards + secrets); the repo
already contains all the code + machinery. HF username assumed `anne-voigt`.

1. **Push to GitHub.** `git remote add origin <repo-url> && git push -u origin main`.
2. **Create the corpus HF Dataset** (private), e.g. `anne-voigt/bcc-lit-corpus`.
   Populate it once locally: set `CORPUS_HF_DATASET` + `HF_TOKEN` in `.env`, then
   `python -m pipeline.run_weekly --from-spike` (builds the corpus and pushes it),
   and `python -m pipeline.backfill --years 5` (coverage history).
3. **Create the HF Gradio Space** `anne-voigt/bcc-lit-agent` (SDK = gradio). HF
   reads the Space card from this repo's README frontmatter (`app_file: app.py`).
   Set Space **secrets** `ANTHROPIC_API_KEY`, `HF_TOKEN`; Space **variable**
   `CORPUS_HF_DATASET`. (`app.py` pulls the corpus from it at startup.)
4. **Auto-deploy the Space.** Add GitHub repo **secret** `HF_TOKEN` and
   **variables** `HF_USERNAME`, `HF_SPACE_ID` — then `sync-to-hf-space.yml`
   force-pushes the repo to the Space on every push to `main`.
5. **Schedule ingestion.** Add the `weekly.yml` secrets/vars (§1). Mondays it
   pulls the corpus → harvests → … → pushes the corpus → writes a dry-run digest.
6. **Go live on email** once `recipients.yaml` is filled and a `--test-send`
   looks right: set the `SEND_LIVE` repo variable to `1`.
7. **(Optional) Phase 7** — apply [INTEGRATION.md](INTEGRATION.md) to
   research-coordinator so it routes literature questions to lit-agent.

Per-surface detail follows.

## 1. Offline pipeline (GitHub Actions cron) — Phase 3

`.github/workflows/weekly.yml` runs `pipeline/run_weekly.py` weekly (Mondays
13:00 UTC) and on demand: pull durable corpus → harvest → normalize → embed →
classify → persist → digest → deliver → push.

**Repo secrets** (Settings → Secrets): `ANTHROPIC_API_KEY`, `HF_TOKEN`,
`RESEND_API_KEY`, and optionally `NCBI_API_KEY` (lifts the PubMed rate limit).
**Repo variables** (Settings → Variables): `CORPUS_HF_DATASET`, `EMAIL_PROVIDER`
(`resend`), `EMAIL_SENDER`, and `SEND_LIVE`.

**Live-send gate:** the digest stays DRY-RUN — writing `out/digest_<date>.html`
and sending nothing — until the `SEND_LIVE` variable is exactly `1`. Verify a
real send first with `python -m pipeline.run_weekly --from-spike --no-sync
--test-send you@example.com`.

### Email setup (Resend + Cloudflare DNS)

The provider is config (`EMAIL_PROVIDER`, default `resend`; `postmark` also
supported). Resend and Cloudflare don't conflict — Cloudflare Email Routing is
inbound-only, Resend is outbound. Recommended:

1. In Resend, add a **sending subdomain** (e.g. `send.your-domain.com`) — not the
   root — so its records don't collide with root-domain inbound routing.
2. Add the records Resend shows to **Cloudflare DNS** for that subdomain: a DKIM
   `TXT`/`CNAME`, an SPF `TXT`, and a return-path `MX`. Set them **DNS-only
   (grey cloud), not proxied**.
3. Set `EMAIL_SENDER="BCC Lit Digest <digest@send.your-domain.com>"` and
   `RESEND_API_KEY`. Recipients live in `config/recipients.yaml`.

Access ≠ redistribution: link out + short fair-use snippets; never embed licensed
full text in the email.

## 1b. Offline pipeline on HF Jobs (ADR-0001 — target runner)

[ADR-0001](docs/adr/ADR-0001-hf-jobs-weekly-pipeline.md) moves this same cron off
GitHub Actions onto **HF Jobs**, so the pipeline runs on HF infrastructure next to
the corpus Dataset (same-platform pull/push, a GPU flavor a flag away). It is a
runner + scheduler change only — same `python -m pipeline.run_weekly -v`, same
`config/*.yaml`, same env vars. `weekly.yml` stays in the repo as fallback.

`scripts/hf_job.sh` drives it. The Job clones this (public) repo into `python:3.11`,
`pip install -r requirements.txt`, and runs the module — mirroring the Actions steps.

**Prereqs:** install the CLI (`curl -LsSf https://hf.co/cli/install.sh | bash`) and
`hf auth login`. Two credentials are resolved + forwarded **automatically** by
`hf_job.sh`, so they need not live in any file: **`HF_TOKEN`** (from your login) and
**`ANTHROPIC_API_KEY`** (from your shell env, else the macOS keychain — set once with
`security add-generic-password -s ANTHROPIC_API_KEY -a "$USER" -w 'sk-ant-...'`; or skip
it entirely by running with `LLM_PROVIDER=hf`, which authenticates the cheap steps with
`HF_TOKEN`). Put the rest in an **optional** gitignored `.env` (from `.env.example`):
`CORPUS_HF_DATASET` (required for corpus sync), `EMAIL_PROVIDER`, `EMAIL_SENDER`,
`RESEND_API_KEY`, `NCBI_API_KEY`, `SEND_LIVE`. Secrets are masked by HF; `SEND_LIVE`
stays the live-send gate. A Job is a fresh container — it can't read Space secrets.

**Cutover (run in this order — do NOT flip the Actions schedule until HF is proven):**

1. **Verify one full run** end-to-end on HF (dry-run, `SEND_LIVE=0`):
   `scripts/hf_job.sh run` — streams logs; exits non-zero on failure. Confirms
   corpus pull → harvest → embed → persist → digest (dry-run) → push.
2. **Register the weekly schedule:** `scripts/hf_job.sh schedule` (cron `0 13 * * 1`,
   UTC — same cadence as `weekly.yml`). Check it with `scripts/hf_job.sh ps`.
3. **Only after a clean scheduled run, retire the duplicate cron:** edit
   `.github/workflows/weekly.yml` to keep `workflow_dispatch` but **remove the
   `schedule:` block** (leaving it as a manual fallback). Doing this earlier would
   leave a window with no scheduled run; doing it never means both crons fire and the
   digest goes out twice.

**Hardware / cost:** defaults to `cpu-basic` (parity with Actions) and a 30-minute
timeout. Promote the embedding step with `FLAVOR=cpu-upgrade scripts/hf_job.sh …` (or
a GPU flavor) only once wall-clock justifies it — a flag, not a code change. HF Jobs
is pay-as-you-go per second, so watch Jobs billing; a weekly ~30-min CPU job is small.

## 2. Online Space (Hugging Face) — Phase 5

`app.py` builds `ui.py` and launches on port 7860 (Python 3.10+ — gradio 5; HF
runs 3.11). On startup it `pull_from_hub()`s the corpus (SQLite + embedding
index) and serves it **read-only** — grounded Q&A (DOI-cited, abstract-only
guard) + cached analytics. It NEVER ingests. Space secrets: `ANTHROPIC_API_KEY`
(answers) and `CORPUS_HF_DATASET` + `HF_TOKEN` (to pull the corpus). Without a
key it falls back to returning the retrieved passages unsynthesized. Run locally
with `python app.py`.

### Sync workflow — to reconstruct in Phase 3

The reference repos document a `.github/workflows/sync-to-hf-space.yml` that
force-pushes `origin/main` → the `hf` Space remote on every push to `main`
(dev/prod split, retry/backoff, `HF_TOKEN` repo secret). **That file is not
present in the local reference checkouts**, so it will be reconstructed from the
documented behavior rather than copied. Plan: a dev Space first, promote to prod
after the dry-run digest and Q&A eval look right.

## 3. Persistence — HF Dataset repo (decided)

HF Space storage is ephemeral, so the corpus lives in a private **HF Dataset
repo** (`CORPUS_HF_DATASET`, e.g. `anne-voigt/bcc-lit-corpus`). Each cron run
`pull_from_hub()`s `corpus.sqlite` + `vectors.npz` at the start (so it
accumulates week over week), and `sync_to_hub()`s them at the end. The Space
(Phase 5) pulls the same files read-only at startup. Both no-op without
`CORPUS_HF_DATASET` + `HF_TOKEN`, so local runs stay self-contained.

## Local dev

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in keys; .env is git-ignored
.venv/bin/python -m pipeline.harvest -v
```
