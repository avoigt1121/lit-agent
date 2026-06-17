"""
eval/run_eval.py — groundedness + digest-relevance graders (Phase 6).

Adapts research-coordinator/eval/run_eval.py to lit-agent's two trust metrics:

  qa         — Q&A GROUNDEDNESS. Runs each question through qa.retrieve + qa.answer,
               then grades with (1) a deterministic backstop — any DOI cited in the
               answer that was NOT in the retrieved passages is a hallucinated
               citation → FAIL — and (2) a trace-aware LLM judge that sees ONLY the
               retrieved passages as allowed evidence.
  relevance  — DIGEST RELEVANCE PRECISION. Runs each labeled paper through the
               classifier (pipeline.score) and scores predicted focus areas against
               human labels (precision / recall on "is it BCC-relevant", plus
               focus-area assignment accuracy).

Usage:
    python eval/run_eval.py qa
    python eval/run_eval.py relevance
    python eval/run_eval.py all

Outputs eval/results/<ts>_<mode>_{graded.json,report.md}.
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

RESULTS = Path(__file__).parent / "results"
RESULTS.mkdir(exist_ok=True)
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.I)


def _client():
    import anthropic
    return anthropic.Anthropic()


def _norm_doi(d: str) -> str:
    return (d or "").strip().lower().rstrip(").],;")


def extract_dois(text: str) -> set[str]:
    return {_norm_doi(m) for m in _DOI_RE.findall(text or "")}


def _judge(client, prompt: str) -> dict:
    resp = client.messages.create(model=JUDGE_MODEL, max_tokens=300,
                                  messages=[{"role": "user", "content": prompt}])
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"verdict": "PARTIAL", "reason": f"judge parse error: {raw[:160]}"}


# ---------------------------------------------------------------------------
# Q&A groundedness
# ---------------------------------------------------------------------------

QA_RUBRIC = """You grade a PDAC-literature Q&A assistant for GROUNDEDNESS. The \
assistant must answer ONLY from the retrieved passages (all are abstracts) and \
cite DOIs. Grade strictly.

Question: {question}
Expected behavior: {expected_behavior}
Grading notes: {notes}

--- RETRIEVED PASSAGES (the ONLY evidence the assistant was allowed to use) ---
{passages}
--- ANSWER ---
{answer}
--- END ---

Rubric:
- GROUNDED_ANSWER: every factual claim must be supported by the passages and the \
answer should cite DOIs. PASS if claims trace to the passages; FAIL if it asserts \
anything not in the passages or relies on outside knowledge.
- ABSTRACT_ONLY_GUARD: the question asks for methodological/quantitative detail \
that the abstracts do not contain. PASS if the assistant says the full text \
isn't available and summarizes only what the abstract states; FAIL if it \
fabricates methods/numbers.
- REFUSE_NO_EVIDENCE: the passages do not actually address the question. PASS if \
the assistant says it can't answer from the corpus; FAIL if it answers anyway \
from outside knowledge.

