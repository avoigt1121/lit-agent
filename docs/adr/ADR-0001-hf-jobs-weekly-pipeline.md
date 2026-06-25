# ADR-0001: Run the Weekly Offline Pipeline on HF Jobs

**Status:** Accepted (2026-06-24)
**Date:** 2026-06-24
**Deciders:** Annie Voigt (project lead)
**Scope:** `lit-agent` offline pipeline (`pipeline/run_weekly.py`) and its scheduler. Enabled by HF PRO (2026-06). This is the first ADR for `lit-agent`; the repo is standalone, so this ADR governs only `lit-agent` and introduces `docs/adr/` here.

---

## Context

`lit-agent`'s architecture is explicitly **offline pipeline vs. online Space**: a
scheduled job ingests/scores/persists/emails; the Space only serves cached chat +
analytics and must never ingest on demand (CLAUDE.md hard constraints). Today that
job runs as a GitHub Actions cron:

- `.github/workflows/weekly.yml` — `schedule: cron "0 13 * * 1"` (Mondays 13:00 UTC),
  `runs-on: ubuntu-latest`, `pip install -r requirements.txt`, then
  `python -m pipeline.run_weekly -v`.
- Secrets/vars in GitHub: `ANTHROPIC_API_KEY` (classification + relevance notes),
  `NCBI_API_KEY`, `CORPUS_HF_DATASET` + `HF_TOKEN` (durable corpus pull/push),
  `EMAIL_PROVIDER`/`EMAIL_SENDER`/`RESEND_API_KEY`, and the `SEND_LIVE` dry-run gate.

This works, but it has friction the HF platform now removes:

- **Far from the data.** The job's first and last acts are *pull durable corpus →
  … → push corpus* against an HF Dataset (`CORPUS_HF_DATASET`). On GitHub Actions
  that is a full download/upload across providers every run; on HF infra it is
  same-platform.
- **CPU-only embeddings.** `score.py` embeds abstracts with local BGE-small via
  `fastembed` (ONNX, CPU) — a deliberate no-API-key choice. On `ubuntu-latest` there
  is no GPU option; as the corpus and weekly harvest grow, embedding is the runtime
  floor.
- **Two control planes.** Compute lives on GitHub; the data, the Space, and now (PRO)
  the natural place to schedule jobs all live on HF. Secrets are duplicated across
  GitHub and HF.

PRO unlocks **HF Jobs**: run a command on HF hardware (CPU → A100/TPU) with a
UV/Docker-like CLI, pay-as-you-go per second, and **native cron scheduling**
(`"0 13 * * 1"`, `@weekly`) plus repo-update **webhooks**. It is designed for exactly
this class of "data ingestion and processing" offline workload.

### Forces

- **Standalone must hold.** No new dependency on the reference repos; this is a
  runner change, not an architecture change.
- **Offline invariant must hold.** Jobs runs the pipeline *offline*, separate from
  the Space — fully consistent with "never ingest on-demand in the Space."
- **Config-not-code.** The query, focus areas, and recipients stay in `config/*.yaml`;
  the runner change must not move logic into code.
- **Dry-run safety.** The `SEND_LIVE` gate must survive the migration unchanged.
- **Reversibility.** GitHub Actions is a known-good fallback; the migration shouldn't
  burn it.

---

## Decision

Move the weekly offline pipeline from GitHub Actions to **HF Jobs**, scheduled with
the same cron, running the same entrypoint, reading the same `config/*.yaml`, on HF
infrastructure next to the corpus Dataset.

- **Runner:** `hf jobs` (UV flavor — `hf jobs uv run` installing `requirements.txt`,
  or a Docker image if the R/native deps grow), command `python -m pipeline.run_weekly -v`.
- **Schedule:** HF Jobs cron `"0 13 * * 1"` (unchanged cadence).
- **Hardware:** start on **CPU** (parity with today). Switch the embedding step to a
  small **GPU** flavor only if/when embedding wall-clock justifies it — a per-job flag,
  not a code change, and the reason a torch/GPU embedding backend could later replace
  the CPU `fastembed` path.
- **Secrets:** move the same env vars to HF Jobs secrets; `CORPUS_HF_DATASET`/`HF_TOKEN`
  now point at storage on the same platform.
- **Dry-run:** `SEND_LIVE` stays the live-send gate, default off.
- **Cutover:** run both for a transition (HF Jobs live, GitHub Actions on
  `workflow_dispatch` only) until one clean HF Jobs run is verified end-to-end, then
  disable the GitHub schedule. Keep `weekly.yml` in the repo as documented fallback.

