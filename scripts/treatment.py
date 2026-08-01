#!/usr/bin/env python3
"""Georgia Appellate Watch treatment sweep (scripts/treatment.py).

A reverse citation sweep. For each card in opinions.json, ask CourtListener which
LATER in-scope appellate opinions cite it, and have a model read each new citing
passage and judge whether it treats the card's proposition adversely (overruled,
reversed, abrogated, superseded by statute, limited, disapproved, criticized, or
narrowed).

This is a TRIPWIRE, not a citator. CourtListener gives citation existence, not
Shepard's-grade treatment, and the model's read is a screen. So the sweep only
ever RAISES a card to "caution" and records what it found; it never confirms
negative treatment and never marks a card dead. A human clears each flag on
Shepard's (Gate 2) and, by editing opinions.json, promotes "caution" to
"negative" or "superseded", or back to "ok". Through treatment_core it never
downgrades a human setting and never re-raises a citing case already recorded, so
a cleared flag stays cleared.

This is the thorough weekend backstop to the daily forward escalation in
update.py: it walks the full citation graph, including criminal and out-of-scope
citers the daily screen drops before triage ever sees them.

Budget. Every CourtListener REST call routes through cl_rate (the shared budget:
configurable per-minute, per-hour, and per-day limits, paced as rolling windows).
The sweep spends REST only on DISCOVERY: one call to resolve each card's lead
opinion id (cached in state the moment it is resolved), and one or two to find its
citers. Each citing opinion's TEXT comes from its free PDF (the same trick the
daily pipeline uses), not a REST call, falling back to REST only when a PDF will
not extract. Because the weekly run gets a long wall-clock budget
(TREATMENT_BUDGET_SEC), when an hourly window fills the run WAITS for it to refill
and keeps going, draining a backlog in one run with no second trigger; it is
bounded by that budget, by the per-day limit, and by the job timeout. Only a
backlog larger than a day's limit defers its tail to the next run. Built to run on
a weekend, when the daily updater is idle and the budget is free.

Scope. Only citers from the feed's own courts (Supreme Court of Georgia, Court of
Appeals of Georgia, Eleventh Circuit, U.S. Supreme Court, plus the supplementary
Florida and Alabama appellate courts) count: only a court in the case's own
hierarchy can treat it adversely. A card is swept across its full
citation history the first time, and only for recent citers thereafter.

State (treatment_state.json) holds, per card, the resolved lead opinion id and the
set of citing clusters already evaluated. It changes only when new citers are
seen, so a quiet week writes nothing and opens no PR.

Reuses update.py (CourtListener + Anthropic plumbing, PDF extraction), render.py,
treatment_core.py (the shared flag model), and cl_rate.py (the REST budget).

Env:
  ANTHROPIC_API_KEY        required
  COURTLISTENER_TOKEN      recommended (citation search; raises the REST limit)
  TREATMENT_MODEL          classifier (default claude-sonnet-5)
  TREATMENT_LOOKBACK_DAYS  recent-citer window for already-swept cards (default 200)
  TREATMENT_PER_CARD       max new citers classified per card per run (default 6)
  TREATMENT_PER_RUN        max new citers classified per run, all cards (default 25)
  TREATMENT_PAGES          citer search pages per card, 20 per page (default 2)
  TREATMENT_FIRST_PER_RUN  never-swept cards given an unbounded full-history crawl per run
                           (default 3); the rest keep their never-swept state for a later run
  TREATMENT_PAGE_LOG_EVERY print a progress line every Nth citation page (default 10)
  TREATMENT_BUDGET_SEC     wall-clock cap; also how long one run will wait across rate
                           windows to drain a backlog (default 900; the workflow raises it for the weekly sweep)
  TREATMENT_MAXCHARS       citing-opinion characters sent to the classifier (default 9000)
  TREATMENT_PDF_MIN_CHARS  min extracted PDF chars to use before REST fallback (default 500)
  TREATMENT_BREAKER        stop after this many consecutive model-call failures (default 4)
  TREATMENT_PENDING_TRIES  give up on an individually-failing citer after this many runs (default 4)
  CL_PER_MINUTE / CL_PER_HOUR / CL_PER_DAY / CL_RATE_MARGIN  REST budget (see cl_rate.py)
  DRY_RUN=1                evaluate and print; write nothing, open no PR
  OPINIONS_DEBUG=1         verbose (inherited from update.py)

Run via .github/workflows/treatment.yml (weekend cron + manual dispatch).
"""
import os, re, sys, json, time, html, datetime
import urllib.parse
import update            # CourtListener + Anthropic plumbing, PDF extraction, helpers
import render            # single source of truth renderer
import treatment_core    # shared treatment-flag model
import cl_rate           # shared CourtListener REST budget (limits, pacing, defer)
import safeio            # crash-safe atomic writes
import batch             # Message Batches transport (per-card classify batch when TREATMENT_BATCH is on)

JSON_PATH  = update.JSON_PATH
STATE_PATH = os.path.join(update.REPO, "treatment_state.json")
PR_PATH    = os.path.join(update.REPO, "scripts", "treatment_pr_body.md")

