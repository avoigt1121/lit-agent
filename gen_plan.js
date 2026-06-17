const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, ExternalHyperlink,
  TabStopType, TabStopPosition, TableOfContents, HeadingLevel,
  BorderStyle, WidthType, ShadingType, PageNumber, PageBreak
} = require("docx");

// ---------- helpers ----------
const FONT = "Arial";
const MONO = "Consolas";
const CW = 9360; // content width US Letter, 1" margins

const H1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const H2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const H3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(t)] });

function P(runs, opts = {}) {
  const children = Array.isArray(runs) ? runs : [new TextRun(runs)];
  return new Paragraph({ children, spacing: { after: 140, line: 276 }, ...opts });
}
function bullet(runs, level = 0) {
  const children = Array.isArray(runs) ? runs : [new TextRun(runs)];
  return new Paragraph({ numbering: { reference: "bullets", level }, spacing: { after: 60 }, children });
}
function num(runs, level = 0) {
  const children = Array.isArray(runs) ? runs : [new TextRun(runs)];
  return new Paragraph({ numbering: { reference: "nums", level }, spacing: { after: 60 }, children });
}
const b = (t) => new TextRun({ text: t, bold: true });
const tx = (t) => new TextRun(t);
const code = (t) => new TextRun({ text: t, font: MONO, size: 20 });
function link(text, url) {
  return new ExternalHyperlink({ children: [new TextRun({ text, style: "Hyperlink" })], link: url });
}

// shaded left-accent callout box (single border -> schema-safe)
function callout(children, accent = "E0A93B", fill = "FFF6E5") {
  return new Paragraph({
    shading: { fill, type: ShadingType.CLEAR },
    border: { left: { style: BorderStyle.SINGLE, size: 18, color: accent, space: 8 } },
    spacing: { before: 80, after: 160, line: 276 },
    indent: { left: 120 },
    children,
  });
}
// monospace code block: one shaded paragraph per line
function codeBlock(lines) {
  return lines.map((ln, i) => new Paragraph({
    shading: { fill: "F3F4F6", type: ShadingType.CLEAR },
    spacing: { before: i === 0 ? 80 : 0, after: i === lines.length - 1 ? 160 : 0, line: 248 },
    indent: { left: 120 },
    children: [new TextRun({ text: ln || " ", font: MONO, size: 18 })],
  }));
}

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const HEAD_FILL = "1F3864";
const ALT_FILL = "EEF2F8";

function cell(content, w, { head = false, fill = null } = {}) {
  const paras = (Array.isArray(content) ? content : [content]).map((c) =>
    typeof c === "string"
      ? new Paragraph({ children: [new TextRun({ text: c, bold: head, color: head ? "FFFFFF" : "000000", size: 20 })] })
      : c
  );
  return new TableCell({
    borders,
    width: { size: w, type: WidthType.DXA },
    shading: { fill: head ? HEAD_FILL : (fill || "FFFFFF"), type: ShadingType.CLEAR },
    margins: { top: 60, bottom: 60, left: 110, right: 110 },
    children: paras,
  });
}
function table(widths, rows) {
  return new Table({
    width: { size: CW, type: WidthType.DXA }, columnWidths: widths,
    rows: rows.map((cells, ri) => new TableRow({
      tableHeader: ri === 0,
      children: cells.map((c, ci) => cell(c, widths[ci], { head: ri === 0, fill: ri > 0 && ri % 2 === 0 ? ALT_FILL : null })),
    })),
  });
}
const spacer = () => new Paragraph({ children: [new TextRun("")], spacing: { after: 60 } });

// ---------- body ----------
const body = [];

// Title
body.push(new Paragraph({ spacing: { after: 60 },
  children: [new TextRun({ text: "Technical Plan & Build Brief — Literature Review Agent", bold: true, size: 38, font: FONT })] }));
body.push(new Paragraph({ spacing: { after: 40 },
  children: [new TextRun({ text: "PDAC Research Agent System · Standalone-first · Design rationale + Claude Code instructions", size: 22, color: "1F3864" })] }));
body.push(new Paragraph({
  border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "2E75B6", space: 1 } },
  spacing: { after: 200 },
  children: [new TextRun({ text: "Author: Annie Voigt · June 15, 2026 · Internal design doc", italics: true, size: 20, color: "595959" })] }));