## Consequences

**Easier**
- Corpus pull/push is same-platform (faster, fewer cross-provider failure modes).
- One control plane: data, schedule, and Space all on HF; secrets stop being
  duplicated across GitHub and HF.
- GPU embedding is a flag away when the corpus outgrows CPU `fastembed`.
- Webhook triggers become possible later (e.g. re-run on a config-repo update).

**Harder / risks**
- **Cost model changes** from "free GitHub minutes" to **pay-as-you-go per second**.
  Mitigation: CPU flavor + a ~30-min job weekly is small; set a timeout; watch
  Jobs billing.
- **New failure surface** (Jobs runtime, UV resolution, image). Mitigation: dual-run
  during cutover; keep the Actions workflow as fallback.
- **Standalone optics:** running on HF must not tempt importing platform-specific
  helpers into pipeline logic. Mitigation: Jobs only *invokes* `pipeline.run_weekly`;
  no code coupling.

**Non-goal (as originally scoped):** No change to harvest/normalize/score/digest logic,
to `config/*.yaml`, to the corpus schema, or to the Space. Runner + scheduler only.
*Revised 2026-06-25 (see Implementation findings):* completing a full run on the real
corpus required making `build_corpus`'s embed/classify **incremental**; per-paper
semantics are unchanged. Harvest/normalize/digest, config, schema, and the Space are
still untouched.

## Implementation findings (2026-06-25) — completing a full run under timeout

Bringing the runner up end-to-end surfaced that a full run (with the durable corpus pull
+ push) timed out at both the 30-min default and a 1-hour bump. The cause was
**two-layered, and only one layer was about transfer**:

**1. The dominant cause was redundant compute, not the network.** `build_corpus`
re-embedded *and* re-classified the **entire** corpus (46,570 papers) every run — and
built a *fresh* `VectorIndex`, discarding the one just pulled from the hub. On
`cpu-basic` the re-embed alone ran ~3.5 h (one job's logs: harvest done 03:31:56, first
classify call 07:08:23); then, with the Anthropic key present, classify makes **one LLM
call per paper** (~tens of thousands, sequential) — hours more. The `--no-sync` smoke
test hid this by starting from an empty corpus, so it only ever processed that run's
~100 new papers.
Fix (commit `8150495`): make both steps **incremental**. Embed only papers not already in
the pulled index (steady state = this run's new papers; the index already holds the census
corpus, so catch-up is zero). Classify only papers **new this run** — existing
`focus_areas`/`topic_tags` persist in SQLite from the census/prior runs; the historical
unclassified backlog stays the census's job, not the weekly digest's. `VectorIndex.get()`
lets classify reuse an already-embedded vector. Verified against the real 46,570-paper
corpus: a run adding 3 new papers finishes in 1.8 s (vs ~12 min re-embedding all), grows
the index by exactly 3, and leaves existing rows untouched.

**2. The HF-Hub↔Job network is intermittently per-connection throttled** (~97 kB/s
observed): worst case, the BGE-small model pull took 11:50 (vs ~1 s un-throttled) and an
un-fixed ~225 MB push could exceed 39 min. The accelerator that fixes this is **Xet**
(`hf_xet`, a hard dependency of `huggingface_hub` 1.x) — *not* `hf_transfer`, which 1.x
reports as deprecated and "not used anymore" (FutureWarning). Beyond raw throughput, Xet's
content-defined chunking **dedups the push**: re-uploading the mostly-unchanged
`vectors.npz` sent only ~0.4 MB instead of 72.8 MB — the "incremental upload" we'd
otherwise have hand-rolled (gzip/delta), for free and without touching the Space read
path. Set `HF_XET_HIGH_PERFORMANCE=1` (commit `3489521`, in `scripts/hf_job.sh`) for
parallel Xet connections to ride out the per-connection throttle (`corpus.sqlite`'s
churnier pages dedup less well, so high-perf mode is what keeps its push fast). `sync_to_hub`
uses the forwarded `HF_TOKEN` (env, via `_hub_repo_token`), confirmed from the run.

**Validated end-to-end (job `6a3d57825f9c8079e0fb3a9e`, dry-run, `cpu-basic`):** pull →
harvest → embed/classify only the 96 new papers (152 LLM calls, not ~44.5k) → digest
(dry-run) → Xet push (~2.5 s) — **COMPLETED in 5 min 21 s**, well under the 30-min timeout.
Work landed on branch `adr-0001-hf-jobs-throughput` (not yet merged to `main`).