KEY           = update.KEY
MODEL         = os.environ.get("TREATMENT_MODEL", "claude-sonnet-5")
LOOKBACK_DAYS = int(os.environ.get("TREATMENT_LOOKBACK_DAYS", "200"))
PER_CARD      = int(os.environ.get("TREATMENT_PER_CARD", "6"))
PER_RUN       = int(os.environ.get("TREATMENT_PER_RUN", "25"))
PAGES         = int(os.environ.get("TREATMENT_PAGES", "2"))
# How many never-swept cards may take their unbounded full-history crawl in ONE run.
#
# A card with no completed full pass pages its ENTIRE citation history (max_pages=None).
# That is correct -- a partial first pass would mark the card swept while an older
# overruling went unexamined -- but it is also the only unbounded work here, and the
# never-swept cards are ordered first. They accumulate: a run that exhausts PER_RUN or the
# REST budget before reaching them leaves them for next week, so the pile grows while the
# per-run cost of clearing it grows with it. By 2026-08-01 nine cards were waiting, some
# from 06-24, and successful-run durations had climbed 1:00 -> 2:02 across five weeks.
#
# Capping them per run drains the backlog over several weeks at a bounded cost each time,
# instead of one run attempting every full crawl at once. Cards past the cap are simply not
# visited; they keep their never-swept state and come first again next run, so nothing is
# marked done that was not actually swept.
FIRST_PER_RUN = int(os.environ.get("TREATMENT_FIRST_PER_RUN", "3"))
# Page-progress cadence for the full-history crawl. Every page for the first few, then
# every Nth, so a long crawl leaves a trail without burying the classifications.
PAGE_LOG_EVERY = int(os.environ.get("TREATMENT_PAGE_LOG_EVERY", "10"))
BUDGET_SEC    = int(os.environ.get("TREATMENT_BUDGET_SEC", "900"))
MAXCHARS      = int(os.environ.get("TREATMENT_MAXCHARS", "9000"))
# When passage() cannot locate the cited case in the citing opinion by a distinctive party name, it
# hands the classifier a WIDE contiguous slice (this many chars) instead of only the opening. The
# opening rarely contains the discussion of the cited case, so classifying it yields a false
# "neutral" -- which then marks the citer seen forever and silently misses a real overruling. A wide
# slice makes a "neutral" from the not-located path trustworthy. Bounded so a very long opinion does
# not blow the token budget.
WIDE_MAXCHARS = int(os.environ.get("TREATMENT_WIDE_MAXCHARS", str(3 * MAXCHARS)))
# Ubiquitous case-caption words that must NOT be used to anchor the passage window: they match the
# first occurrence of an everyday word (the opinion says "state"/"city"/"in re" constantly), pinning
# the window on noise unrelated to the cited case. We anchor on the first DISTINCTIVE token per side
# instead (typically a party surname), and if none is present fall back to the wide slice above.
_CAPTION_STOP = frozenset((
    "the", "of", "and", "for", "in", "re", "ex", "rel", "state", "states", "city", "county",
    "town", "village", "estate", "matter", "interest", "united", "people", "commonwealth",
    "department", "dept", "board", "commission", "authority", "company", "co", "inc", "llc",
    "corp", "corporation", "ltd", "lp", "llp",
))
PDF_MIN_CHARS = int(os.environ.get("TREATMENT_PDF_MIN_CHARS", "500"))
BREAKER       = int(os.environ.get("TREATMENT_BREAKER", "4"))   # stop after this many consecutive API failures
PENDING_TRIES = int(os.environ.get("TREATMENT_PENDING_TRIES", "4"))  # give up on a citer after this many failed classify runs
# Route each card's citer classifications through the 50%-priced Batch API. ON by default: the weekly
# sweep is latency-tolerant (a held card is not urgent), so half price is a clear win. A card's
# qualifying citers are collected (text fetched, gates + caps applied) and classified as ONE job, then
# the verdicts are processed exactly as the synchronous path does. Set TREATMENT_BATCH=0 for the
# synchronous path. A batch that fails or times out defers that card's citers -- and, like any stop,
# leaves the card NOT marked fully-swept, so its history is re-searched next run (never skipped).
BATCH         = os.environ.get("TREATMENT_BATCH", "on").strip().lower() in ("1", "true", "yes", "on")
BATCH_SEC     = int(os.environ.get("TREATMENT_BATCH_SEC", "600"))  # wait budget for a card's classify batch; fits the sweep job
DRY_RUN       = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")

# CourtListener court ids whose decisions can bind or treat a card in our feed.
SCOPE_COURTS = update.COURTS_ALL   # full CL id set across all registered jurisdictions (ignores the OPINIONS_COURTS override)
COURT_MAP    = update.COURT_MAP    # CL court id -> our internal code