body.push(callout([
  new TextRun({ text: "Purpose of this document. ", bold: true, size: 21 }),
  new TextRun({ text: "This is a working design doc, not a customer deliverable. It records the design decisions behind the Literature Review Agent and translates them into a concrete, phase-by-phase brief that can be handed to Claude Code to build. Sections 3 and 10–11 are the operative parts: the decision log, the component specifications, and the per-phase instructions (the “tell Claude Code” blocks) with acceptance criteria.", size: 21 }),
], "2E75B6", "EAF1F8"));

body.push(new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }));
body.push(new Paragraph({ children: [new PageBreak()] }));

// 1. Executive Summary
body.push(H1("1. Executive Summary"));
body.push(P([tx("A "), b("Literature Review Agent"), tx(" for the BCC that (1) emails a curated weekly digest of newly published open-access PDAC literature to a 5–20-person list, (2) produces “what was most covered” analytics over week / month / year windows, and (3) runs on Hugging Face Spaces as an expert chat for grounded follow-up questions about the papers it surfaced.")]));
body.push(P([tx("It is designed "), b("standalone-first"), tx(" — its own HF Space plus a scheduled ingestion/email job. It is a "), b("separate system"), tx(" from DecoupleRpy / biodata-registry / the Research Coordinator: those operate on transcriptomic data; this operates on published papers. There is no runtime dependency between them. What carries over is "), b("engineering experience and reusable scaffolding"), tx(" (the Gradio chat shell, the deploy/sync setup, the eval-harness design, and the “answer only from real sources” discipline), which is why the build is cheaper than from-scratch — not because the systems integrate. The hardest part is the "), b("trust layer"), tx(": dedup/recency, BCC-relevance precision, and grounding Q&A in real full text. If the OHSU library can grant text-and-data-mining (TDM) access, that trust layer gets dramatically easier (§9.1).")]));

// 2. Scope
body.push(H1("2. Scope & Functional Requirements"));
body.push(H2("2.1 In scope"));
body.push(num([b("Weekly digest email"), tx(" — newest OA PDAC literature, grouped by topic, to 5–20 addresses, each item tagged with the BCC focus area(s) it serves and a one-line relevance note inferred from the BCC interest profile.")]));
body.push(num([b("Coverage analytics"), tx(" — most-covered PDAC topics over week / month / year: counts, deltas vs prior period, movers.")]));
body.push(num([b("Expert Q&A on HF"), tx(" — chat answering follow-ups grounded in the ingested corpus (methods, comparisons, “apply to our data” reasoning).")]));
body.push(H2("2.2 Out of scope (v1)"));
body.push(bullet("Closed-access full text (metadata + abstracts always; full text only for OA, unless OHSU TDM is enabled)."));
body.push(bullet("Wet-lab / protocol design (same boundary the Coordinator enforces)."));
body.push(bullet("Running analyses on BCC datasets (that stays with DecoupleRpy; the Lit Agent may suggest a handoff but does not execute)."));

