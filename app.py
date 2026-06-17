"""
app.py — Hugging Face Space entry point (Phase 5).

Mirrors research-coordinator/app.py: build the chat UI and launch. The Space
serves grounded Q&A + cached analytics ONLY — it NEVER ingests (ingestion is the
offline pipeline). The corpus store is loaded read-only at startup.

TODO(Phase 5):
    from ui import LitAgentUI
    LitAgentUI().build().launch(server_name="0.0.0.0", server_port=7860)
"""
