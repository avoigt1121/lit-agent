# Phase 7 — optional integration with research-coordinator

lit-agent stays **standalone**. This is the *optional* seam that lets the
research-coordinator route literature questions to it and relay the answer. It
is **ready to apply but not applied** — do it only after lit-agent is deployed,
because the coordinator would otherwise dispatch to a Space that doesn't exist.

lit-agent already exposes the integration endpoint: a one-shot **`/ask`** API
(`ui.py`), callable by any client and verified via `gradio_client`:

```python
from gradio_client import Client
Client("anne-voigt/bcc-lit-agent").predict("what's new on KRAS G12D resistance?", api_name="/ask")
# -> a grounded, DOI-cited answer string
```

## Prerequisite
Deploy lit-agent to an HF Gradio Space (Space secrets: `ANTHROPIC_API_KEY`, and
`CORPUS_HF_DATASET` + `HF_TOKEN` so `app.py` pulls the corpus). Confirm `/ask`
responds via the snippet above.

## Step 1 — register the agent
Add [integration/agents_entry.yaml](integration/agents_entry.yaml) under
`agents:` in `research-coordinator/agents.yaml`, with `hf_space` set to the
deployed slug.

## Step 2 — teach the router about it (prompts.yaml, not router.py)
In `research-coordinator/prompts.yaml` `routing_prompt`:
- widen the agent id: `"agent_id": null | "decouplerpy"` → `… | "litagent"`
- add a routing rule:
  > Route to "specialist" (agent_id: "litagent") for questions about **recent or
  > published literature** — "what's new on X", "recent papers/findings on X",
  > "review the literature", "what has been published" — i.e. answered from
  > publications, not from transcriptomic computation on the registered datasets
  > (those stay with `decouplerpy`).

## Step 3 — one small dispatch change (honest caveat)
The plan envisioned "one agents.yaml entry, no router.py changes," but the
coordinator's `dispatch_to_specialist_stream` is **hardcoded to DecoupleRpy's
API** (`/lambda` → `/interact_with_agent` → `/handle_continue`, plus its
chatbot-history/panel parsing). lit-agent speaks a simpler protocol, so the
coordinator needs a small, additive branch keyed on the agent's `api.type`
(it leaves the DecoupleRpy path untouched):

```python
# near the top of dispatch_to_specialist_stream, after resolving `agent`/`hf_space`:
api = agent.get("api", {})
if api.get("type") == "simple_predict":
    gc = GradioClient(hf_space, **client_kwargs)
    answer = gc.predict(message, api_name=api.get("api_name", "/ask"))
    yield (answer, answer, {}, True)   # (display_text, trace, panels, done)
    return
# else: existing DecoupleRpy two-step dispatch …
```

(Alternative if you truly want zero coordinator code change: have lit-agent's
Space mimic DecoupleRpy's `/lambda` + `/interact_with_agent` + `/handle_continue`
contract — not recommended; it couples a simple RAG chat to the CodeAgent
protocol.)

## Step 4 — verify
Through the coordinator UI, ask *"what are the most recent papers on KRAS G12D
resistance?"* → it should route to `litagent`, call `/ask`, and relay the
grounded, DOI-cited answer. A computation question ("run DE between …") must
still route to `decouplerpy`.

## Boundary
This does not make lit-agent depend on the coordinator: lit-agent runs fully on
its own. The only artifacts here are config + docs for the *coordinator* side;
nothing in lit-agent imports it.
