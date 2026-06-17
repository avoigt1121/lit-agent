"""
pipeline/run_weekly.py — offline pipeline orchestrator.

Runs OFFLINE only (the weekly cron / locally) — never inside the Space. Full flow:

    pull durable corpus → harvest → normalize/dedup → embed → classify
        → persist (SQLite + index) → digest (DRY-RUN HTML) → deliver (if SEND_LIVE)
        → push durable corpus (HF Dataset)

Email is gated behind SEND_LIVE: without it, the digest is written to
out/digest_<date>.html and nothing is sent. `--test-send ADDR` sends one message
to a single address (explicit; still needs a provider key + verified sender).
Classification + relevance notes use an Anthropic key if present
(ANTHROPIC_API_KEY, e.g. in a local .env), else embedding-only + abstract notes.

Usage:
    python -m pipeline.run_weekly --from-spike --no-sync     # local: reuse spike, no hub
    python -m pipeline.run_weekly                            # cron: pull→harvest→…→push
    python -m pipeline.run_weekly --digest-only              # re-render from existing corpus
    python -m pipeline.run_weekly --from-spike --no-sync --test-send you@example.com
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from pathlib import Path

from pipeline.harvest import harvest_all, load_config
from pipeline.normalize import normalize_records
from pipeline.score import Embedder, classify_and_score, embed_corpus, load_interest_profile
from pipeline.digest import (build_digest_html, write_dry_run, load_recipients,
                             default_subject, deliver, send_test)
from pipeline import analytics
from store import db
from store.vectors import VectorIndex

logger = logging.getLogger("run_weekly")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
DEFAULT_INDEX = ROOT / "data" / "vectors.npz"
SPIKE_PATH = ROOT / "data" / "spike.json"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"
RECIPIENTS_PATH = ROOT / "config" / "recipients.yaml"


def _maybe_client():
    """Anthropic client if a key is set, else None (embedding-only fallback)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Anthropic unavailable (%s) — embedding-only classification.", exc)
        return None


def build_corpus(records: list[dict], window: dict, *, db_path: Path = DEFAULT_DB,
                 index_path: Path = DEFAULT_INDEX, do_embed: bool = True, client=None) -> dict:
    """Normalize → persist → embed → classify → persist tags. Returns a summary."""
    norm = normalize_records(records)
    deduped = norm["records"]
    for r in deduped:
        r["embedding_id"] = r["paper_id"]  # 1:1 for abstract embeddings

    conn = db.connect(db_path)
    db.init_schema(conn)
    new_ids = db.upsert_papers(conn, deduped)

    area_counts: dict[str, int] = {}
    if do_embed:
        embedder = Embedder()
        all_papers = list(db.iter_papers(conn))
        vectors = embed_corpus(all_papers, embedder)
        index = VectorIndex()
        for p in all_papers:
            index.add(p["embedding_id"], vectors[p["embedding_id"]])
        index.save(index_path)

        profile = load_interest_profile(PROFILE_PATH)
        tags = classify_and_score(all_papers, vectors, profile, embedder, client=client)
        db.upsert_papers(conn, all_papers)  # persist focus_areas + relevance_score
        for p in all_papers:
            db.set_topic_tags(conn, p["paper_id"], tags.get(p["paper_id"], {}))
            for aid in p.get("focus_areas") or []:
                area_counts[aid] = area_counts.get(aid, 0) + 1

    db.record_run(conn, date.today().isoformat(), window.get("from", ""),
                  window.get("to", ""), n_harvested=len(records),
                  n_new=len(new_ids), n_emailed=0)
    total = db.count_papers(conn)
    conn.close()
    return {"stats": norm["stats"], "n_new": len(new_ids), "corpus_total": total,
            "area_counts": area_counts, "db_path": str(db_path),
            "index_path": str(index_path) if do_embed else None}


def make_digest(window: dict, *, db_path: Path = DEFAULT_DB, client=None) -> tuple[Path, str]:
    """Render the dry-run digest + cache analytics. Returns (path, html)."""
    conn = db.connect(db_path)
    papers = list(db.iter_papers(conn))
    profile = load_interest_profile(PROFILE_PATH)
    adata = analytics.compute(conn)
    conn.close()
    analytics.cache(adata, ROOT / "data" / "analytics.json")
    footer = analytics.footer_html(adata, profile, "week")
    html_str = build_digest_html(papers, profile, window, client=client, analytics_html=footer)
    path = write_dry_run(html_str, out_dir=ROOT / "out", date_str=window.get("to"))
    return path, html_str


def _latest_window(db_path: Path) -> dict:
    conn = db.connect(db_path)
    row = conn.execute("SELECT window_from, window_to FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return ({"from": row[0], "to": row[1]} if row
            else {"from": "", "to": date.today().isoformat()})


# ---------------------------------------------------------------------------
# Durable store (HF Dataset) — pull at start, push at end of a cron run
# ---------------------------------------------------------------------------

def _hub_repo_token():
    return os.environ.get("CORPUS_HF_DATASET"), os.environ.get("HF_TOKEN")