// 3. Design Decisions & Rationale
body.push(H1("3. Design Decisions & Rationale"));
body.push(P("The decisions below are the spine of the design. Each is a deliberate choice with a rationale and, where relevant, the alternative that was rejected — these are what Claude Code should treat as fixed constraints unless explicitly revisited."));
body.push(table([2500, 3200, 3660], [
  ["Decision", "Choice", "Why / rejected alternative"],
  ["System boundary", "Standalone HF Space + scheduled job", "Q&A needs the agent to hold corpus state; the Coordinator is thin/stateless. Rejected: building it inside the Coordinator (couples a slow pipeline to interactive chat)."],
  ["Relationship to existing system", "Independent; optional future seam", "Different data domain (papers vs transcriptomics). Coordinator integration is later and cheap (one agents.yaml entry), but NOT built in v1."],
  ["Ingestion timing", "Offline scheduled pipeline", "Decouples slow, rate-limited, failure-prone API work from the live chat. Rejected: ingest-on-demand in the Space (latency + reliability)."],
  ["Primary source", "Europe PMC REST (primary) + bioRxiv/medRxiv + PubMed", "Europe PMC ingests all PubMed and adds OA full text + text-mined annotations. Preprints needed for “newest.” PubMed adds MeSH."],
  ["Full-text policy", "OA-only v1; pursue OHSU TDM", "Honest and safe. TDM via library-provisioned tokens is the sanctioned automation path; raw proxy scraping is prohibited (§9.1)."],
  ["Corpus persistence", "Durable store (SQLite + vector index, hosted as an HF Dataset or external DB)", "HF Space storage is ephemeral on restart. The corpus is the system’s memory and must survive restarts."],
  ["Interest model", "Versioned YAML profile, not hardcoded", "BCC priorities drift; edit config without redeploying. Mirrors the Coordinator keeping scope in prompts/registry."],
  ["Embeddings", "Biomedical-grade (e.g. Voyage) over commodity", "Domain accuracy + paper-length context window improve retrieval; modest cost at this volume. Commodity (OpenAI) is the cheaper fallback."],
  ["Email delivery", "Transactional provider (Resend/Postmark/SES)", "Solves SPF/DKIM, bounces, deliverability. Rejected: raw SMTP."],
  ["Scheduler", "GitHub Actions cron", "Reuses the surface already used to sync to HF; team knows it."],
  ["Rollout", "Dev surface first, dry-run email, then promote", "Mirrors how the Coordinator/DecoupleRpy were hardened before prod."],
]));

// 4. Architecture
body.push(H1("4. Architecture Overview"));
body.push(P([tx("Two runtime surfaces share one persistent corpus: an "), b("offline pipeline"), tx(" (scheduled: ingest → dedup → score → persist → email → analytics) and an "), b("online Space"), tx(" (Gradio chat for grounded Q&A + cached analytics).")]));
body.push(H3("4.1 Data flow"));
body.push(table([2400, 6960], [
  ["Stage", "What happens"],
  ["1 · Harvest", "Scheduled job queries Europe PMC / PubMed / bioRxiv / medRxiv for the trailing window using the saved PDAC query; pulls metadata + abstracts (+ OA full text where available)."],
  ["2 · Normalize & dedup", "Records → common schema; cross-source duplicates collapsed (DOI norm + fuzzy title + preprint→published linkage)."],
  ["3 · Classify & score", "Each paper embedded; assigned BCC focus area(s) + relevance score via embeddings + an LLM confirm against the interest profile."],
  ["4 · Persist", "New records + embeddings + scores written to the corpus store."],
  ["5 · Digest", "LLM composes the weekly email from top-ranked new items, grouped by topic, with per-item relevance notes; rendered to HTML; sent."],
  ["6 · Analytics", "Window counts/trends recomputed and cached for the Space."],
  ["7 · Q&A (online)", "Space answers via retrieval over the corpus — abstracts always, full text for OA — grounded with citations."],
]));

// 5. Sources
body.push(H1("5. Literature Data Sources"));
body.push(table([2200, 4360, 2800], [
  ["Source", "What it gives", "Role"],
  ["Europe PMC REST", "33M+ records incl. all PubMed; OA full-text search; Annotations API (genes, diseases, chemicals)", "PRIMARY"],
  ["PubMed E-utilities", "Authoritative index (esearch/efetch); MeSH terms", "Secondary / MeSH tagging"],
  ["bioRxiv + medRxiv API", "Preprints by date range as JSON", "REQUIRED for recency"],
]));
body.push(P([b("Limits to design around: "), tx("PubMed E-utilities = 3 req/s (10 with a free API key). bioRxiv/medRxiv "), code("details"), tx(" endpoint paginates by cursor. Europe PMC paginates via "), code("cursorMark"), tx(". Use a single saved, version-controlled PDAC query so coverage is reproducible.")]));

// 6. Interest model
body.push(H1("6. The BCC Interest Model"));
body.push(P([tx("A versioned YAML profile (schema in §10.4) describes each BCC focus area with keywords, exemplar papers, and an audience note. Two consumers: "), b("classification"), tx(" (multi-label assignment via embedding similarity + a cheap LLM confirm) and "), b("relevance inference"), tx(" (the per-item “why this matters to <area>” line, grounded in the abstract). Config, not code, so priorities can change without a redeploy.")]));

