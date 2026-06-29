#!/bin/bash
# HF Jobs runner for the weekly offline pipeline (ADR-0001).
#
# Migrates `python -m pipeline.run_weekly -v` off GitHub Actions onto HF Jobs, so
# the offline pipeline runs on HF infrastructure next to the corpus Dataset. Same
# entrypoint, same config/*.yaml, same env vars — RUNNER + SCHEDULER ONLY (see
# docs/adr/ADR-0001-hf-jobs-weekly-pipeline.md). The Space is untouched; this only
# *invokes* the pipeline, so there is no code coupling to the platform.
#
# How the code reaches the Job: it clones THIS repo (public) into a stock
# python:3.11 image, installs requirements.txt, and runs the module — mirroring the
# Actions steps 1:1 (checkout -> setup-python -> pip install -> run). No Dockerfile
# or packaging needed.
#
# Auth / secrets (all injected at SUBMIT TIME — a Job is a fresh container; it can
# NOT read Space secrets or any HF-side store):
#   - HF_TOKEN — forwarded from your local `hf auth login` via `--secrets HF_TOKEN`.
#   - ANTHROPIC_API_KEY — resolved here from the environment, else the macOS keychain
#     (mirrors scripts/run_weekly.sh), then forwarded via `--secrets ANTHROPIC_API_KEY`
#     (the VALUE rides in this process's env, never on the command line). Set it once:
#       security add-generic-password -s ANTHROPIC_API_KEY -a "$USER" -w 'sk-ant-...'
#     NOT needed when LLM_PROVIDER=hf (the cheap steps then authenticate with HF_TOKEN).
#   - Everything else (CORPUS_HF_DATASET, EMAIL_*, RESEND_API_KEY, SEND_LIVE, ...) comes
#     from an OPTIONAL gitignored dotenv file (default: .env) via `--secrets-file`.
#     SEND_LIVE stays the live-send gate (default 0 = dry-run).
#
# Usage:
#   scripts/hf_job.sh run         # one-off run now (mirrors workflow_dispatch); streams logs
#   scripts/hf_job.sh schedule    # register the weekly cron scheduled Job
#   scripts/hf_job.sh ps          # list scheduled Jobs + status
#   scripts/hf_job.sh unschedule  # delete a scheduled Job (lists ids, prompts)
#   scripts/hf_job.sh backfill    # ONE-OFF: LLM-classify the historical backlog (scripts/classify_backfill.py)
#   scripts/hf_job.sh annotate    # ONE-OFF: EPMC-annotation-enrich the corpus (scripts/annotate_backfill.py; keyless)
#
# `backfill` is a deliberately separate, ONE-OFF Job (`hf jobs run`, never `scheduled run`)
# — it is ~13k sequential LLM calls (~2-3h), resumable, and must NEVER ride the weekly cron.
# It runs `scripts.classify_backfill`, NOT `pipeline.run_weekly`, with a longer default timeout.
#
# Override via env (all have sensible defaults matching weekly.yml):
#   FLAVOR=cpu-basic  TIMEOUT=30m  IMAGE=python:3.11  REF=main  SECRETS_FILE=.env
#   REPO_URL=https://github.com/avoigt1121/lit-agent  CRON="0 13 * * 1"  NAMESPACE=
#   LLM_PROVIDER=hf  CLASSIFY_MODEL=…  NOTE_MODEL=…  (forwarded to the Job when set)
#   HF_XET_HIGH_PERFORMANCE=1        (ADR-0001: high-throughput Xet transfer; on by default, 0 to disable)
#   RUN_ARGS="--no-sync --no-send"   (extra flags appended to run_weekly, e.g. a smoke test)
#   BACKFILL_TIMEOUT=4h              (timeout for the `backfill` Job; the run is hours, not minutes)
#   BACKFILL_ARGS="--limit 100"      (extra flags appended to classify_backfill, e.g. a smoke test)
set -uo pipefail

FLAVOR="${FLAVOR:-cpu-basic}"            # ADR-0001: start at CPU parity; GPU is a flag away (e.g. FLAVOR=cpu-upgrade / a10g-small)
TIMEOUT="${TIMEOUT:-30m}"                # matches Actions `timeout-minutes: 30`
IMAGE="${IMAGE:-python:3.11}"            # matches Actions `python-version: "3.11"`
REPO_URL="${REPO_URL:-https://github.com/avoigt1121/lit-agent}"
REF="${REF:-main}"                       # main IS the deployed pipeline
SECRETS_FILE="${SECRETS_FILE:-.env}"     # OPTIONAL dotenv (non-keychain config); gitignored
CRON="${CRON:-0 13 * * 1}"               # Mondays 13:00 UTC — same cadence as weekly.yml