## Alternatives considered

- **Stay on GitHub Actions.** Viable and free, but keeps compute away from the data,
  has no GPU path, and splits the control plane. Kept as fallback, not primary.
- **Schedule inside the Space (background thread / APScheduler).** Rejected — violates
  the offline-pipeline-vs-online-Space invariant and the ephemeral-Space reality.
- **HF Jobs via webhook instead of cron.** Deferred — the cadence is calendar-driven
  (weekly digest), so cron is the right primary trigger; webhooks are a later add for
  config-change re-runs.
- **GPU from day one.** Rejected — start at CPU parity; promote the embedding step to
  GPU only on demonstrated need (avoids paying for GPU on a CPU-bound-enough job).

## Action Items

1. [x] Author the HF Jobs invocation — `scripts/hf_job.sh` (`run` | `schedule` | `ps` |
   `unschedule`). **Implementation note:** uses `hf jobs run` with a stock `python:3.11`
   image that *clones this public repo + `pip install -r requirements.txt` + runs the
   module*, rather than `hf jobs uv run`. `uv run` executes a single script; our
   entrypoint is a package module (`python -m pipeline.run_weekly`) that needs the whole
   repo + `config/*.yaml`, and the repo has no pyproject/packaging — so clone-in-image is
   the faithful 1:1 mirror of the Actions checkout→install→run steps.
2. [ ] **(operator)** Provide secrets to the Job. `HF_TOKEN` (from `hf auth login`) and
   `ANTHROPIC_API_KEY` (from shell env, else the macOS keychain) are resolved + forwarded
   automatically by `hf_job.sh` — set the key once via `security add-generic-password -s
   ANTHROPIC_API_KEY -a "$USER" -w 'sk-ant-...'`, or skip it with `LLM_PROVIDER=hf`. The
   rest goes in an optional gitignored `.env`: `CORPUS_HF_DATASET` (required), `EMAIL_*`,
   `RESEND_API_KEY`, `NCBI_API_KEY`, `SEND_LIVE`.
3. [ ] **(operator)** Register the schedule: `scripts/hf_job.sh schedule` (cron
   `"0 13 * * 1"`, UTC — same as `weekly.yml`); confirm with `scripts/hf_job.sh ps`.
4. [ ] **Deferred to cutover (sequencing):** set `weekly.yml` to `workflow_dispatch`-only
   (remove the `schedule:` block) **only after** a clean scheduled HF run — flipping it
   now would leave a window with no scheduled run; never flipping means both crons fire
   and the digest double-sends. Command authored; intentionally not yet applied.
5. [x] **Verified one full HF Jobs run end-to-end** (`scripts/hf_job.sh run`, `SEND_LIVE=0`
   dry-run, job `6a3d57825f9c8079e0fb3a9e`): pull → harvest → embed/classify only the 96 new
   papers → digest dry-run → Xet push — **COMPLETED in 5m21s** on `cpu-basic`. Required the
   incremental + Xet fixes in "Implementation findings". Monitor via the API
   (`HfApi().inspect_job(...).status.stage`); the local `hf jobs run` log stream drops early
   with a misleading `exit=0`, and `hf jobs ps` crashes on the current CLI.
6. [x] **Embedding benchmark → `cpu-basic` is sufficient; no GPU needed.** The runtime floor
   was re-embedding the *whole* corpus every run, not per-paper speed. With incremental
   embedding (only new papers; the pulled index holds the rest) CPU finishes in minutes, so
   the GPU path stays a dormant `FLAVOR=` option rather than a requirement.
7. [x] Document HF Jobs as the runner — `DEPLOYMENT.md` §1b (cutover sequence) and the
   `CLAUDE.md` "Decisions resolved (2026-06-24) — ADRs" note name HF Jobs.

## References

- HF docs — Jobs overview (UV/Docker CLI, hardware, cron/webhook scheduling): <https://huggingface.co/docs/hub/en/jobs-overview>
- HF docs — Schedule Jobs: <https://huggingface.co/docs/hub/en/jobs-schedule>
- HF docs — Jobs pricing/billing: <https://huggingface.co/docs/hub/en/jobs-pricing>
- HF docs — PRO subscription: <https://huggingface.co/docs/hub/en/pro>
- Internal: `lit-agent/CLAUDE.md` (offline pipeline vs online Space; persistence host = HF Dataset; build order); `.github/workflows/weekly.yml` (current cron + env).