// 7. Digest & analytics
body.push(H1("7. Weekly Digest & Analytics"));
body.push(P([tx("Triggered by a GitHub Actions cron. The composer selects top-ranked new items per area, has the LLM write the topic intros + relevance lines, renders templated HTML, and sends via the transactional provider. A "), b("dry-run mode"), tx(" writes the email to a file for review before any live send. Analytics are simple aggregations over the timestamped, topic-tagged corpus, precomputed and cached so the Space renders them instantly.")]));

// 8. Q&A
body.push(H1("8. Expert Q&A on Hugging Face"));
body.push(P([tx("Reuses the Coordinator’s Gradio chat shell. The difference is "), b("retrieval-augmented generation"), tx(": retrieve relevant passages from the corpus, answer strictly from them with DOI citations, and "), b("guard"), tx(" — if a methods question targets a paper with no OA full text, say so plainly rather than inventing methods (the Q&A analogue of the DecoupleRpy anti-fabrication discipline).")]));

// 9. Hardest part
body.push(H1("9. The Hardest Part"));
body.push(P([b("The trust layer, not the plumbing. "), tx("APIs, a chat, a cron, and an email send are routine. Risk concentrates where a wrong answer is worse than none:")]));
body.push(H3("9.1 Full-text access is the binding constraint on Q&A"));
body.push(P([tx("Methods questions need full text, but many new PDAC papers are paywalled. The agent always has the "), b("abstract"), tx("; full text only for OA. The failure mode is confidently describing a method it never read. Mitigation is architectural: retrieve-then-answer, hard-refuse method detail not in retrieved text, label “abstract-only.”")]));
body.push(P([b("OHSU access is the single biggest lever. "), tx("If OHSU library subscriptions include TDM entitlements, the OA-only limit largely dissolves. Nuances: (1) you "), b("cannot"), tx(" crawl the library proxy (EZproxy/OpenAthens/Shibboleth) — licenses forbid it and it risks blocking OHSU’s whole IP range; the sanctioned path is publisher "), b("TDM/API programs"), tx(" with a token the library provisions once. (2) "), b("Access ≠ redistribution"), tx(" — read full text to ground answers, but don’t embed licensed full text in emails; link out, quote short fair-use snippets.")]));
body.push(callout([
  new TextRun({ text: "Aside — the one email to send the OHSU library: ", bold: true, size: 21 }),
  new TextRun({ text: "“Do our institutional subscriptions include text-and-data-mining (TDM) or API access to publisher full text, and if so how do we obtain the API tokens? Do we authenticate via OpenAthens or Shibboleth?” ", italics: true, size: 21 }),
  new TextRun({ text: "The answer decides whether Q&A is OA-only or near-comprehensive — resolve before Phase 1.", size: 21 }),
]));
body.push(H3("9.2 Dedup & “what’s genuinely new”"));
body.push(P([tx("The same paper appears as bioRxiv → medRxiv → PubMed → Europe PMC with drifting titles. Without DOI normalization, fuzzy title matching, and preprint→published linkage, the digest double-counts and analytics inflate. “New” must be defined explicitly (first-seen vs published vs indexed date) and applied consistently.")]));
body.push(H3("9.3 Relevance precision"));
body.push(P([tx("“Of interest to the BCC” is a precision problem — a noisy email gets ignored within weeks. The interest model must be tuned against real feedback, and the eval must grade digest relevance, not just Q&A correctness. This is harder to evaluate than routing correctness because relevance is subjective and needs human labels.")]));

// 10. Component specifications
body.push(H1("10. Component Specifications for Claude Code"));
body.push(P("Concrete contracts so Claude Code builds to spec rather than improvising. Treat these as the source of truth for structure, schemas, and interfaces."));