# What the Job runs: clone -> install -> run the SAME module the Actions job runs.
# &&-chained so any step failing aborts the Job with a non-zero exit.
BOOTSTRAP="git clone --depth 1 --branch ${REF} ${REPO_URL} /tmp/lit-agent \
&& cd /tmp/lit-agent \
&& pip install --no-cache-dir -q -r requirements.txt \
&& python -m pipeline.run_weekly -v ${RUN_ARGS:-}"

# Same clone+install, but the entrypoint is the ONE-OFF classify backfill (NOT the
# weekly pipeline). Resumable: a timeout-killed Job re-runs and skips processed papers.
BACKFILL_TIMEOUT="${BACKFILL_TIMEOUT:-4h}"
BACKFILL_BOOTSTRAP="git clone --depth 1 --branch ${REF} ${REPO_URL} /tmp/lit-agent \
&& cd /tmp/lit-agent \
&& pip install --no-cache-dir -q -r requirements.txt \
&& python -m scripts.classify_backfill -v ${BACKFILL_ARGS:-}"

# Same clone+install, entrypoint = the ONE-OFF EPMC-annotation backfill (ADR-0004).
# KEYLESS: the Europe PMC Annotations API needs no credential, so this Job needs only
# HF_TOKEN + CORPUS_HF_DATASET to pull/push the durable corpus — NO Anthropic key.
# Corpus-scale + resumable: a timeout-killed Job re-runs and skips processed papers.
ANNOTATE_TIMEOUT="${ANNOTATE_TIMEOUT:-4h}"
ANNOTATE_BOOTSTRAP="git clone --depth 1 --branch ${REF} ${REPO_URL} /tmp/lit-agent \
&& cd /tmp/lit-agent \
&& pip install --no-cache-dir -q -r requirements.txt \
&& python -m scripts.annotate_backfill -v ${ANNOTATE_ARGS:-}"

require_cli() {
  command -v hf >/dev/null 2>&1 || {
    echo "error: 'hf' CLI not found. Install:  curl -LsSf https://hf.co/cli/install.sh | bash" >&2
    echo "       then authenticate:  hf auth login" >&2
    exit 1
  }
}

# Resolve ANTHROPIC_API_KEY without a .env: prefer the environment, else the macOS
# keychain (the same item scripts/run_weekly.sh reads). Exporting it lets
# `--secrets ANTHROPIC_API_KEY` forward it BY NAME, keeping the value out of argv.
resolve_anthropic_key() {
  [ -n "${ANTHROPIC_API_KEY:-}" ] && return 0
  local v
  v="$(security find-generic-password -s ANTHROPIC_API_KEY -w 2>/dev/null || true)"
  [ -n "$v" ] && export ANTHROPIC_API_KEY="$v"
}

# Are the cheap LLM steps set to run on HF Inference Providers (no Anthropic key
# needed)? Reads LLM_PROVIDER from the LOCAL env — pass it to this script and it is
# forwarded to the Job (see build_args), so the two stay consistent.
_is_hf_provider() {
  case "$(printf '%s' "${LLM_PROVIDER:-anthropic}" | tr '[:upper:]' '[:lower:]')" in
    hf|huggingface|inference-providers) return 0 ;;
    *) return 1 ;;
  esac
}

_forward_anthropic="no"   # set by preflight_secrets, read by build_args

preflight_secrets() {
  # CORPUS_HF_DATASET drives the durable corpus pull/push (env or the dotenv file).
  if [ -z "${CORPUS_HF_DATASET:-}" ] && \
     ! { [ -f "$SECRETS_FILE" ] && grep -qE "^[[:space:]]*CORPUS_HF_DATASET=" "$SECRETS_FILE"; }; then
    echo "warning: CORPUS_HF_DATASET not in env or $SECRETS_FILE — the Job will NOT sync the durable corpus." >&2
  fi
  # Cheap-LLM credential: keychain/env Anthropic, unless running on HF providers.
  if _is_hf_provider; then
    echo "note: LLM_PROVIDER=hf — cheap steps use HF Inference Providers (HF_TOKEN); no Anthropic key needed." >&2
    return 0
  fi
  resolve_anthropic_key
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    _forward_anthropic="yes"
  else
    echo "error: ANTHROPIC_API_KEY not in the environment or the macOS keychain." >&2
    echo "       set it once:  security add-generic-password -s ANTHROPIC_API_KEY -a \"\$USER\" -w 'sk-ant-...'" >&2
    echo "       (or: export ANTHROPIC_API_KEY=... , or run with LLM_PROVIDER=hf to skip it)" >&2
    exit 1
  fi
}