TREATMENT_SYSTEM = (
    "You screen how a LATER court opinion treats an EARLIER case, for a citation-"
    "treatment tripwire. You are given (A) the earlier CITED case: its name and a "
    "short statement of the proposition it stands for in our feed, and (B) a "
    "passage from a later opinion that cites it. Decide how the later opinion "
    "treats the cited case AS TO THAT PROPOSITION.\n\n"
    "Call it NEGATIVE only if the later opinion actually undercuts the cited "
    "proposition: it overrules, reverses, abrogates, holds it superseded by "
    "statute, limits or narrows its rule, disapproves it, or criticizes it as "
    "wrongly decided. Distinguishing the case on its facts WITHOUT narrowing the "
    "rule is NOT negative. Following, applying, explaining, or citing it in "
    "support is POSITIVE. A bare string cite, or a mention that does not engage "
    "the proposition, is NEUTRAL.\n\n"
    "Set affects_proposition true only if the treatment actually bears on the "
    "proposition in (A), not some other point the cited case also made. This is a "
    "screen for human Shepard's review, so do catch genuine negative treatment, "
    "but do not invent treatment the passage does not contain; if the passage does "
    "not clearly engage the cited proposition, return neutral.\n\n"
    "Output ONLY a JSON object with keys: treatment ('positive', 'neutral', or "
    "'negative'); kind (one of overruled, reversed, abrogated, superseded by "
    "statute, limited, disapproved, criticized, distinguished-narrowing, or null); "
    "affects_proposition (true or false); note (one neutral sentence, no case "
    "citations, on how the later opinion treats the cited case); confidence "
    "('high', 'medium', or 'low')."
)


def lead_opinion_id(cluster_id, deadline):
    """Lead (first) sub-opinion id for a cluster, via the clusters endpoint."""
    cl = update.cl_get("/api/rest/v4/clusters/%d/" % int(cluster_id), deadline)
    for s in (cl.get("sub_opinions") or []):
        m = re.search(r"/opinions/(\d+)/", s) if isinstance(s, str) else None
        if m:
            return int(m.group(1))
    return None


def first_time_allowed(first_done, cap=None):
    """Whether another never-swept card may take its unbounded full-history crawl this run.

    Split out of the loop so the budget is testable on its own. A cap of 0 means no card
    gets a full crawl this run -- valid, and the way to force a sweep to stay incremental
    while a backlog problem is being worked on.
    """
    return first_done < (FIRST_PER_RUN if cap is None else cap)


def log_this_page(pages, every=None):
    """Whether page `pages` of a citation crawl should print. First page always -- the point
    is to mark that the crawl STARTED, which is what three dead runs could not tell us."""
    n = PAGE_LOG_EVERY if every is None else every
    return pages == 1 or (n > 0 and pages % n == 0)


def citing_results(opinion_id, since, deadline, max_pages=None):
    """In-scope opinions citing opinion_id, filed on/after `since`, newest first.

    Uses the search `cites:(id)` query with repeated court params (the REST API accepts repeated court=
    filters). Returns (results, exhausted): `exhausted` is True only when the search reached the end
    (no further page) within `max_pages`. `max_pages=None` pages the ENTIRE history -- a first,
    full-history sweep must see every citer, not just the newest page, or a card with more citers than
    one page is marked fully-swept while its older citers (possibly an overruling) are never examined.
    Incremental runs pass max_pages=PAGES to stay cheap in the recent window.
    """
    params = [("type", "o"), ("q", "cites:(%d)" % int(opinion_id)),
              ("filed_after", since), ("order_by", "dateFiled desc"), ("page_size", "20")]
    params += [("court", c) for c in SCOPE_COURTS]
    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params)
    out, pages = [], 0
    while url and (max_pages is None or pages < max_pages):
        data = update.cl_get(url, deadline)
        out += data.get("results", [])
        url = data.get("next")
        pages += 1
        # Progress, not decoration. An unbounded full-history sweep prints nothing until it
        # finishes -- the loop below only speaks once a citer is classified -- so three runs
        # died mid-crawl (exit 143, runner shutdown) leaving a log whose last line was the
        # Anthropic status check. There was no way to tell which card, or how deep, from the
        # outside. Every page now leaves a mark, so the next failure names its own position.
        if log_this_page(pages):
            print("    ~ citation page %d (%d citer(s) so far)%s"
                  % (pages, len(out), "" if url else "; last page"), flush=True)
        time.sleep(0.5)
    if max_pages is not None and url:
        print("    ~ stopped at the %d-page cap; older citers roll to a later run" % pages,
              flush=True)
    return out, (url is None)   # exhausted iff no further page remains


def _rest_opinion_text(oid, deadline):
    """REST fallback for opinion text (deadline-guarded and paced via cl_get)."""
    o = update.cl_get("/api/rest/v4/opinions/%s/" % oid, deadline)
    for f in ("plain_text", "html_with_citations", "html", "xml_harvard", "html_lawbox", "html_columbia"):
        v = o.get(f)
        if v:
            if f != "plain_text":
                v = html.unescape(re.sub(r"<[^>]+>", " ", v))
            return re.sub(r"[ \t]+", " ", v).strip()
    return ""


