"""
pipeline/digest.py — compose + render HTML + send the weekly digest (Phase 2/3).

Phase 2 (dry-run, this file): select top-ranked items per focus area, write a
one-sentence per-item relevance note grounded ONLY in the abstract (LLM when an
Anthropic key is present; otherwise the abstract's first sentence — never
fabricated), render email-safe HTML, and write out/digest_<date>.html. No send.

Phase 3 (deliver): send via a transactional provider (key + sender from env),
recipients from config/recipients.yaml. A --dry-run guard plus a SEND_LIVE env
flag keep live sending explicit; the digest stays dry-run until SEND_LIVE=1.
Licensing: link out + short fair-use snippets; never embed licensed full text.

Roadmap (CLAUDE.md "Post-v1 roadmap"): the digest is composed as INDEPENDENT
per-focus-area sections, so a per-recipient email is just a filtered subset of
sections (recipients.yaml may carry an optional per-recipient `focus_areas`).
v1 sends everyone the full digest.
"""
from __future__ import annotations

import html
import os
import re
from datetime import date
from pathlib import Path

import requests
import yaml

NOTE_MODEL = os.environ.get("NOTE_MODEL", "claude-haiku-4-5-20251001")


def _esc(s) -> str:
    return html.escape(str(s)) if s else ""


def _first_sentence(text: str | None, limit: int = 280) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return (s[:limit].rstrip() + "…") if len(s) > limit else s


def _authors(rec: dict, n: int = 3) -> str:
    a = rec.get("authors") or []
    return ", ".join(a[:n]) + (" et al." if len(a) > n else "") if a else ""


def _link(rec: dict) -> str | None:
    return rec.get("oa_fulltext_url") or (f"https://doi.org/{rec['doi']}" if rec.get("doi") else None)


def relevance_note(rec: dict, area: dict, client=None) -> str:
    """One sentence on why this paper matters to the area — grounded in the abstract.

    LLM-written when a client is present (CLAUDE.md relevance-note prompt); else
    the abstract's first sentence. Never asserts anything not in the abstract.
    """
    abstract = rec.get("abstract")
    if client is not None and abstract:
        try:
            note = " ".join((area.get("audience_note") or "").split())
            prompt = (
                f"In ONE sentence, explain why this paper matters to: {note}\n"
                "Use ONLY claims supported by the abstract below. No overstatement, "
                "no hype, no claims the abstract does not make.\n\n"
                f"Title: {rec.get('title')}\nAbstract: {abstract[:2500]}\n\nOne sentence:")
            resp = client.messages.create(
                model=NOTE_MODEL, max_tokens=140,
                messages=[{"role": "user", "content": prompt}])
            return resp.content[0].text.strip()
        except Exception:
            pass
    return _first_sentence(abstract) or "(abstract not available)"


def topic_intro(area: dict, items: list[dict], client=None) -> str:
    if client is not None and items:
        try:
            titles = "; ".join((p.get("title") or "")[:120] for p in items[:5])
            note = " ".join((area.get("audience_note") or "").split())
            prompt = (
                f"Write ONE short sentence introducing this week's papers in the "
                f"focus area '{area['name']}' for this audience: {note}\n"
                f"This week's titles: {titles}\nNo overstatement. One sentence:")
            resp = client.messages.create(
                model=NOTE_MODEL, max_tokens=140,
                messages=[{"role": "user", "content": prompt}])
            return resp.content[0].text.strip()
        except Exception:
            pass
    return " ".join((area.get("audience_note") or "").split())


def _badges(rec: dict) -> str:
    spec = []
    if rec.get("is_preprint"):
        spec.append(("preprint", "#92400e", "#fef3c7"))
    if rec.get("is_oa"):
        spec.append(("open access", "#065f46", "#d1fae5"))
    if rec.get("source"):
        spec.append((rec["source"], "#3730a3", "#e0e7ff"))
    return "".join(
        f'<span style="display:inline-block;font-size:11px;color:{fg};background:{bg};'
        f'border-radius:4px;padding:1px 6px;margin-left:6px;">{_esc(t)}</span>'
        for t, fg, bg in spec)