# Shared hf-jobs flags: hardware, timeout, forwarded HF token, OPTIONAL dotenv, the
# keychain/env-resolved Anthropic key (forwarded by name), and any provider/model
# knobs set locally (so the Job's LLM_PROVIDER matches what preflight assumed).
build_args() {
  local k v
  ARGS=(--flavor "$FLAVOR" --timeout "$TIMEOUT" --secrets HF_TOKEN)
  # ADR-0001 transfer fix: the HF-Hub<->Job network is intermittently per-connection throttled
  # (~97 kB/s observed). huggingface_hub 1.x ships Xet (hf_xet, a hard dep) which accelerates the
  # model pull + corpus push and chunk-dedups the push (vectors.npz re-upload: 72.8 MB -> ~0.4 MB).
  # HF_XET_HIGH_PERFORMANCE adds parallel connections to ride out the per-connection throttle. On by
  # default, overridable (=0). (hf_transfer is deprecated/unused in hub 1.x — Xet replaces it.)
  ARGS+=(-e "HF_XET_HIGH_PERFORMANCE=${HF_XET_HIGH_PERFORMANCE:-1}")
  [ -f "$SECRETS_FILE" ] && ARGS+=(--secrets-file "$SECRETS_FILE")
  [ "$_forward_anthropic" = "yes" ] && ARGS+=(--secrets ANTHROPIC_API_KEY)
  for k in LLM_PROVIDER CLASSIFY_MODEL NOTE_MODEL HF_INFERENCE_PROVIDER; do
    v="${!k:-}"
    [ -n "$v" ] && ARGS+=(-e "$k=$v")
  done
  [ -n "${NAMESPACE:-}" ] && ARGS+=(--namespace "$NAMESPACE")
}

# Only run the dispatcher when executed directly — sourcing defines the functions
# (for tests) without submitting anything.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
case "${1:-}" in
  run)
    require_cli; preflight_secrets; build_args
    echo "-> one-off HF Job:  image=$IMAGE  flavor=$FLAVOR  timeout=$TIMEOUT  ref=$REF" >&2
    exec hf jobs run "${ARGS[@]}" -- "$IMAGE" bash -c "$BOOTSTRAP"
    ;;
  schedule)
    require_cli; preflight_secrets; build_args
    echo "-> scheduling weekly HF Job:  cron='$CRON'  flavor=$FLAVOR  ref=$REF" >&2
    # Only ever schedules pipeline.run_weekly (BOOTSTRAP). The classify backfill is
    # intentionally NOT schedulable here — it is a one-off (`backfill` case below).
    exec hf jobs scheduled run "$CRON" "${ARGS[@]}" -- "$IMAGE" bash -c "$BOOTSTRAP"
    ;;
  backfill)
    # ONE-OFF only — `hf jobs run`, never `scheduled run`. Longer timeout (hours).
    require_cli; preflight_secrets; TIMEOUT="$BACKFILL_TIMEOUT"; build_args
    echo "-> ONE-OFF classify-backfill HF Job (NOT scheduled):  flavor=$FLAVOR  timeout=$TIMEOUT  ref=$REF" >&2
    exec hf jobs run "${ARGS[@]}" -- "$IMAGE" bash -c "$BACKFILL_BOOTSTRAP"
    ;;
  annotate)
    # ONE-OFF EPMC-annotation backfill (ADR-0004). KEYLESS — skips the Anthropic
    # preflight entirely; only warns if CORPUS_HF_DATASET is absent (no durable sync).
    require_cli
    if [ -z "${CORPUS_HF_DATASET:-}" ] && \
       ! { [ -f "$SECRETS_FILE" ] && grep -qE "^[[:space:]]*CORPUS_HF_DATASET=" "$SECRETS_FILE"; }; then
      echo "warning: CORPUS_HF_DATASET not in env or $SECRETS_FILE — the Job will NOT sync the durable corpus." >&2
    fi
    TIMEOUT="$ANNOTATE_TIMEOUT"; build_args
    echo "-> ONE-OFF EPMC-annotation backfill HF Job (NOT scheduled):  flavor=$FLAVOR  timeout=$TIMEOUT  ref=$REF" >&2
    exec hf jobs run "${ARGS[@]}" -- "$IMAGE" bash -c "$ANNOTATE_BOOTSTRAP"
    ;;
  ps)
    require_cli
    hf jobs scheduled ps -a
    ;;
  unschedule)
    require_cli
    echo "Scheduled Jobs:"; hf jobs scheduled ps -a; echo
    read -r -p "scheduled-job-id to delete (blank to cancel): " id
    [ -n "${id:-}" ] && hf jobs scheduled delete "$id" || echo "cancelled."
    ;;
  *)
    echo "usage: $0 {run|schedule|ps|unschedule|backfill|annotate}" >&2
    exit 2
    ;;
esac
fi
