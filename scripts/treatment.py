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
  TREATMENT_BUDGET_SEC     wall-clock cap; also how long one run will wait across rate
                           windows to drain a backlog (default 900; the workflow raises it for the weekly sweep)
  TREATMENT_MAXCHARS       citing-opinion characters sent to the classifier (default 9000)
  TREATMENT_PDF_MIN_CHARS  min extracted PDF chars to use before REST fallback (default 500)
  TREATMENT_BREAKER        stop after this many consecutive model-call failures (default 4)
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

JSON_PATH  = update.JSON_PATH
STATE_PATH = os.path.join(update.REPO, "treatment_state.json")
PR_PATH    = os.path.join(update.REPO, "scripts", "treatment_pr_body.md")

KEY           = update.KEY
MODEL         = os.environ.get("TREATMENT_MODEL", "claude-sonnet-5")
LOOKBACK_DAYS = int(os.environ.get("TREATMENT_LOOKBACK_DAYS", "200"))
PER_CARD      = int(os.environ.get("TREATMENT_PER_CARD", "6"))
PER_RUN       = int(os.environ.get("TREATMENT_PER_RUN", "25"))
PAGES         = int(os.environ.get("TREATMENT_PAGES", "2"))
BUDGET_SEC    = int(os.environ.get("TREATMENT_BUDGET_SEC", "900"))
MAXCHARS      = int(os.environ.get("TREATMENT_MAXCHARS", "9000"))
PDF_MIN_CHARS = int(os.environ.get("TREATMENT_PDF_MIN_CHARS", "500"))
BREAKER       = int(os.environ.get("TREATMENT_BREAKER", "4"))   # stop after this many consecutive API failures
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


def citing_results(opinion_id, since, deadline):
    """In-scope opinions citing opinion_id, filed on/after `since`, newest first.

    Uses the search `cites:(id)` query with repeated court params (the REST API
    accepts repeated court= filters), paginating up to PAGES pages.
    """
    params = [("type", "o"), ("q", "cites:(%d)" % int(opinion_id)),
              ("filed_after", since), ("order_by", "dateFiled desc"), ("page_size", "20")]
    params += [("court", c) for c in SCOPE_COURTS]
    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params)
    out, pages = [], 0
    while url and pages < PAGES:
        data = update.cl_get(url, deadline)
        out += data.get("results", [])
        url = data.get("next")
        pages += 1
        time.sleep(0.5)
    return out


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


def passage(text, name):
    """A window of the citing opinion around where the cited case is discussed,
    keyed on the cited case's party surnames. Falls back to the opening."""
    if not text:
        return ""
    toks = []
    for side in re.split(r"\bv\.?\b", name, maxsplit=1):
        w = re.findall(r"[A-Z][A-Za-z'&.-]{2,}", side)
        if w:
            toks.append(w[0])
    low = text.lower()
    spans = []
    for t in toks:
        i = low.find(t.lower())
        if i >= 0:
            spans.append((max(0, i - 1200), min(len(text), i + 1800)))
    if not spans:
        return text[:MAXCHARS]
    spans.sort()
    chunks, used = [], 0
    for a, b in spans:
        if used >= MAXCHARS:
            break
        seg = text[a:b]
        chunks.append(seg)
        used += len(seg)
    return ("\n...\n".join(chunks))[:MAXCHARS]


def classify(card, citing_name, citing_text):
    prop = "%s\nProposition: %s\nWhy it matters: %s" % (
        card.get("name", ""), card.get("synopsis", ""), card.get("why", ""))
    body = passage(citing_text, card.get("name", "")) or "(no text available)"
    user = ("CITED CASE (A):\n%s\n\nLATER OPINION THAT CITES IT (B) -- %s:\n%s"
            % (prop, citing_name, body))
    return update.anthropic_json(
        {"model": MODEL, "max_tokens": 400, "system": TREATMENT_SYSTEM,
         "messages": [{"role": "user", "content": user}]}, "treatment")


