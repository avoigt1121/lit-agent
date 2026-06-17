# Deployment

Two deployable surfaces; mirrors how the Research Coordinator / DecoupleRpy were
hardened (dev surface first, dry-run email, then promote).

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
