# Reference Repositories for Claude Code

**Context:** The Literature Review Agent is a *separate* system from the existing repos — different data domain, no runtime dependency. The existing repos are **architectural templates to copy from**, not systems this agent calls. Giving Claude Code these repos as read-only references turns "build me a literature agent" into "build me one that looks like the system we already trust."

---

## What each repo contributes

### `research-coordinator` — primary template
The closest structural match; lean on it hardest. Mirror almost 1:1:

- HF Space + Gradio chat skeleton — `app.py`, `gradio_ui.py`
- Streaming + transparency-panel pattern (Data / Code / Logic extraction)
- Config-driven design — `prompts.yaml`, `agents.yaml`
- **Eval harness** — `eval/run_eval.py`, question banks, trace-aware LLM judge, deterministic anti-fabrication backstop
- **Deploy machinery** — `.github/workflows/sync-to-hf-space.yml`, dev/prod split, secret naming, retry/backoff hardening

→ The Lit Agent's Space, eval, and deployment should mirror this structure.

### `biodata-registry` — data-layer template
The corpus store is the *same shape*: a pip-installable package wrapping a collection of items (papers instead of datasets) with manifests, loaders, and a `*_list_available`-style accessor.

→ Mirror its package structure for `store/` rather than inventing a new one.

### `DecoupleRpy_Agent` — pattern reference only (not a code template)
The reusable thing is the **groundedness discipline**: answer only from real evidence, surface the trace, re-read-before-reporting, refuse to fabricate — exactly what the Q&A guard needs.

→ Study the *approach*; do **not** copy its domain logic (decoupleR computation, dataset-selection heuristics don't apply).

---

## Priority for inclusion

| Rank | Repo | Include? | Use as |
|------|------|----------|--------|
| 1 | `research-coordinator` | **Definitely** | Space / eval / deploy template |
| 2 | `biodata-registry` | **Yes** | `store/` package model |
| 3 | `DecoupleRpy_Agent` | Optional / low-priority | Groundedness-pattern reference only |

---

## How to wire it up for Claude Code (most effective first)

1. **Put the reference repos on disk in the working tree.** Sibling directories, or a small workspace with `lit-agent/` next to read-only checkouts of the others. Claude Code reads local files far more reliably than a URL.

2. **Add a `CLAUDE.md`** to the new `lit-agent` repo that names what to mirror and points at specific files (template below).

3. **State the boundary explicitly:** these are *structural references, not dependencies* — do not add them to `requirements.txt`, do not call into them. This keeps the "standalone, no runtime integration" decision intact.

---

## `CLAUDE.md` template for the new `lit-agent` repo

```markdown
# lit-agent — build conventions

This repo is a STANDALONE Literature Review Agent. It does NOT integrate with
or depend on the repos below at runtime — they are STRUCTURAL REFERENCES ONLY.
Do not add them to requirements.txt and do not import from them.

## Mirror these patterns
- HF Space + Gradio chat, eval harness, and deployment:
  see ../research-coordinator
    - app.py, gradio_ui.py            (Space + streaming chat shell)
    - eval/run_eval.py                (eval harness + LLM judge design)
    - .github/workflows/sync-to-hf-space.yml  (deploy/sync workflow)
    - prompts.yaml, agents.yaml       (config-driven design)
- Corpus store package structure (manifests + loaders + list_available):
  see ../biodata-registry  → model store/ on this
- Groundedness discipline ONLY (no domain logic):
  see ../DecoupleRpy_Agent  → answer from real evidence, show trace,
                              refuse to fabricate

## Do not copy
- decoupleR / scanpy computation, dataset-selection heuristics,
  or any transcriptomics domain logic — different data domain.
```

---

*Companion to the Literature Review Agent — Technical Plan & Build Brief.*