def sweep_since(card, full_done, today=None):
    """Lower bound for a card's citation search. Until a full-history pass has
    completed (full_done), search from the card's own filing date so nothing older
    is missed; after that, only the cheap LOOKBACK_DAYS incremental window. Pure so
    the full-vs-incremental decision (the bug this guards) is unit-testable."""
    if not full_done:
        return card["date"]
    today = today or datetime.date.today()
    return (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()


def swept_full(full_done, stopped):
    """Whether a card is marked as having a completed full-history pass after this
    run. Once true it stays true; otherwise it becomes true only if the pass ran to
    completion -- `stopped` (a global rate/time/breaker/config stop) truncating it
    leaves the flag unset so the next run redoes the full-history search."""
    return bool(full_done or not stopped)


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
    new_flags = []     # card dicts newly raised to caution this run
    changed = False    # any tracked-file change (state grew, or a flag changed)
    stopped = ""       # why the run ended early, if it did
    defer = ""         # operator-facing detail when stopped on the rate budget
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
        # first_time == run a full-history citation search (since the card's own date),
        # vs. the cheap LOOKBACK_DAYS incremental window. Gate it on whether a full pass
        # has actually COMPLETED, not on mere presence in state: a run that resolves the
        # oid then defers on the rate/time budget before (or during) the citer search must
        # not leave the card marked done -- otherwise the next run drops to the 200-day
        # window and the card's older history is never searched, silently missing an
        # overruling decision filed before that window. Entries predating this flag have no
        # "full" key, so they each get one corrective full-history pass.
        first_time = not full_done

        try:
            oid = st.get("oid")
            if oid is None:
                oid = lead_opinion_id(int(cid), deadline)
                if oid:
                    state[key] = {"oid": oid, "seen": sorted(seen), "full": full_done}   # cache id immediately
                    changed = True
            if not oid:
                state[key] = {"oid": None, "seen": sorted(seen), "full": full_done}      # do not refetch weekly
                changed = True
                continue
            since = sweep_since(card, full_done)
            citers = citing_results(int(oid), since, deadline)
        except cl_rate.RateBudgetExceeded:
            stopped = "rest budget"; defer = cl_rate.PACER.defer_note(); break
        except Exception as e:
            print("  ! citation lookup failed for %s (%s): %s" % (cid, card.get("name"), e))
            continue

        before = set(seen)
        per_card = 0
        for r in citers:                               # newest first
            if classified >= PER_RUN or per_card >= PER_CARD:
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
                v = classify(card, cname, ctext)
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

            seen.add(ccid)
            classified += 1
            per_card += 1
            t = (v.get("treatment") or "neutral").lower()
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

        if first_time or seen != before:
            # Mark the card fully swept only when this run actually completed a full-history
            # pass without a global stop (rate/time budget, breaker, config error) cutting it
            # short; `stopped` is set only by those global conditions, not by the per-card /
            # per-run classification caps. An incremental run keeps the existing flag. A stop
            # leaves `full` unset so the next run redoes the full-history search (skipping the
            # citers already in `seen`, so it resumes rather than restarts).
            new_full = swept_full(full_done, stopped)
            state[key] = {"oid": oid, "seen": sorted(seen), "full": new_full}
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
    if stopped:
        lines += ["", "_Run stopped early (%s%s); remaining cards roll to the next run._"
                  % (stopped, "; " + defer if defer else "")]
    pr_body = "\n".join(lines) + "\n"

    print("\nclassified %d citing opinion(s); new flags: %d; CourtListener REST calls: %d%s"
          % (classified, len(new_flags), cl_rate.PACER.calls,
             (" (stopped: %s%s)" % (stopped, "; " + defer if defer else "")) if stopped else ""))

    safeio.step_summary(
        "## Georgia Appellate Watch \u00b7 treatment sweep\n\n"
        "Classified %d citing opinion(s); raised %d flag(s) to caution.\n\n"
        "CourtListener REST calls: %d%s"
        % (classified, len(new_flags), cl_rate.PACER.calls,
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
