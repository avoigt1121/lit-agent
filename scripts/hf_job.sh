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
# Auth / secrets:
#   - HF_TOKEN is forwarded from your local `hf auth login` via `--secrets HF_TOKEN`
#     (the same token the corpus pull/push needs) — no need to put it in the file.
#   - Everything else comes from a gitignored dotenv file (default: .env), mirroring
#     .env.example. On HF there is no macOS keychain / launchd, so for the Job that
#     file MUST contain at least ANTHROPIC_API_KEY and CORPUS_HF_DATASET. Secrets are
#     masked by HF; SEND_LIVE stays the live-send gate (default 0 = dry-run).
#
# Usage:
#   scripts/hf_job.sh run         # one-off run now (mirrors workflow_dispatch); streams logs
#   scripts/hf_job.sh schedule    # register the weekly cron scheduled Job
#   scripts/hf_job.sh ps          # list scheduled Jobs + status
#   scripts/hf_job.sh unschedule  # delete a scheduled Job (lists ids, prompts)
#
# Override via env (all have sensible defaults matching weekly.yml):
#   FLAVOR=cpu-basic  TIMEOUT=30m  IMAGE=python:3.11  REF=main  SECRETS_FILE=.env
#   REPO_URL=https://github.com/avoigt1121/lit-agent  CRON="0 13 * * 1"  NAMESPACE=
set -uo pipefail

FLAVOR="${FLAVOR:-cpu-basic}"            # ADR-0001: start at CPU parity; GPU is a flag away (e.g. FLAVOR=cpu-upgrade / a10g-small)
TIMEOUT="${TIMEOUT:-30m}"                # matches Actions `timeout-minutes: 30`
IMAGE="${IMAGE:-python:3.11}"            # matches Actions `python-version: "3.11"`
REPO_URL="${REPO_URL:-https://github.com/avoigt1121/lit-agent}"
REF="${REF:-main}"                       # main IS the deployed pipeline
SECRETS_FILE="${SECRETS_FILE:-.env}"     # gitignored; mirrors .env.example
CRON="${CRON:-0 13 * * 1}"               # Mondays 13:00 UTC — same cadence as weekly.yml

# What the Job runs: clone -> install -> run the SAME module the Actions job runs.
# &&-chained so any step failing aborts the Job with a non-zero exit.
BOOTSTRAP="git clone --depth 1 --branch ${REF} ${REPO_URL} /tmp/lit-agent \
&& cd /tmp/lit-agent \
&& pip install --no-cache-dir -q -r requirements.txt \
&& python -m pipeline.run_weekly -v"

require_cli() {
  command -v hf >/dev/null 2>&1 || {
    echo "error: 'hf' CLI not found. Install:  curl -LsSf https://hf.co/cli/install.sh | bash" >&2
    echo "       then authenticate:  hf auth login" >&2
    exit 1
  }
}

preflight_secrets() {
  if [ ! -f "$SECRETS_FILE" ]; then
    echo "error: secrets file '$SECRETS_FILE' not found." >&2
    echo "       copy the template and fill it in:  cp .env.example $SECRETS_FILE" >&2
    exit 1
  fi
  local key
  for key in ANTHROPIC_API_KEY CORPUS_HF_DATASET; do
    grep -qE "^[[:space:]]*${key}=" "$SECRETS_FILE" || \
      echo "warning: '$key' is not set (uncommented) in $SECRETS_FILE — the Job will likely fail without it." >&2
  done
}

# Shared hf-jobs flags: hardware, timeout, masked secrets file, forwarded HF token.
build_args() {
  ARGS=(--flavor "$FLAVOR" --timeout "$TIMEOUT" --secrets-file "$SECRETS_FILE" --secrets HF_TOKEN)
  [ -n "${NAMESPACE:-}" ] && ARGS+=(--namespace "$NAMESPACE")
}

case "${1:-}" in
  run)
    require_cli; preflight_secrets; build_args
    echo "-> one-off HF Job:  image=$IMAGE  flavor=$FLAVOR  timeout=$TIMEOUT  ref=$REF" >&2
    exec hf jobs run "${ARGS[@]}" "$IMAGE" bash -c "$BOOTSTRAP"
    ;;
  schedule)
    require_cli; preflight_secrets; build_args
    echo "-> scheduling weekly HF Job:  cron='$CRON'  flavor=$FLAVOR  ref=$REF" >&2
    exec hf jobs scheduled run "$CRON" "${ARGS[@]}" "$IMAGE" bash -c "$BOOTSTRAP"
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
    echo "usage: $0 {run|schedule|ps|unschedule}" >&2
    exit 2
    ;;
esac
