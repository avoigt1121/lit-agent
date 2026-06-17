"""
app.py — Hugging Face Space entry point (Phase 5).

Mirrors research-coordinator/app.py. On startup it best-effort pulls the latest
corpus from the durable HF Dataset (so the Space serves what the weekly pipeline
produced), then builds the chat UI and launches. The Space serves grounded Q&A +
cached analytics ONLY — it NEVER ingests.
"""
import logging

logging.basicConfig(level=logging.INFO)

# Pull the durable corpus (no-op locally / without CORPUS_HF_DATASET + HF_TOKEN).
try:
    from pipeline.run_weekly import pull_from_hub
    pull_from_hub()
except Exception as exc:  # noqa: BLE001 — fall back to whatever corpus is on disk
    logging.warning("corpus pull skipped: %s", exc)

from ui import LitAgentUI  # noqa: E402 — after the optional pull

if __name__ == "__main__":
    LitAgentUI().build().launch(server_name="0.0.0.0", server_port=7860)
