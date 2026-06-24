"""
pipeline/llm.py — provider switch for the CHEAP offline LLM steps (ADR-0002).

The weekly pipeline makes three cheap, batch, latency-insensitive LLM calls:
  - focus-area classify-confirm  (pipeline/score.py:  confirm_with_llm)
  - per-item relevance note       (pipeline/digest.py: relevance_note)
  - per-area topic intro          (pipeline/digest.py: topic_intro)

All three receive ONE client (built in run_weekly._maybe_client) and call it as
``client.messages.create(model=…, max_tokens=…, messages=[{"role","content"}])``,
reading ``resp.content[0].text``. This module returns a client that honors that
Anthropic-style contract for whichever provider ``LLM_PROVIDER`` selects, so the
three call sites never change.

  LLM_PROVIDER=anthropic (default)  -> the real anthropic.Anthropic() client;
                                       behavior is byte-identical to before this ADR.
  LLM_PROVIDER=hf | huggingface     -> HF Inference Providers (PRO included credits),
                                       via a thin shim over InferenceClient.

Model ids stay configurable exactly as today via CLASSIFY_MODEL / NOTE_MODEL — set
them to the chosen open model when LLM_PROVIDER=hf. Returns None (the unchanged
key-free embedding-only fallback) when the selected provider has no credential.

OUT OF SCOPE (ADR-0002): the Q&A answer model (qa/answer.py QA_MODEL) and the Space
(ui.py) build their own clients and are deliberately NOT touched here — groundedness
gets its own eval-gated ADR.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("lit-agent.llm")

# Which upstream provider HF routes to ("auto" lets HF pick one that serves the model).
HF_INFERENCE_PROVIDER = os.environ.get("HF_INFERENCE_PROVIDER", "auto")
_HF_RETRIES = 2  # transient-error retries before falling back (ADR-0002 AI#2)


def cheap_client():
    """Return the client for the cheap offline steps, per ``LLM_PROVIDER``.

    None means "no usable credential" -> the call sites' existing key-free
    fallback (embedding-only classify; abstract-sentence notes) applies unchanged.
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider in ("hf", "huggingface", "inference-providers"):
        _warn_if_default_model_is_claude()
        return _hf_client()
    if provider not in ("anthropic", "", "claude"):
        logger.warning("Unknown LLM_PROVIDER=%r; falling back to anthropic.", provider)
    return _anthropic_client()


def _anthropic_client():
    """The original behavior: anthropic.Anthropic() if a key is set, else None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Anthropic unavailable (%s) — embedding-only classification.", exc)
        return None


def _hf_token() -> str | None:
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok
    try:  # the offline runner authenticates via `hf auth login` (no HF_TOKEN in env)
        from huggingface_hub import get_token
        return get_token()
    except Exception:  # noqa: BLE001
        return None


def _hf_client():
    token = _hf_token()
    if not token:
        logger.warning("LLM_PROVIDER=hf but no HF token (HF_TOKEN / `hf auth login`) — "
                       "embedding-only classification.")
        return None
    try:
        from huggingface_hub import InferenceClient
    except Exception as exc:  # noqa: BLE001
        logger.warning("InferenceClient unavailable (%s) — embedding-only classification.", exc)
        return None
    return _HFCheapClient(InferenceClient(provider=HF_INFERENCE_PROVIDER, api_key=token))


def _warn_if_default_model_is_claude() -> None:
    """LLM_PROVIDER=hf needs HF model ids; the claude defaults won't serve there."""
    for var in ("CLASSIFY_MODEL", "NOTE_MODEL"):
        val = os.environ.get(var, "")
        if not val or val.lower().startswith("claude"):
            logger.warning("LLM_PROVIDER=hf but %s=%r is unset/Anthropic — set it to an "
                           "HF model id (e.g. meta-llama/Llama-3.1-8B-Instruct) or the "
                           "step falls back to embedding-only.", var, val or "(default)")


# --- Anthropic-shaped response shims so call sites keep using resp.content[0].text ---

class _Block:
    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


class _Resp:
    __slots__ = ("content",)

    def __init__(self, text: str):
        self.content = [_Block(text)]


class _Messages:
    """Mimics ``anthropic_client.messages`` so call sites stay ``client.messages.create``."""

    def __init__(self, outer: "_HFCheapClient"):
        self._outer = outer

    def create(self, *, model: str, max_tokens: int = 256,
               messages: list[dict], **_ignored) -> _Resp:
        # Extra kwargs are ignored so this stays a strict superset of what the
        # call sites pass — they never send Anthropic-only params, but if they
        # ever did, we silently drop them rather than break the HF path.
        return self._outer._create(model=model, max_tokens=max_tokens, messages=messages)


class _HFCheapClient:
    """Anthropic-shaped shim over HF Inference Providers (cheap steps only).

    JSON for the classifier is enforced the same way the Anthropic path already
    does it — by the prompt ("JSON only") plus the caller's parse-or-empty
    validation in confirm_with_llm — not a server-side grammar (the shim is shared
    with the prose relevance/intro calls, so it can't force a global JSON mode).
    The shim adds retry on transient errors; on persistent failure it RAISES so
    each call site applies its own documented fallback (classify -> {}, notes ->
    abstract sentence). It never fabricates output.
    """

    def __init__(self, client):
        self._client = client
        self.messages = _Messages(self)

    def _create(self, *, model: str, max_tokens: int, messages: list[dict]) -> _Resp:
        last = None
        for attempt in range(_HF_RETRIES + 1):
            try:
                out = self._client.chat_completion(
                    messages=messages, model=model, max_tokens=max_tokens)
                return _Resp((out.choices[0].message.content or "").strip())
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.debug("HF chat_completion attempt %d/%d failed: %s",
                             attempt + 1, _HF_RETRIES + 1, exc)
        raise RuntimeError(f"HF inference failed after {_HF_RETRIES + 1} attempts: {last}")