body.push(H3("10.1 Project structure"));
body.push(...codeBlock([
  "lit-agent/",
  "  app.py                  # Gradio entrypoint (HF Space)",
  "  ui.py                   # chat UI (reuse research-coordinator/gradio_ui.py patterns)",
  "  config/",
  "    interest_profile.yaml # BCC focus areas (see 10.4)",
  "    sources.yaml          # PDAC query string + per-source params",
  "    recipients.yaml       # digest distribution list",
  "  pipeline/",
  "    harvest.py            # per-source API clients + windowed pull",
  "    normalize.py          # schema mapping + dedup/linkage",
  "    score.py              # embed + classify + relevance score",
  "    digest.py             # compose + render HTML + send (dry-run flag)",
  "    analytics.py          # window aggregations",
  "    run_weekly.py         # orchestrates harvest->...->digest->analytics",
  "  store/",
  "    db.py                 # SQLite schema + read/write (see 10.3)",
  "    vectors.py            # embedding index read/write",
  "  qa/",
  "    retrieve.py           # top-k retrieval over corpus",
  "    answer.py             # grounded answer + guards (see 10.5)",
  "  eval/",
  "    questions.json        # Q&A bank   relevance_set.json # digest labels",
  "    run_eval.py           # reuse research-coordinator/eval design",
  "  .github/workflows/",
  "    weekly.yml            # cron trigger for run_weekly.py",
  "  requirements.txt  README.md  DEPLOYMENT.md",
]));

body.push(H3("10.2 Normalized paper record"));
body.push(P("Every source maps into this shape before storage:"));
body.push(...codeBlock([
  "{",
  '  "doi": "10.x/...",            // normalized, lowercase; primary key when present',
  '  "ids": {"pmid": "...", "pmcid": "...", "preprint_doi": "..."},',
  '  "title": "...", "abstract": "...",',
  '  "authors": ["..."], "journal_or_server": "...",',
  '  "published_date": "YYYY-MM-DD", "first_seen_date": "YYYY-MM-DD",',
  '  "is_oa": true, "oa_fulltext_url": "..." | null,',
  '  "source": "europepmc|pubmed|biorxiv|medrxiv",',
  '  "is_preprint": false, "linked_published_doi": "..." | null,',
  '  "mesh": ["..."], "annotations": {"genes": [], "diseases": []},',
  '  "focus_areas": ["..."], "relevance_score": 0.0,   // filled by score.py',
  '  "embedding_id": "..."',
  "}",
]));

body.push(H3("10.3 Corpus store (SQLite + vector index)"));
body.push(bullet([code("papers"), tx(" — one row per deduped record (fields above); PK = DOI, fallback synthetic id.")]));
body.push(bullet([code("topic_tags"), tx(" — (paper_id, focus_area, score); supports multi-label + analytics.")]));
body.push(bullet([code("runs"), tx(" — (run_date, window, n_harvested, n_new, n_emailed) for audit + analytics deltas.")]));
body.push(bullet([code("vectors"), tx(" — embedding per paper (abstract; + chunked OA full text where available) in the vector index.")]));
body.push(P([b("Persistence: "), tx("commit the SQLite file + index to a durable store (HF Dataset repo or external DB) at the end of each run; the Space loads it read-only at startup.")]));

body.push(H3("10.4 Interest profile YAML"));
body.push(...codeBlock([
  "focus_areas:",
  "  - id: kras_targeting",
  "    name: KRAS-directed therapy",
  "    keywords: [KRAS, G12C, G12D, pan-RAS, SHP2]",
  "    exemplar_dois: [10.x/..., 10.x/...]",
  "    audience_note: >",
  "      The BCC's translational group works on KRAS-mutant PDAC;",
  "      surface mechanism, resistance, and combination-therapy results.",
  "  - id: tumor_microenvironment",
  "    name: Tumor microenvironment & immunology",
  "    keywords: [stroma, CAF, immune evasion, T cell]",
  "    exemplar_dois: [...]",
  "    audience_note: >",
  "      ...",
]));
body.push(P([b("Needed from the BCC to seed this: "), tx("the actual focus areas and a few exemplar DOIs per area. Quality of the whole digest tracks the quality of this file.")]));

body.push(H3("10.5 Prompt skeletons"));
body.push(P([b("Classification (cheap model): "), tx("“Given this title+abstract and these focus-area descriptors, return the matching area ids and a 0–1 confidence each. Return [] if none fit. JSON only.”")]));
body.push(P([b("Relevance note (digest): "), tx("“In one sentence, explain why this paper matters to <area audience_note>, using only claims supported by the abstract. No overstatement.”")]));
body.push(P([b("Q&A grounding (with guard): "), tx("“Answer ONLY from the retrieved passages below, citing DOIs. If the question asks about methodology and no full-text passage is present (abstract only), say the full text isn’t available and summarize what the abstract states — do not infer methods.”")]));

