# ADR-0002: Route the Cheap Classifier/Relevance Steps Through HF Inference Providers

**Status:** Accepted (2026-06-24)
**Date:** 2026-06-24
**Deciders:** Annie Voigt (project lead)
**Scope:** `lit-agent` offline scoring only — the multi-label focus-area **classifier** and the one-sentence **relevance note** (`pipeline/score.py`). Enabled by HF PRO (2026-06). The Q&A grounded-answer model (`qa/answer.py`) is **explicitly out of scope** here.

---

## Context

Two pipeline steps call an LLM via `ANTHROPIC_API_KEY`:

- **Classify (cheap model):** title+abstract → matching focus-area ids with 0–1
  confidence each (JSON only). Multi-label, already designed to grow with the
  area count.
- **Relevance note:** one sentence on why a paper matters to the audience, using only
  claims supported by the abstract.

Both are **offline**, **batch**, **latency-insensitive**, run inside the weekly
pipeline, and are deliberately specified as a *cheap* model. They are run over the
whole new-paper set each week (and over thousands of rows during census/backfill).

PRO includes **Inference Providers credits** (~$2/month of included usage, ~20× the
free tier, then pay-as-you-go) giving serverless access to open models (Llama, Qwen,
Mistral, etc.) through one HF-routed API. These cheap, structured, high-volume calls
are a natural fit for included credits rather than per-token Anthropic spend.

Crucially, this is **not** a proposal to touch the Q&A answer path. `qa/answer.py`
enforces the project's central guarantee — *answer only from retrieved passages with
DOI citations; never infer methods when only an abstract exists*. That surface is
correctness-critical and graded by `eval/run_eval.py`; its model choice is a separate
decision with a higher bar.

### Forces

- **Cost & consolidation.** High-volume, low-stakes calls are the right thing to move
  onto included credits; it also reduces the Anthropic dependency for the cheap path.
- **Config-not-code.** Model/provider selection must be an env/config switch, mirroring
  the existing `EMBEDDING_MODEL` override pattern — not hardcoded.
- **Quality is measurable, so gate on it.** A weaker open model could degrade
  classification precision or overstate relevance notes. `lit-agent` already has
  `eval/relevance_set.json` + `eval/questions.json`; the swap must be gated on them,
  not assumed safe.
- **Structured output.** The classifier requires strict JSON; the chosen model/route
  must reliably honor a JSON/grammar constraint.
- **Groundedness is sacred.** The "answer only from real evidence" constraint means
  the Q&A answer model is not casually swapped.

---

## Decision

Add a **configurable provider/model** for the two cheap offline steps and route them
through HF Inference Providers, gated behind the existing relevance eval. Leave the
Q&A answer model on its current (Anthropic) path until a separate, groundedness-gated
ADR says otherwise.

- **Introduce `CLASSIFIER_MODEL` / `LLM_PROVIDER` config** (env-overridable, same
  spirit as `EMBEDDING_MODEL`), defaulting such that behavior is unchanged until
  explicitly switched.
- **Call via the HF Inference Providers API** for classification + relevance notes,
  consuming PRO's included credits (pay-as-you-go beyond).
- **Enforce structured output** (JSON / grammar) for the classifier; keep the existing
  prompt skeletons verbatim.
- **Gate the switch on eval:** the candidate open model must match the current
  classifier within an agreed tolerance on `relevance_set.json` (per-area precision,
  §9.3) and not increase overstatement on relevance notes, before it becomes the
  default. Keep `ANTHROPIC_API_KEY` as an instant fallback via the same config switch.
- **Out of scope:** `qa/answer.py`. The grounded-answer model stays put pending a
  dedicated groundedness-eval ADR.

## Consequences

**Easier**
- High-volume weekly (and backfill) classification runs on included credits instead of
  metered Anthropic tokens.
- Provider/model becomes a config knob — easy A/B, easy rollback, no code change.
- One fewer hard external dependency on the cheap path.

**Harder / risks**
- **Quality regression** on classification/relevance if the open model is weaker.
  Mitigation: eval-gated rollout; per-area precision watch; one-switch fallback.
- **JSON reliability** varies by model/provider. Mitigation: require grammar/JSON mode;
  validate + retry; reject non-conforming output.
- **Credit exhaustion** during large backfills could spill to pay-as-you-go.
  Mitigation: batch sizing, cache by `doi`, monitor credit balance.
- **Two LLM providers** to keep configured. Mitigation: a single `LLM_PROVIDER` switch;
  document both in `.env.example`.

**Non-goal:** No change to the Q&A grounding guard, the prompt skeletons' wording, the
corpus schema, or what counts as "new" (`first_seen_date`).

## Alternatives considered

- **Keep everything on Anthropic.** Simplest; forgoes included credits and keeps the
  cheap, high-volume path on metered tokens. Rejected as the default but retained as
  the fallback.
- **Move the Q&A answer model too.** Rejected here — groundedness is the product's core
  promise and demands its own eval-gated decision; bundling it would raise the risk of
  this change without need.
- **Self-host the classifier (local small model in the Job).** Possible later (it's an
  offline Job), but adds model-management overhead; Inference Providers needs no hosting
  and the credits are already included. Revisit if call volume makes self-hosting cheaper.

## Action Items

1. [ ] Add `LLM_PROVIDER` / `CLASSIFIER_MODEL` config (env-overridable) wired into `pipeline/score.py`; default = current behavior.
2. [ ] Implement the HF Inference Providers call path with JSON/grammar-constrained output + validation/retry.
3. [ ] Document both providers in `.env.example`; keep `ANTHROPIC_API_KEY` as fallback.
4. [ ] Run `eval/run_eval.py` against `relevance_set.json` for candidate open models; record per-area precision vs the Anthropic baseline.
5. [ ] Flip the default only if within tolerance; otherwise keep Anthropic and document the gap.
6. [ ] Add a credit-usage check to the weekly Job (warn before pay-as-you-go spill on backfills).
7. [ ] Note in `CLAUDE.md` that the Q&A answer model is intentionally excluded pending a groundedness-eval ADR.

## References

- HF docs — Inference Providers: <https://huggingface.co/docs/inference-providers/>
- HF docs — PRO subscription (included inference credits): <https://huggingface.co/docs/hub/en/pro>
- Internal: `lit-agent/CLAUDE.md` (prompt skeletons; "answer only from real evidence"; multi-label classifier; §9.3 per-area precision); `eval/` (relevance + Q&A graders); ADR-0001 (the Job this runs inside).