def _item_html(rec: dict, note: str) -> str:
    title = _esc(rec.get("title") or "(untitled)")
    url = _link(rec)
    title_html = (f'<a href="{_esc(url)}" style="color:#1d4ed8;text-decoration:none;">{title}</a>'
                  if url else title)
    meta = " · ".join(x for x in [_esc(_authors(rec)), _esc(rec.get("journal_or_server")),
                                  _esc(rec.get("published_date"))] if x)
    doi = rec.get("doi")
    doi_html = (f'<div style="font-size:11px;color:#9ca3af;margin-top:3px;">doi:{_esc(doi)}</div>'
                if doi else "")
    return (
        '<tr><td style="padding:12px 0;border-bottom:1px solid #eee;">'
        f'<div style="font-size:15px;font-weight:600;line-height:1.35;">{title_html}</div>'
        f'<div style="font-size:12px;color:#6b7280;margin:4px 0;">{meta}{_badges(rec)}</div>'
        f'<div style="font-size:13px;color:#1f2937;margin-top:4px;line-height:1.45;">{_esc(note)}</div>'
        f'{doi_html}</td></tr>')


def build_digest_html(papers: list[dict], profile: dict, window: dict,
                      client=None, analytics_html: str | None = None) -> str:
    """Render the weekly digest HTML from classified papers (top-N per area)."""
    top_n = int(profile.get("digest", {}).get("top_n_per_area", 6))
    confirmed = client is not None

    sections = []
    for area in profile["focus_areas"]:
        aid = area["id"]
        items = [p for p in papers if aid in (p.get("focus_areas") or [])]
        items.sort(key=lambda p: p.get("relevance_score") or 0, reverse=True)
        if items:
            sections.append((area, items[:top_n]))

    n_classified = sum(1 for p in papers if p.get("focus_areas"))
    win = f"{window.get('from','?')} → {window.get('to','?')}"

    parts = [
        '<div style="max-width:720px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111;">',
        '<h1 style="font-size:22px;margin:0 0 2px;">BCC PDAC Literature Digest</h1>',
        f'<div style="font-size:13px;color:#6b7280;">New open-access PDAC literature · {win} · '
        f'{n_classified} of {len(papers)} papers matched a focus area</div>',
        '<div style="font-size:12px;color:#92400e;background:#fffbeb;border:1px solid #fde68a;'
        'border-radius:6px;padding:8px 10px;margin:12px 0;">DRY RUN — not sent. '
        + ('' if confirmed else 'Classification is embedding-only (not yet LLM-confirmed); '
           'narrow targets like MYC/HuR may include off-topic papers until an Anthropic key is added. ')
        + 'Relevance notes are ' + ('LLM-written from the abstract.' if confirmed
                                     else 'the abstract\'s first sentence (no LLM key set).')
        + '</div>',
    ]
    if analytics_html:
        parts.append(analytics_html)
    for area, items in sections:
        parts.append(
            f'<h2 style="font-size:17px;margin:22px 0 2px;border-bottom:2px solid #111;padding-bottom:4px;">'
            f'{_esc(area["name"])} '
            f'<span style="font-size:12px;font-weight:400;color:#6b7280;">({len(items)})</span></h2>')
        parts.append(f'<div style="font-size:13px;color:#4b5563;font-style:italic;margin:6px 0 4px;">'
                     f'{_esc(topic_intro(area, items, client))}</div>')
        parts.append('<table style="width:100%;border-collapse:collapse;">')
        parts.extend(_item_html(p, relevance_note(p, area, client)) for p in items)
        parts.append('</table>')

    if not sections:
        parts.append('<p style="color:#6b7280;">No papers matched a focus area this window.</p>')

    parts.append(
        '<div style="font-size:11px;color:#9ca3af;margin-top:28px;border-top:1px solid #eee;padding-top:10px;">'
        'Generated by lit-agent · grounded in Europe PMC / PubMed / bioRxiv / medRxiv metadata.'
        '</div></div>')
    return "\n".join(parts)