body.push(H3("10.6 Reference repositories for Claude Code"));
body.push(P([tx("The existing repos are "), b("structural templates to copy from"), tx(", not systems this agent calls at runtime. Giving Claude Code these as read-only references makes it build something shaped like what already works. State explicitly that they are "), b("references, not dependencies"), tx(" — do not add them to "), code("requirements.txt"), tx(" or import from them.")]));
body.push(table([2600, 1500, 5260], [
  ["Repo", "Priority", "Use as"],
  ["research-coordinator", "Primary", "Space + chat shell (app.py, gradio_ui.py), eval harness (eval/run_eval.py), deploy/sync (.github/workflows/sync-to-hf-space.yml), config design (prompts.yaml, agents.yaml)"],
  ["biodata-registry", "Yes", "store/ package model — manifests + loaders + a *_list_available accessor"],
  ["DecoupleRpy_Agent", "Optional", "Groundedness discipline ONLY (answer from evidence, show trace, refuse to fabricate). Do not copy domain logic."],
]));
body.push(P([b("Wiring: "), tx("place the reference repos on disk in the working tree (sibling dirs / a small workspace with "), code("lit-agent/"), tx(" beside read-only checkouts) — local files beat URLs — and add a "), code("CLAUDE.md"), tx(" to the new repo naming what to mirror and what not to copy:")]));
body.push(...codeBlock([
  "# lit-agent — build conventions",
  "# STANDALONE. The repos below are STRUCTURAL REFERENCES ONLY —",
  "# do not depend on, import from, or list them in requirements.txt.",
  "",
  "## Mirror these patterns",
  "- Space + chat, eval, deploy:  ../research-coordinator",
  "    app.py, gradio_ui.py        (Space + streaming chat)",
  "    eval/run_eval.py            (eval harness + judge)",
  "    .github/workflows/sync-to-hf-space.yml",
  "    prompts.yaml, agents.yaml   (config-driven design)",
  "- Corpus store package:        ../biodata-registry  -> model store/ on it",
  "- Groundedness discipline ONLY: ../DecoupleRpy_Agent",
  "",
  "## Do not copy",
  "- decoupleR/scanpy computation or dataset-selection heuristics",
  "  (different data domain).",
]));

// 11. Build brief
body.push(H1("11. Build Brief — Phase-by-Phase Instructions for Claude Code"));
body.push(P("Each phase is independently demoable. The “Tell Claude Code” line is the instruction to paste; “Done when” is the acceptance criterion to verify before moving on. Build on a dev surface and keep the weekly email in dry-run until Phase 3."));