def citer_text(r, deadline):
    """Text of a citing opinion. PDF first (a free static fetch, no REST quota),
    falling back to a REST call only when the PDF will not extract and the shared
    budget has room. The REST fallback reads every sub-opinion (lead plus any
    concurrences and dissents), so a citation discussed in a writing other than the
    first is not missed. A RateBudgetExceeded propagates so the run can defer."""
    ops = r.get("opinions") or []
    op0 = ops[0] if ops and isinstance(ops[0], dict) else {}
    pdf_url = op0.get("download_url") or ""
    text = update.pdf_text(pdf_url, deadline=deadline) if pdf_url else ""
    if len(text) >= PDF_MIN_CHARS and sum(c.isalpha() for c in text) >= 100:
        return text
    try:
        return update.opinion_text_full(r, deadline)
    except cl_rate.RateBudgetExceeded:
        raise
    except Exception:
        return ""


def _anchor_spans(text, name):
    """Char spans in `text` around where the cited case is discussed, located by the first DISTINCTIVE
    party token on each side of the caption's "v." -- distinctive meaning not a ubiquitous caption
    word (_CAPTION_STOP), which would pin the window on the first everyday "state"/"in re"/"co." in
    the opinion rather than on the cited case. Empty when no distinctive token is present in the text
    (e.g. the citer refers to the case only by reporter citation or a mangled name)."""
    low = text.lower()
    spans = []
    for side in re.split(r"\bv\.?\b", name, maxsplit=1):
        for w in re.findall(r"[A-Z][A-Za-z'&.-]{2,}", side):
            if w.lower().strip(".") in _CAPTION_STOP:
                continue
            i = low.find(w.lower())
            if i >= 0:
                spans.append((max(0, i - 1200), min(len(text), i + 1800)))
                break  # this side is anchored; don't add spurious spans for its other tokens
    return spans


def passage(text, name):
    """A window of the citing opinion around where the cited case is discussed, located by the cited
    case's distinctive party name(s). When the case cannot be located that way, return a WIDE
    contiguous slice (not merely the opening): the opening rarely contains the discussion of the
    cited case, so classifying it produces a false 'neutral' that then marks the citer permanently
    seen and silently misses a real overruling. The wide slice keeps a not-located 'neutral'
    trustworthy. `located()` exposes which path was taken for the caller's observability."""
    if not text:
        return ""
    spans = _anchor_spans(text, name)
    if not spans:
        return text[:WIDE_MAXCHARS]
    spans.sort()
    chunks, used = [], 0
    for a, b in spans:
        if used >= MAXCHARS:
            break
        seg = text[a:b]
        chunks.append(seg)
        used += len(seg)
    return ("\n...\n".join(chunks))[:MAXCHARS]


def located(text, name):
    """True if passage() could anchor on the cited case's distinctive party name in `text` (vs. having
    to fall back to the wide slice). The sweep logs the not-located citers so the residual risk -- a
    long opinion whose adverse discussion sits past the wide-slice cap -- is observable, not silent."""
    return bool(text) and bool(_anchor_spans(text, name))


def classify_request(card, citing_name, citing_text):
    """The Messages body for one treatment classification. Shared by the synchronous classify() and
    the per-card batch, the same request/transport split update.summarize_request uses."""
    prop = "%s\nProposition: %s\nWhy it matters: %s" % (
        card.get("name", ""), card.get("synopsis", ""), card.get("why", ""))
    body = passage(citing_text, card.get("name", "")) or "(no text available)"
    user = ("CITED CASE (A):\n%s\n\nLATER OPINION THAT CITES IT (B) -- %s:\n%s"
            % (prop, citing_name, body))
    return {"model": MODEL, "max_tokens": 400, "system": TREATMENT_SYSTEM,
            "messages": [{"role": "user", "content": user}]}


def classify(card, citing_name, citing_text):
    return update.anthropic_json(classify_request(card, citing_name, citing_text), "treatment")


