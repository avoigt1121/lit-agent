#!/bin/bash
# Weekly offline pipeline runner (invoked by the launchd agent, or by hand).
# Full run: pull durable corpus → harvest → normalize → embed → classify →
# persist → push corpus → (dry-run) digest → refresh coverage; then restart the
# Space so it re-pulls the fresh corpus.
#
# Auth: HF token comes from `hf auth login` (huggingface_hub.get_token, no .env).
# The Anthropic key is read from the macOS keychain if present, else from .env.
# One-time keychain setup (avoids .env):
#   security add-generic-password -s ANTHROPIC_API_KEY -a "$USER" -w 'sk-ant-...'
set -uo pipefail

PROJ="/Users/annivoigt/Documents/OHSU/lit-agent"
SPACE="anne-voigt/bcc-lit-agent"
cd "$PROJ" || exit 1

export CORPUS_HF_DATASET="anne-voigt/bcc-lit-corpus"
# Anthropic key from keychain (no .env) if not already in the environment.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  ANTHROPIC_API_KEY="$(security find-generic-password -s ANTHROPIC_API_KEY -w 2>/dev/null || true)"
  [ -n "$ANTHROPIC_API_KEY" ] && export ANTHROPIC_API_KEY
fi

mkdir -p logs
LOG="logs/weekly_$(date +%Y%m%d_%H%M%S).log"
{
  echo "=== weekly run $(date) ==="
  .venv/bin/python -m pipeline.run_weekly -v
  rc=$?
  echo "pipeline exit: $rc"
  if [ "$rc" -eq 0 ]; then
    echo "restarting Space $SPACE to re-pull corpus…"
    .venv/bin/python - <<'PY'
from huggingface_hub import restart_space, get_token
print(restart_space("anne-voigt/bcc-lit-agent", token=get_token()).stage)
PY
  fi
  echo "=== done $(date) ==="
} >> "$LOG" 2>&1