Respond with ONLY JSON: {{"verdict":"PASS|FAIL|PARTIAL","reason":"<one sentence>"}}"""


def _passages_for_judge(passages) -> str:
    # Give the judge the FULL passage text the answer was grounded in — truncating
    # here makes the judge flag in-abstract details as "fabricated" (false FAILs).
    out = []
    for i, p in enumerate(passages, 1):
        out.append(f"[{i}] DOI {p.doi or p.paper_id}: {p.title or ''}\n{p.text or ''}")
    return "\n\n".join(out) or "(none retrieved)"


def run_qa(bank: list[dict]) -> list[dict]:
    from qa.retrieve import Retriever
    from qa import answer as qa_answer
    retriever, client = Retriever(), _client()
    graded = []
    for i, q in enumerate(bank, 1):
        t0 = time.monotonic()
        passages = retriever.retrieve(q["question"], k=6)
        ans = qa_answer.answer(q["question"], passages, client)
        retrieved = {_norm_doi(p.doi) for p in passages if p.doi}
        cited = extract_dois(ans)
        unsupported = sorted(cited - retrieved)
        verdict = _judge(client, QA_RUBRIC.format(
            question=q["question"], expected_behavior=q["expected_behavior"],
            notes=q.get("notes", ""), passages=_passages_for_judge(passages),
            answer=ans[:6000]))
        v, reason = verdict.get("verdict", "PARTIAL"), verdict.get("reason", "")
        # deterministic backstop: a cited DOI that wasn't retrieved == hallucination
        if unsupported and v != "FAIL":
            v = "FAIL"
            reason = f"[citation backstop] cited DOI(s) not in retrieved set: {unsupported}. " + reason
        graded.append({**q, "answer": ans, "retrieved_dois": sorted(retrieved),
                       "cited_dois": sorted(cited), "unsupported_citations": unsupported,
                       "verdict": v, "reason": reason, "latency_s": round(time.monotonic() - t0, 1)})
        print(f"[{i}/{len(bank)}] {q['id']}: {v} ({graded[-1]['latency_s']}s)")
    return graded


def report_qa(graded: list[dict]) -> str:
    counts = {"PASS": 0, "FAIL": 0, "PARTIAL": 0}
    for r in graded:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    viol = sum(1 for r in graded if r["unsupported_citations"])
    lines = ["# Q&A Groundedness Eval", "",
             f"**{len(graded)} questions** — PASS {counts['PASS']}, PARTIAL {counts['PARTIAL']}, "
             f"FAIL {counts['FAIL']} · hallucinated-citation violations: {viol}", ""]
    for r in graded:
        emoji = {"PASS": "✅", "FAIL": "❌", "PARTIAL": "⚠️"}.get(r["verdict"], "?")
        lines += [f"## {emoji} {r['id']} — {r['expected_behavior']}",
                  f"**Q:** {r['question']}", f"**Verdict:** {r['verdict']} — {r['reason']}",
                  f"**Cited DOIs:** {r['cited_dois'] or '—'} · **unsupported:** {r['unsupported_citations'] or 'none'}",
                  "<details><summary>Answer</summary>", "", "```", r["answer"][:2500], "```", "</details>", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Digest relevance precision
# ---------------------------------------------------------------------------

def run_relevance(items: list[dict]) -> list[dict]:
    from pipeline.score import Embedder, classify_and_score, embed_corpus, load_interest_profile
    embedder, client = Embedder(), _client()
    profile = load_interest_profile(ROOT / "config" / "interest_profile.yaml")
    graded = []
    for i, it in enumerate(items, 1):
        rec = {"paper_id": it.get("doi") or it["id"], "title": it.get("title"),
               "abstract": it.get("abstract"), "ids": {}, "focus_areas": [], "relevance_score": 0.0}
        vecs = embed_corpus([rec], embedder)
        classify_and_score([rec], vecs, profile, embedder, client=client)
        predicted = rec["focus_areas"]
        expected = it.get("expected_focus_areas", [])
        graded.append({
            "id": it["id"], "doi": it.get("doi"), "title": it.get("title"),
            "is_relevant": bool(it["is_relevant"]), "expected_focus_areas": expected,
            "predicted_focus_areas": predicted, "predicted_relevant": bool(predicted),
            "area_match": sorted(set(predicted) & set(expected)),
        })
        print(f"[{i}/{len(items)}] {it['id']}: pred={predicted or '[]'} (label_relevant={bool(it['is_relevant'])})")
    return graded


def report_relevance(graded: list[dict]) -> str:
    tp = sum(1 for r in graded if r["predicted_relevant"] and r["is_relevant"])
    fp = sum(1 for r in graded if r["predicted_relevant"] and not r["is_relevant"])
    fn = sum(1 for r in graded if not r["predicted_relevant"] and r["is_relevant"])
    tn = sum(1 for r in graded if not r["predicted_relevant"] and not r["is_relevant"])
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    # area accuracy: among relevant+correctly-flagged, did predicted areas hit an expected one?
    rel = [r for r in graded if r["is_relevant"] and r["expected_focus_areas"]]
    area_hit = sum(1 for r in rel if r["area_match"])
    lines = ["# Digest Relevance Eval", "",
             "_Labels are provisional (seed set) — replace with BCC-confirmed labels for a real metric._", "",
             f"**{len(graded)} items** — relevance precision **{prec:.0%}** ({tp}/{tp+fp}), "
             f"recall **{rec:.0%}** ({tp}/{tp+fn}) · TP {tp} FP {fp} FN {fn} TN {tn}",
             f"**Focus-area hit rate** (relevant items whose predicted areas include an expected one): "
             f"{area_hit}/{len(rel)}", "", "| id | label | predicted areas | expected | ok |", "|---|---|---|---|---|"]
    for r in graded:
        ok = "✅" if (r["predicted_relevant"] == r["is_relevant"]) else "❌"
        lines.append(f"| {r['id']} | {'rel' if r['is_relevant'] else 'not'} | "
                     f"{','.join(r['predicted_focus_areas']) or '[]'} | "
                     f"{','.join(r['expected_focus_areas']) or '[]'} | {ok} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "qa"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jobs = {"qa": ("questions.json", run_qa, report_qa),
            "relevance": ("relevance_set.json", run_relevance, report_relevance)}
    todo = ["qa", "relevance"] if mode == "all" else [mode]
    for m in todo:
        fname, runner, reporter = jobs[m]
        bank = json.loads((Path(__file__).parent / fname).read_text())
        items = bank.get("questions") or bank.get("items") or []
        if not items:
            print(f"{m}: bank {fname} is empty — add cases first."); continue
        print(f"\n=== {m} eval ({len(items)} cases) ===")
        graded = runner(items)
        (RESULTS / f"{ts}_{m}_graded.json").write_text(json.dumps(graded, indent=2))
        report = reporter(graded)
        (RESULTS / f"{ts}_{m}_report.md").write_text(report)
        print(report.split("\n\n")[1] if "\n\n" in report else report[:300])
        print(f"-> eval/results/{ts}_{m}_report.md")


if __name__ == "__main__":
    main()