const phases = [
  ["Phase 0 — Spike",
    "“Build pipeline/harvest.py with clients for Europe PMC, PubMed (esearch/efetch), and bioRxiv/medRxiv. Read the PDAC query and per-source params from config/sources.yaml. Pull the last 7 days, map each result into the normalized paper record (§10.2), and write the combined list to data/spike.json. Respect a 3 req/s cap (PubMed) and paginate fully.”",
    "spike.json contains a week of PDAC papers from all sources with populated title/abstract/doi/dates, and counts per source are printed."],
  ["Phase 1 — Corpus",
    "“Build store/db.py and store/vectors.py per §10.3. Add normalize.py: DOI-normalize, fuzzy-title dedup, and link preprints to published versions; define ‘new’ as first_seen_date. Add score.py to embed abstracts (biomedical embedding model) and write everything to the store. Persist the SQLite file + index to the durable store at end of run.”",
    "Re-running ingestion adds only genuinely new papers (no duplicates across sources/preprints), and the store reloads intact after a restart."],
  ["Phase 2 — Digest (dry-run)",
    "“Implement the interest model: load config/interest_profile.yaml, classify each new paper into focus areas (embeddings + LLM confirm, §10.5), and compute relevance_score. Build digest.py to group top items per area, generate topic intros + per-item relevance notes, render templated HTML, and (in --dry-run) write it to out/digest_<date>.html WITHOUT sending.”",
    "out/digest_<date>.html is well-organized, items map to sensible focus areas, and relevance notes are accurate to the abstracts on manual review."],
  ["Phase 3 — Deliver",
    "“Wire digest.py to a transactional email provider (key + sender from env). Read recipients from config/recipients.yaml. Add .github/workflows/weekly.yml to run pipeline/run_weekly.py on a weekly cron. Keep a --dry-run guard and a SEND_LIVE env flag so live sending is explicit.”",
    "A scheduled run delivers a correctly formatted email to a test address; flipping SEND_LIVE sends to the real list; SPF/DKIM pass."],
  ["Phase 4 — Analytics",
    "“Build analytics.py to compute per-topic counts and deltas for week/month/year windows from the store, cache results to a file the Space reads, and append a compact ‘analytics at a glance’ block to the digest email.”",
    "Window counts reconcile against the runs table, deltas are correct vs the prior window, and the email footer matches."],
  ["Phase 5 — Q&A Space",
    "“Build app.py/ui.py (reuse research-coordinator/gradio_ui.py patterns), qa/retrieve.py (top-k over the corpus, filterable by paper/week), and qa/answer.py with the grounding guard (§10.5). Stream answers with DOI citations.”",
    "Asking about a known paper returns a grounded, cited answer; asking methods on an abstract-only paper triggers the ‘full text unavailable’ guard instead of fabricating."],
  ["Phase 6 — Eval & harden",
    "“Adapt research-coordinator/eval into eval/run_eval.py: a Q&A bank graded for groundedness (no claim absent from retrieved text) and a labeled digest set graded for relevance precision. Report pass rates; iterate the interest profile and guards against failures.”",
    "Eval runs green on the bank; groundedness violations and irrelevant digest items are driven down across iterations."],
  ["Phase 7 — Optional integration",
    "“Only if desired: register the Lit Agent as a second specialist in the Coordinator’s agents.yaml with trigger keywords for literature questions. No router.py changes.”",
    "The Coordinator routes a literature question to the Lit Agent and relays its answer."],
];
phases.forEach(([title, tell, done]) => {
  body.push(H3(title));
  body.push(callout([new TextRun({ text: "Tell Claude Code: ", bold: true, size: 21 }), new TextRun({ text: tell, italics: true, size: 21 })], "2E75B6", "EAF1F8"));
  body.push(P([b("Done when: "), tx(done)]));
});

// 12. LOE
body.push(H1("12. Level of Effort vs. Everything Built So Far"));
body.push(P([tx("The fair baseline is the whole body of work: the Coordinator router, the "), b("DecoupleRpy specialist"), tx(", the "), b("biodata-registry"), tx(", the eval harness, and two-Space deployment hardening. If the router alone is “1 unit,” that full system is ~6–9 units (DecoupleRpy and the registry are the bulk).")]));
body.push(P([b("Important honesty check: "), tx("the Lit Agent does "), b("not"), tx(" functionally integrate with those systems — it works on a different data domain. What lowers its cost is "), b("reuse of patterns, scaffolding, and experience"), tx(", not shared runtime: the corpus store re-skins the registry "), b("pattern"), tx("; the grounded Q&A re-applies the DecoupleRpy anti-fabrication "), b("approach"), tx("; the eval reuses the harness "), b("design"), tx("; the Gradio/deploy scaffolding is directly reusable. That is real leverage, but it is engineering carry-over, not a half-finished product.")]));
body.push(table([2700, 1500, 5160], [
  ["Workstream", "Effort", "Notes"],
  ["Gradio/HF Space + chat", "0.3", "Reuse of gradio_ui.py + streaming/panels"],
  ["Harvester (multi-source ingestion)", "0.6", "Net-new: 3–4 API clients, windowing, rate limits"],
  ["Dedup + normalization", "0.5", "Net-new and genuinely hard (§9.2)"],
  ["Corpus store + vector index", "0.25", "Re-skins the biodata-registry pattern"],
  ["Interest model + relevance scoring", "0.5", "Config + embeddings + LLM tuning; precision-critical"],
  ["Digest composer + mailer", "0.4", "Net-new delivery surface"],
  ["Analytics (windows/trends)", "0.2", "Straightforward given the schema"],
  ["RAG Q&A + groundedness guards", "0.35", "Reuses DecoupleRpy anti-fabrication lesson"],
  ["Scheduler + ops", "0.2", "GitHub Actions cron"],
  ["Eval (relevance + groundedness)", "0.25", "Reuses harness design; new graders"],
  ["Total (net of reuse)", "≈ 3.5", "Concentrated in the four net-new rows"],
]));
body.push(P([b("Net: ~0.4–0.6× of everything built so far — about half. "), tx("The Lit Agent is ~3–3.5 router-equivalent units vs ~6–9 for the full prior system. Net-new effort is just ingestion, dedup, email, and analytics; only "), b("dedup/recency"), tx(" is novel difficulty. The biggest schedule risk is the "), b("iterate-with-real-feedback"), tx(" loop on relevance/groundedness, which needs a few real weekly cycles + human labels.")]));