def write_dry_run(html_str: str, out_dir: str | Path = "out", date_str: str | None = None) -> Path:
    date_str = date_str or date.today().isoformat()
    path = Path(out_dir) / f"digest_{date_str}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_str, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Delivery (Phase 3) — transactional send, gated behind SEND_LIVE
# ---------------------------------------------------------------------------
# Provider-agnostic: EMAIL_PROVIDER selects the adapter (default "resend").
# Both Resend and Postmark are plain HTTPS POSTs (no extra SDK). The sender
# (EMAIL_SENDER) must be on a domain/subdomain verified with the provider so
# SPF/DKIM pass. Nothing sends unless SEND_LIVE=1 (or an explicit --test-send).


def load_recipients(path: str | Path = "config/recipients.yaml") -> list[dict]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out = []
    for r in (data.get("recipients") or []):
        if isinstance(r, dict) and r.get("email"):
            out.append({"name": r.get("name", ""), "email": r["email"],
                        "focus_areas": r.get("focus_areas") or []})
    return out


def default_subject(window: dict, n_new: int | None = None) -> str:
    base = f"BCC PDAC Literature Digest — {window.get('to', '')}".rstrip(" —")
    return base + (f" ({n_new} new)" if n_new is not None else "")


def _send_resend(api_key: str, sender: str, to: str, subject: str, html_body: str) -> str:
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": sender, "to": [to], "subject": subject, "html": html_body},
        timeout=30)
    r.raise_for_status()
    return r.json().get("id", "")


def _send_postmark(token: str, sender: str, to: str, subject: str, html_body: str) -> str:
    r = requests.post(
        "https://api.postmarkapp.com/email",
        headers={"X-Postmark-Server-Token": token, "Accept": "application/json"},
        json={"From": sender, "To": to, "Subject": subject, "HtmlBody": html_body,
              "MessageStream": "outbound"},
        timeout=30)
    r.raise_for_status()
    return str(r.json().get("MessageID", ""))


_PROVIDERS = {"resend": ("RESEND_API_KEY", _send_resend),
              "postmark": ("POSTMARK_SERVER_TOKEN", _send_postmark)}


def _send_one(to: str, subject: str, html_body: str) -> str:
    name = os.environ.get("EMAIL_PROVIDER", "resend").lower()
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown EMAIL_PROVIDER {name!r}; supported: {list(_PROVIDERS)}")
    key_env, fn = _PROVIDERS[name]
    api_key = os.environ.get(key_env)
    sender = os.environ.get("EMAIL_SENDER")
    if not api_key or not sender:
        raise RuntimeError(f"{name}: set EMAIL_SENDER and {key_env} to send.")
    return fn(api_key, sender, to, subject, html_body)


def deliver(html_body: str, subject: str, recipients: list[dict], *, force: bool = False) -> dict:
    """Send the digest — ONLY if SEND_LIVE=1 (or force=True). Otherwise a no-op:
    the dry-run HTML file is the artifact. Each recipient gets their own message
    (forward-compatible with per-recipient focus-area filtering). Returns a summary.
    """
    live = force or os.environ.get("SEND_LIVE") == "1"
    if not live:
        return {"sent": 0, "dry_run": True, "recipients": len(recipients), "errors": {}}
    sent, errors = 0, {}
    for r in recipients:
        try:
            _send_one(r["email"], subject, html_body)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — collect per-recipient failures
            errors[r["email"]] = str(exc)
    return {"sent": sent, "dry_run": False, "recipients": len(recipients), "errors": errors}


def send_test(html_body: str, subject: str, address: str) -> str:
    """Explicit one-off test send (bypasses SEND_LIVE; still needs key + sender)."""
    return _send_one(address, f"[TEST] {subject}", html_body)