def sweep_since(card, full_done, today=None):
    """Lower bound for a card's citation search. Until a full-history pass has
    completed (full_done), search from the card's own filing date so nothing older
    is missed; after that, only the cheap LOOKBACK_DAYS incremental window. Pure so
    the full-vs-incremental decision (the bug this guards) is unit-testable."""
    if not full_done:
        return card["date"]
    today = today or datetime.date.today()
    return (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()


def swept_full(full_done, stopped, truncated=False):
    """Whether a card is marked as having a completed full-history pass after this run. Once true it
    stays true; otherwise it becomes true only if the pass ran to completion -- neither a global stop
    (`stopped`: a rate/time/breaker/config halt) NOR a `truncated` pass (the citer search hit its page
    cap with more citers behind it, or the per-card/per-run cap cut the collect loop short) may set it,
    since either left history unexamined. Leaving it unset makes the next run redo the full-history
    search, resuming past the citers already in `seen`."""
    return bool(full_done or (not stopped and not truncated))


def _pending_rec(r, tries):
    """Trim a citing search result to the minimum needed to re-fetch its text and re-classify it on a
    later run: cluster id, name, date, court, and the sub-opinion ids + PDF urls citer_text relies on
    (PDF-first, REST fallback). `_tries` counts genuine per-citer classification failures toward
    PENDING_TRIES. Kept tiny because it is persisted in treatment_state.json (committed to git)."""
    ops = [{"id": o.get("id"), "download_url": o.get("download_url")}
           for o in (r.get("opinions") or []) if isinstance(o, dict)]
    return {"cluster_id": update.cluster_id_of(r),
            "caseName": r.get("caseName") or r.get("caseNameFull") or "(unnamed)",
            "dateFiled": r.get("dateFiled"), "court_id": r.get("court_id"),
            "opinions": ops, "_tries": int(tries)}


def _pending_key(recs):
    """Order-independent identity of a pending list -- (ccid, tries) pairs -- so a run can tell whether
    the pending set actually changed and skip a no-op state write."""
    out = []
    for r in recs or []:
        cid = update.cluster_id_of(r)
        if cid:
            out.append((cid, int(r.get("_tries", 0))))
    return sorted(out)


def _classify_batch(card, collect, deadline):
    """Classify a card's collected citers as ONE 50%-priced batch. `collect` is a list of dicts, each
    {ccid, cname, cdate, ccourt, ctext}. Returns (verdicts, ok): `verdicts` maps ccid -> the parsed
    verdict for each citer that succeeded (a per-result batch error or an unparseable body is omitted,
    so that citer stays unseen and retries next run -- the same outcome a synchronous per-citer
    failure gets); `ok` is False only when the WHOLE batch timed out or failed transport, so the
    caller defers the card and leaves it not-fully-swept. Isolated from main() so the orchestration is
    unit-testable (test_treatment) without a live sweep."""
    reqs = [batch.from_body(str(c["ccid"]), classify_request(card, c["cname"], c["ctext"]))
            for c in collect]
    try:
        res = batch.run(reqs, deadline=deadline, label="treatment-batch")
    except (batch.BatchTimeout, batch.BatchError) as be:
        print("  ! treatment classify batch deferred (%s); %d citer(s) retry next run" % (be, len(collect)))
        return {}, False
    verdicts = {}
    for c in collect:
        ccid = c["ccid"]
        rr = res.get(str(ccid))
        if not rr or not rr.get("ok"):
            print("  . treatment classify unavailable citing=%s (%s); retry next run"
                  % (ccid, (rr or {}).get("type")))
            continue
        try:
            verdicts[ccid] = update.parse_json(rr["text"])
        except Exception as pe:
            print("  ! treatment classify unparseable citing=%s: %s" % (ccid, pe))
    return verdicts, True


def main():
    if not KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)

    # Guarantee the PR body exists on every exit path (the workflow reads it).
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No treatment changes this run.\n")

    # Anthropic status preflight (shared with the daily). On a confirmed API outage,
    # skip the sweep cleanly rather than spending CourtListener and model budget on
    # calls that will fail. Fail-open: unknown/unreachable status proceeds.
    slevel, sdesc = update.anthropic_status()
    print("Anthropic status: %s%s" % (sdesc, "" if slevel in ("operational", "unknown") else " [%s]" % slevel))
    if slevel == "outage" and update.STATUS_MODE == "on":
        print("  ! Anthropic API is in a reported outage; skipping this sweep. The next run will retry.")
        open(PR_PATH, "w", encoding="utf-8").write("Skipped: Anthropic API outage.\n")
        return

    entries = json.load(open(JSON_PATH, encoding="utf-8"))
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    if not isinstance(state, dict):
        state = {}

    run_start = time.time()
    deadline = run_start + BUDGET_SEC
    classified = 0
    report = []        # (card_name, citing_name, citing_date, verdict_str)
    stuck = []         # (card_name, citing_name, ccid, tries) citers given up after PENDING_TRIES failed runs
    new_flags = []     # card dicts newly raised to caution this run
    changed = False    # any tracked-file change (state grew, or a flag changed)
    stopped = ""       # why the run ended early, if it did
    defer = ""         # operator-facing detail when stopped on the rate budget
    first_done = 0     # never-swept cards given their full-history crawl this run
    deferred_first = 0 # never-swept cards left for a later run by FIRST_PER_RUN
    api_fail = 0       # consecutive model-call failures (circuit breaker)

    # Never-swept cards first (full history), oldest first within each group so the
    # most-cited landmarks are worked through across the early runs.
    order = sorted(entries, key=lambda e: (str(e.get("cluster_id")) in state, e.get("date", "")))

    for card in order:
        if classified >= PER_RUN:
            break
        if time.time() - run_start > BUDGET_SEC:
            stopped = "time budget"; break
        cid = card.get("cluster_id")
        if not cid:
            continue
        if (card.get("treatment") or "ok") == "superseded":
            continue                                   # already dead; stop spending on it
        key = str(int(cid))
        st = state.get(key) or {}
        seen = set(st.get("seen", []))
        full_done = bool(st.get("full"))
        pending_in = list(st.get("pending") or [])   # citers awaiting individual re-classification (option b)
        # first_time == run a full-history citation search (since the card's own date),
        # vs. the cheap LOOKBACK_DAYS incremental window. Gate it on whether a full pass
        # has actually COMPLETED, not on mere presence in state: a run that resolves the
        # oid then defers on the rate/time budget before (or during) the citer search must
        # not leave the card marked done -- otherwise the next run drops to the 200-day
        # window and the card's older history is never searched, silently missing an
        # overruling decision filed before that window. Entries predating this flag have no
        # "full" key, so they each get one corrective full-history pass.
        first_time = not full_done

        # Bounded backlog drain: only FIRST_PER_RUN never-swept cards take their unbounded
        # full-history crawl per run. Skipped ones are left untouched -- not marked, not
        # partially swept -- so they sort first again next run and lose nothing.
        if first_time:
            if not first_time_allowed(first_done):
                deferred_first += 1
                continue
            first_done += 1

        # One line per card, before any network call. Three runs died inside the work below
        # with a log that ended at the Anthropic status check; from the outside there was no
        # way to say which card was being swept. This is the line that answers that.
        print("  > %s %s (%s)%s"
              % ("full-history" if first_time else "incremental",
                 (card.get("name") or "?")[:56], card.get("date") or "?",
                 "" if first_time else " since %s" % sweep_since(card, full_done)),
              flush=True)

        try:
            oid = st.get("oid")
            if oid is None:
                oid = lead_opinion_id(int(cid), deadline)
                if oid:
                    state[key] = {"oid": oid, "seen": sorted(seen), "full": full_done, "pending": pending_in}   # cache id immediately
                    changed = True
            if not oid:
                state[key] = {"oid": None, "seen": sorted(seen), "full": full_done, "pending": pending_in}      # do not refetch weekly
                changed = True
                continue
            since = sweep_since(card, full_done)
            # First (full-history) sweep pages the ENTIRE citation history so no older citer is
            # orphaned; an already-full incremental run stays in the cheap PAGES-deep recent window.
            citers, exhausted = citing_results(int(oid), since, deadline,
                                               max_pages=(None if first_time else PAGES))
        except cl_rate.RateBudgetExceeded:
            stopped = "rest budget"; defer = cl_rate.PACER.defer_note(); break
        except Exception as e:
            print("  ! citation lookup failed for %s (%s): %s" % (cid, card.get("name"), e))
            continue

        before = set(seen)
        # Re-attempt this card's still-unclassified citers FIRST (option b): a citer that failed its
        # individual classification on a prior run is tracked in `pending` with a stored r-shape and
        # re-swept here even after the card is marked full -- an incremental run's narrow LOOKBACK
        # window would otherwise strand a citer filed before it. Deduped against the fresh search so a
        # pending citer still inside the window is not processed twice.
        work, work_ids = [], set()
        for r in list(pending_in) + citers:            # pending first, then the fresh search (newest first)
            rid = update.cluster_id_of(r)
            if not rid or rid in work_ids:
                continue
            work_ids.add(rid)
            work.append(r)

        # Collect this card's qualifying citers -- fetch text, apply the same gates and the per-card /
        # per-run caps -- then classify them together (one 50%-priced batch when TREATMENT_BATCH is
        # on, else synchronously). Splitting collection from classification is what lets the whole
        # card go through the batch API in one job; the gates and the seen/full state below are
        # unchanged. collect: list of {ccid, cname, cdate, ccourt, ctext, r} dicts.
        collect = []
        cap_truncated = False
        for r in work:                                 # pending, then newest-first fresh citers
            if classified + len(collect) >= PER_RUN or len(collect) >= PER_CARD:
                cap_truncated = True                   # cut short by a cap, not because work ran out
                break
            if time.time() - run_start > BUDGET_SEC:
                stopped = "time budget"; break
            ccid = update.cluster_id_of(r)
            if not ccid or ccid == int(cid) or ccid in seen:
                continue
            if (r.get("court_id") or "") not in SCOPE_COURTS:
                continue
            cname = r.get("caseName") or r.get("caseNameFull") or "(unnamed)"
            cdate = (r.get("dateFiled") or "")[:10]
            ccourt = r.get("court_id") or ""
            try:
                ctext = citer_text(r, deadline)
            except cl_rate.RateBudgetExceeded:
                stopped = "rest budget"; defer = cl_rate.PACER.defer_note(); break
            # CourtListener's text ingestion lags cluster creation by days to weeks. If the
            # citer's text is not available yet, skip it WITHOUT marking it seen, so it is
            # re-examined on a later run once its text -- possibly the very passage that
            # overrules the card -- lands, rather than classifying an empty body as "neutral"
            # and never revisiting it. (Mirrors citer_text's own PDF min-alpha gate.)
            if sum(c.isalpha() for c in ctext) < 100:
                print("  . citing=%s text not ingested yet; will retry next run" % ccid)
                continue
            collect.append({"ccid": ccid, "cname": cname, "cdate": cdate, "ccourt": ccourt, "ctext": ctext, "r": r})

        # Classify the collected citers. verdicts maps ccid -> parsed verdict; a citer absent from it
        # was deferred (not marked seen) and retries next run. A whole-batch failure, a rate-budget
        # stop, a config error, or the breaker sets `stopped`, so the card is NOT marked fully-swept
        # below -- its history is re-searched next run, never silently skipped.
        verdicts = {}
        if collect and BATCH:
            # Mirror the synchronous branch's resilience: _classify_batch already handles a batch
            # timeout / transport failure (ok=False), but a ConfigError must still surface the run
            # loudly, and any other unexpected error must defer the card (stopped) rather than crash
            # the sweep. Either way the card is left not-fully-swept and re-searched next run.
            ok = False
            try:
                verdicts, ok = _classify_batch(card, collect, time.time() + BATCH_SEC)
            except update.ConfigError as e:
                print("  ! configuration error, stopping this sweep so it surfaces: %s" % e)
                stopped = "configuration error"
            except Exception as e:
                print("  ! treatment classify batch failed unexpectedly (%s); deferring the card" % e)
            if not ok:
                stopped = stopped or "treatment batch"
        elif collect:
            for c in collect:
                ccid = c["ccid"]
                try:
                    verdicts[ccid] = classify(card, c["cname"], c["ctext"])
                    api_fail = 0
                except cl_rate.RateBudgetExceeded:
                    stopped = "rest budget"; defer = cl_rate.PACER.defer_note(); break
                except update.ConfigError as e:
                    print("  ! configuration error, stopping this sweep so it surfaces: %s" % e)
                    stopped = "configuration error"; break
                except Exception as e:
                    api_fail += 1
                    print("  ! classify failed citing=%s: %s" % (ccid, e))
                    if api_fail >= BREAKER:
                        stopped = "Anthropic API errors"
                        print("  ! %d consecutive model-call failures; stopping early. "
                              "Remaining cards roll to the next run." % api_fail)
                        break
                    continue

        # Process the verdicts in collection order (newest first); seen/report/flag logic unchanged.
        for c in collect:
            v = verdicts.get(c["ccid"])
            if v is None:
                continue                               # deferred/failed -> not seen, retried next run
            ccid, cname, cdate, ccourt = c["ccid"], c["cname"], c["cdate"], c["ccourt"]
            seen.add(ccid)
            classified += 1
            t = (v.get("treatment") or "neutral").lower()
            # Observability for the residual not-located case: a 'neutral' reached on the wide-slice
            # fallback (the cited case's distinctive name was not found in this citer's text) is the
            # one place a long opinion could hide adverse treatment past the cap. Surface it so a
            # silent miss becomes a visible line rather than an invisible "ok".
            if t == "neutral" and not located(c["ctext"], card.get("name", "")):
                print("  ~ neutral on wide fallback (cited case not located by name) citing=%s" % ccid)
            kind = (v.get("kind") or "").lower().strip() or None
            note = (v.get("note") or "").strip()
            conf = (v.get("confidence") or "").lower()
            verdict = t + ("/" + kind if kind else "") + (" (conf %s)" % conf if conf else "")
            report.append((card.get("name", ""), cname, cdate, verdict))
            print("  . %s <- %s (%s): %s" % (card.get("name", "")[:42], cname[:42], cdate, verdict))
            if t == "negative" and bool(v.get("affects_proposition")) and (kind in treatment_core.NEGATIVE_KINDS):
                citer = {"cluster_id": ccid, "name": cname, "court": COURT_MAP.get(ccourt, ccourt),
                         "date": cdate, "kind": kind, "note": note}
                if treatment_core.flag_caution(card, citer):
                    new_flags.append(card)

        # --- option (b): recompute this card's per-citer pending list ---------------------------------
        # A citer that was ATTEMPTED (fetched, gated, put in `collect`) but yielded no verdict failed
        # its individual classification -- a bad model read, an unparseable body, or a per-result batch
        # error. Track it by id so it is re-swept next run even after the card is marked full; the
        # narrow incremental window would otherwise strand a citer filed before it. A global stop
        # (rate/time/breaker/config/whole-batch defer) is NOT the citer's fault, so it never burns a
        # try. After PENDING_TRIES genuine failures a citer is given up: marked seen so it stops
        # recurring, and surfaced in the PR for manual review -- bounded cost, never a silent drop.
        attempted = {c["ccid"] for c in collect}
        succeeded = set(verdicts)
        prev_pending = {}
        for rec in pending_in:
            pid = update.cluster_id_of(rec)
            if pid:
                prev_pending[pid] = rec
        new_pending = {}
        for pid, rec in prev_pending.items():
            if pid in succeeded:
                continue                                       # classified now -> resolved (already seen)
            if not stopped and pid in attempted:
                tries = int(rec.get("_tries", 0)) + 1
                if tries >= PENDING_TRIES:
                    seen.add(pid)                              # give up: stop recurring, surface for a human
                    stuck.append((card.get("name", ""), rec.get("caseName") or "(unnamed)", pid, tries))
                    continue
                rec = dict(rec, _tries=tries)
            new_pending[pid] = rec                             # bumped, or preserved (not attempted / stopped)
        for c in collect:
            ccid = c["ccid"]
            if ccid in succeeded or ccid in prev_pending:
                continue                                       # succeeded, or already handled above
            new_pending[ccid] = _pending_rec(c["r"], 0 if stopped else 1)   # a fresh individual failure
        pending = list(new_pending.values())

        if first_time or seen != before or _pending_key(pending) != _pending_key(pending_in):
            # Mark the card fully swept only when this run actually completed a full-history pass: no
            # global stop (rate/time/breaker/config), no unpaged search tail (exhausted), and no cap
            # cutting the collect loop short. A first-sweep card with more citers than a run's caps can
            # process therefore stays full=False and resumes next run (past `seen`) instead of stranding
            # its older, unexamined citers behind the incremental window. An incremental run keeps the
            # existing flag regardless (full_done dominates).
            truncated = cap_truncated or not exhausted
            new_full = swept_full(full_done, stopped, truncated)
            state[key] = {"oid": oid, "seen": sorted(seen), "full": new_full, "pending": pending}
            changed = True
        if stopped:
            break

    # --- PR body ---
    lines = ["## Georgia Appellate Watch: treatment sweep", ""]
    if new_flags:
        lines.append("**Newly flagged for Shepard's review (raised to caution):**")
        for c in new_flags:
            tb = c.get("treated_by") or []
            last = tb[-1] if tb else {}
            lines.append("- **%s** -- possibly %s by %s (%s). Verify on Shepard's, then set `treatment` "
                         "to negative or superseded, or back to ok."
                         % (c.get("name", ""), last.get("kind", "negative"),
                            last.get("name", "a later case"), last.get("date", "")))
        lines.append("")
    else:
        lines += ["No new adverse treatment detected this run.", ""]
    if report:
        lines.append("Citing opinions reviewed this run (logged, not all adverse):")
        for cardnm, cname, cdate, verdict in report:
            lines.append("- %s <- %s (%s): %s" % (cardnm, cname, cdate, verdict))
    if stuck:
        lines += ["", "**Could not auto-classify after %d attempts -- CHECK MANUALLY on CourtListener:**" % PENDING_TRIES]
        for cardnm, cname, ccid, tries in stuck:
            lines.append("- %s <- %s -- https://www.courtlistener.com/opinion/%d/x/ "
                         "(%d failed classify attempts; marked reviewed to stop retrying)"
                         % (cardnm, cname, ccid, tries))
    if deferred_first:
        lines += ["", "_%d never-swept card(s) deferred: only %d full-history crawl(s) run per "
                  "sweep (TREATMENT_FIRST_PER_RUN). They are untouched and come first next run._"
                  % (deferred_first, FIRST_PER_RUN)]
    if stopped:
        lines += ["", "_Run stopped early (%s%s); remaining cards roll to the next run._"
                  % (stopped, "; " + defer if defer else "")]
    pr_body = "\n".join(lines) + "\n"

    # Never a silent cap: a deferred card looks exactly like a card with nothing to report
    # unless the count is stated, and "swept everything" is the wrong thing to infer.
    print("\nclassified %d citing opinion(s); new flags: %d; CourtListener REST calls: %d%s%s%s"
          % (classified, len(new_flags), cl_rate.PACER.calls,
             ("; %d full-history crawl(s), %d card(s) deferred to a later run"
              % (first_done, deferred_first)) if deferred_first else "",
             (" (stopped: %s%s)" % (stopped, "; " + defer if defer else "")) if stopped else "",
             ("; %d citer(s) given up for manual review" % len(stuck)) if stuck else ""))

    safeio.step_summary(
        "## Georgia Appellate Watch \u00b7 treatment sweep\n\n"
        "Classified %d citing opinion(s); raised %d flag(s) to caution.%s\n\n"
        "CourtListener REST calls: %d%s"
        % (classified, len(new_flags),
           " \u00b7 %d citer(s) given up for manual review" % len(stuck) if stuck else "",
           cl_rate.PACER.calls,
           " \u00b7 run stopped early, remaining cards roll to the next run" if stopped else ""))

    if DRY_RUN:
        print("\n--- DRY RUN, nothing written ---\n" + pr_body)
        return

    open(PR_PATH, "w", encoding="utf-8").write(pr_body)
    if not changed and not new_flags:
        print("no new citing activity; files unchanged.")
        return

    # Persist progress always; rewrite opinions.json and re-render only when a flag
    # actually changed a card (new_flags) -- state-only weeks still record progress.
    safeio.atomic_write_json(STATE_PATH, {k: state[k] for k in sorted(state)})
    if new_flags:
        safeio.atomic_write_json(JSON_PATH, entries)
        render.render(entries)
        print("wrote treatment_state.json + opinions.json; re-rendered.")
    else:
        print("wrote treatment_state.json (progress only; no card changed).")


if __name__ == "__main__":
    main()
