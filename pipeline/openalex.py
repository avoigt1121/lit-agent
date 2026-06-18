"""
pipeline/openalex.py — minimal, polite OpenAlex Works API client.

OpenAlex (https://openalex.org) is a free, key-less scholarly index with an open
citation graph and DOIs on most records. lit-agent uses it for two things, both
leaning on the same primitive — cursor-paginated ``/works`` with a server-side
``filter`` and a trimmed ``select`` — so this module keeps that primitive clean
and importable rather than burying it in one script:

  * Coverage triangulation (Phase B, ``scripts/coverage_check.py``): run the saved
    PDAC query against OpenAlex and diff the DOI set against Europe PMC's, so
    coverage is a *measured* property, not an assumption.
  * Citation signal (Phase D): *"N papers this week cite your work"* via the
    ``cites:<openalex_id>`` filter over a seed paper — reuses ``iter_works`` /
    ``count`` directly.

STANDALONE: depends only on ``requests`` + the project's own polite-session
helper. Never runs inside the Space (offline pipeline only).
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

import requests

from pipeline.harvest import _session, normalize_doi, request_json

logger = logging.getLogger("openalex")

WORKS_URL = "https://api.openalex.org/works"
REQUEST_TIMEOUT = 30   # seconds
POLITE_PAUSE = 0.2     # seconds between paginated requests (fair-use)
PER_PAGE_MAX = 200     # OpenAlex hard cap


def build_filter(clauses: dict[str, str]) -> str:
    """Join ``{key: value}`` into OpenAlex's comma-separated filter syntax.

    Drops None/empty values and guards the one footgun: a value containing a
    comma would be mis-split into a bogus extra clause (comma is the delimiter).
    """
    parts = []
    for key, value in clauses.items():
        if value is None or value == "":
            continue
        if "," in str(value):
            raise ValueError(
                f"OpenAlex filter value for {key!r} contains a comma, which is the "
                f"filter delimiter: {value!r}. Rephrase the query (or set "
                f"openalex.search in config/sources.yaml)."
            )
        parts.append(f"{key}:{value}")
    return ",".join(parts)


class OpenAlexClient:
    """Thin cursor-paginating wrapper over the OpenAlex ``/works`` endpoint.

    Passing ``mailto`` opts into OpenAlex's faster "polite pool" (their etiquette
    ask — the analogue of NCBI/EBI's tool+email); use the same ``contact_email``
    the rest of the pipeline uses.
    """

    def __init__(self, mailto: str | None = None, tool_name: str = "lit-agent",
                 session: requests.Session | None = None):
        self.mailto = mailto or None
        self.session = session or _session(mailto or "", tool_name)

    def _params(self, **extra) -> dict:
        params = dict(extra)
        if self.mailto:
            params["mailto"] = self.mailto
        return params

    def count(self, filter_str: str) -> int:
        """Total works matching ``filter_str`` (one cheap request, per-page=1)."""
        data = request_json(self.session, WORKS_URL,
                            self._params(filter=filter_str, **{"per-page": 1}),
                            timeout=REQUEST_TIMEOUT)
        return int((data.get("meta") or {}).get("count", 0))

    def iter_works(self, filter_str: str, *, select: str = "doi,id",
                   per_page: int = PER_PAGE_MAX, max_pages: int | None = None,
                   pause: float = POLITE_PAUSE) -> Iterator[dict]:
        """Yield work dicts matching ``filter_str``, following the cursor to the end.

        ``filter_str`` is a literal OpenAlex filter expression (comma-joined
        ``key:value`` clauses) — build it with :func:`build_filter`. Only the
        ``select``-ed fields are returned per work, keeping payloads small.
        """
        cursor, page = "*", 0
        while True:
            params = self._params(filter=filter_str, select=select,
                                  cursor=cursor, **{"per-page": min(per_page, PER_PAGE_MAX)})
            data = request_json(self.session, WORKS_URL, params, timeout=REQUEST_TIMEOUT)
            results = data.get("results", []) or []
            for work in results:
                yield work
            page += 1
            cursor = (data.get("meta") or {}).get("next_cursor")
            logger.info("OpenAlex page %d: +%d (count=%s)", page, len(results),
                        (data.get("meta") or {}).get("count"))
            if not results or not cursor:
                break
            if max_pages is not None and page >= max_pages:
                logger.warning("OpenAlex: hit max_pages=%d cap; result set truncated", max_pages)
                break
            time.sleep(pause)


def search_doi_set(query: str, date_from: str, date_to: str, *,
                   client: OpenAlexClient,
                   search_field: str = "title_and_abstract",
                   date_field: str = "publication_date",
                   types: str | None = None,
                   max_pages: int | None = None) -> tuple[set[str], int]:
    """Normalized DOI set for ``query`` in the window, plus OpenAlex's total count.

    ``query`` is an OpenAlex search expression: uppercase booleans (``OR``/``NOT``)
    and ``"quoted phrases"`` for exact match. ``types`` is an optional OpenAlex
    ``type`` filter (``|``-joined, e.g. ``"article|review|preprint"``) to restrict
    to primary literature comparable to Europe PMC — excluding book chapters,
    peer-review objects, etc. The returned set excludes works with no DOI (they
    can't take part in a DOI diff); ``count - len(dois)`` is how many were dropped
    for that reason.
    """
    filter_str = build_filter({
        f"{search_field}.search": query,
        f"from_{date_field}": date_from,
        f"to_{date_field}": date_to,
        "type": types,
    })
    total = client.count(filter_str)
    dois: set[str] = set()
    for work in client.iter_works(filter_str, select="doi,id", max_pages=max_pages):
        doi = normalize_doi(work.get("doi"))
        if doi:
            dois.add(doi)
    return dois, total