def pull_from_hub(db_path: Path = DEFAULT_DB, index_path: Path = DEFAULT_INDEX) -> bool:
    """Download the existing corpus so the run accumulates week over week.
    No-op (fresh start) if creds/files are absent."""
    repo, token = _hub_repo_token()
    if not repo or not token:
        return False
    from huggingface_hub import hf_hub_download
    ok = False
    for remote, local in (("corpus.sqlite", db_path), ("vectors.npz", index_path)):
        try:
            p = hf_hub_download(repo_id=repo, repo_type="dataset", filename=remote, token=token)
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_bytes(Path(p).read_bytes())
            ok = True
        except Exception as exc:  # noqa: BLE001 — first run / missing file is fine
            logger.info("No %s in hub yet (%s) — starting fresh.", remote, exc)
    return ok


def sync_to_hub(db_path: Path = DEFAULT_DB, index_path: Path = DEFAULT_INDEX) -> bool:
    """Push the corpus to the durable HF Dataset repo. No-op with a warning if unset."""
    repo, token = _hub_repo_token()
    if not repo or not token:
        logger.warning("CORPUS_HF_DATASET / HF_TOKEN unset — skipping durable persist (local only).")
        return False
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True, private=True)
    api.upload_file(path_or_fileobj=str(db_path), path_in_repo="corpus.sqlite",
                    repo_id=repo, repo_type="dataset")
    if Path(index_path).exists():
        api.upload_file(path_or_fileobj=str(index_path), path_in_repo="vectors.npz",
                        repo_id=repo, repo_type="dataset")
    logger.info("Synced corpus to HF Dataset %s", repo)
    return True


def _deliver_step(window: dict, html_str: str, *, test_send: str | None, no_send: bool,
                  n_new: int | None = None) -> None:
    subject = default_subject(window, n_new=n_new)
    if test_send:
        print(f"Test send → {test_send}: id={send_test(html_str, subject, test_send)}")
        return
    if no_send:
        return
    recipients = load_recipients(RECIPIENTS_PATH)
    summary = deliver(html_str, subject, recipients)
    if summary["dry_run"]:
        print(f"Delivery: DRY-RUN (SEND_LIVE!=1) — {summary['recipients']} recipient(s) not emailed.")
    else:
        msg = f"Delivery: sent {summary['sent']}/{summary['recipients']}"
        if summary["errors"]:
            msg += f" | errors: {summary['errors']}"
        print(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline corpus + digest + delivery pipeline.")
    ap.add_argument("--from-spike", action="store_true", help="Load data/spike.json instead of harvesting.")
    ap.add_argument("--days", type=int, default=None, help="Harvest window override.")
    ap.add_argument("--no-embed", action="store_true", help="Skip embeddings/classification (dedup-only).")
    ap.add_argument("--no-digest", action="store_true", help="Skip the digest + delivery.")
    ap.add_argument("--digest-only", action="store_true",
                    help="Re-render digest + analytics from the existing corpus (no harvest/embed/classify).")
    ap.add_argument("--test-send", metavar="ADDR", default=None,
                    help="Send one test email to ADDR (explicit; needs provider key + EMAIL_SENDER).")
    ap.add_argument("--no-send", action="store_true", help="Render the digest but never attempt delivery.")
    ap.add_argument("--no-sync", action="store_true", help="Skip the HF Dataset pull/push.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    client = None if args.no_embed else _maybe_client()

    # --- digest-only: re-render from the existing corpus, optionally deliver ---
    if args.digest_only:
        window = _latest_window(args.db)
        path, html_str = make_digest(window, db_path=args.db, client=client)
        print(f"Digest (dry-run): {path}")
        if not args.no_digest:
            _deliver_step(window, html_str, test_send=args.test_send, no_send=args.no_send)
        return

    # --- full run ---
    if not args.no_sync:
        pull_from_hub(args.db, args.index)

    if args.from_spike:
        payload = json.loads(SPIKE_PATH.read_text())
    else:
        payload = harvest_all(load_config(), days=args.days)
    records, window = payload["records"], payload["window"]

    mode = "LLM-confirmed" if client else "embedding-only"
    result = build_corpus(records, window, db_path=args.db, index_path=args.index,
                          do_embed=not args.no_embed, client=client)

    print("\nDedup stats:")
    for k, v in result["stats"].items():
        print(f"  {k:32s} {v}")
    print(f"\nNew this run: {result['n_new']}  |  Corpus total: {result['corpus_total']}")
    if result["area_counts"]:
        print(f"Focus-area assignments ({mode}):")
        for aid, n in sorted(result["area_counts"].items(), key=lambda x: -x[1]):
            print(f"  {aid:28s} {n}")
    print(f"DB:    {result['db_path']}")
    if result["index_path"]:
        print(f"Index: {result['index_path']}")

    if not args.no_digest and not args.no_embed:
        path, html_str = make_digest(window, db_path=args.db, client=client)
        print(f"Digest (dry-run): {path}")
        _deliver_step(window, html_str, test_send=args.test_send, no_send=args.no_send,
                      n_new=result["n_new"])

    if not args.no_sync:
        sync_to_hub(args.db, args.index)


if __name__ == "__main__":
    main()