// 13. Open questions
body.push(H1("13. Open Questions & Risks"));
body.push(bullet([b("OHSU TDM access: "), tx("decides OA-only vs near-comprehensive Q&A — resolve before Phase 1 (§9.1).")]));
body.push(bullet([b("BCC focus areas: "), tx("need the real priorities + exemplar DOIs to seed §10.4.")]));
body.push(bullet([b("Definition of “new”: "), tx("first-seen vs published vs indexed date — pick one (plan assumes first-seen) and document it.")]));
body.push(bullet([b("Email provider + sender domain: "), tx("choose before Phase 3 for SPF/DKIM.")]));
body.push(bullet([b("Persistence host: "), tx("HF Dataset vs external DB vs managed vector store — decide before Phase 1.")]));
body.push(bullet([b("Embedding model: "), tx("biomedical quality vs commodity cost — confirm the choice.")]));

// Appendix
body.push(H1("Appendix · Sources Consulted"));
body.push(P("Current API/tooling facts verified against:"));
body.push(bullet([link("NCBI E-utilities API & rate limits", "https://www.ncbi.nlm.nih.gov/home/develop/api/")]));
body.push(bullet([link("NCBI E-utilities API keys (3–10 rps)", "https://support.nlm.nih.gov/kbArticle/?pn=KA-05317")]));
body.push(bullet([link("Europe PMC RESTful Web Service", "https://europepmc.org/RestfulWebService")]));
body.push(bullet([link("bioRxiv / medRxiv API", "https://api.biorxiv.org/")]));
body.push(bullet([link("Embedding comparison for biomedical retrieval", "https://document360.com/blog/text-embedding-model-analysis/")]));
body.push(spacer());
body.push(new Paragraph({
  border: { top: { style: BorderStyle.SINGLE, size: 4, color: "BFBFBF", space: 4 } },
  spacing: { before: 120 },
  children: [new TextRun({ text: "Grounded in the current research-coordinator repository (router.py, gradio_ui.py, agents.yaml, prompts.yaml, eval/) as of June 15, 2026.", italics: true, size: 18, color: "595959" })],
}));

// ---------- assemble ----------
const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: FONT, color: "1F3864" },
        paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 25, bold: true, font: FONT, color: "2E5496" },
        paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, font: FONT, color: "404040" },
        paragraph: { spacing: { before: 140, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
      { reference: "nums", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
    headers: { default: new Header({ children: [new Paragraph({
      alignment: AlignmentType.RIGHT,
      border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: "BFBFBF", space: 4 } },
      children: [new TextRun({ text: "Literature Review Agent — Technical Plan & Build Brief", size: 16, color: "808080" })],
    })] }) },
    footers: { default: new Footer({ children: [new Paragraph({
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      children: [
        new TextRun({ text: "PDAC Research Agent System", size: 16, color: "808080" }),
        new TextRun({ text: "\t", size: 16 }),
        new TextRun({ text: "Page ", size: 16, color: "808080" }),
        new TextRun({ children: [PageNumber.CURRENT], size: 16, color: "808080" }),
      ],
    })] }) },
    children: body,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync("Literature_Review_Agent_Technical_Plan.docx", buf);
  console.log("written");
});
