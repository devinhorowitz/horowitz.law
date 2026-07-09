#!/usr/bin/env python3
"""
Queue specific decisions into the Georgia Appellate Watch (scripts/queue_cases.py).

INBOX MODE: read queue.txt at the repo root -- a hand-curated list of cases the
editor found while working matters and wants screened into the feed -- and run
each through the SAME funnel as the daily pipeline, writing a card for the
keepers into opinions.json and opening the same editorial pull request.

This is the low-friction path to add a case without editing backfill.py: paste a
CourtListener opinion URL (or a bare cluster id) on its own line in queue.txt and
commit. The cluster id is read from the URL and the court is re-derived from the
docket at run time (the same resolver backfill.seed_result uses), so no id or
court lookup is needed. A trailing "!" on a line forces the case past the
relevance gate (still subject to the editor's PR review); anything after "#" is a
comment.

Unlike backfill (a one-time, pre-vetted skill seed run as a recall test), the
queue is ongoing and IS screened: each case runs the Tier-2 triage relevance gate
and the Tier-3 summarizer, and a case judged out of scope is reported in the PR
rather than carded -- unless forced. The triage adverse-treatment check runs on
every queued case, so a queued decision that negatively treats an existing card
raises that card to caution exactly as the daily forward escalation does. The
Tier-1 Haiku screen (a firehose pre-filter) is skipped here; the input is curated.

After a run, queue.txt is rewritten:
  - decided lines (carded, gated out, declined, already on the site) are removed
    and reported in the PR body;
  - lines that hit a transient CourtListener budget limit stay active to retry on
    the next push;
  - lines that could not be resolved are parked as comments annotated with the
    reason, for the editor to fix (e.g. add an explicit court) or delete.

Text comes from the PDF enclosure on storage.courtlistener.com first (no REST
quota), falling back to the REST API. Reuses update.py (identical prompts and
models), backfill.py (the cluster resolver), and render.py, so queued cards match
the daily output exactly. Queued cards are stamped first_seen = today, so they
surface in the next email digest as new additions to the feed.

(The file is named queue_cases.py, not queue.py, so it cannot shadow Python's
standard-library "queue" module, which pypdf imports during PDF extraction.)

Env:
  ANTHROPIC_API_KEY      required (same secret as the daily pipeline)
  COURTLISTENER_TOKEN    recommended (cluster/opinion/docket metadata lookups)
  DRY_RUN=1              print the report and drafted cards; write nothing, open no PR
  QUEUE_RESOLVE_ONLY=1   resolve each line (cluster/court/text) and print; call no model
                         and change nothing -- a cheap way to validate the queue first
  QUEUE_MAX              max active lines processed per run (default 25; extras roll over)
  QUEUE_BUDGET_SEC       soft wall-clock budget for one run (default 1500; extras roll over)
  OPINIONS_CL_DEADLINE_SEC  per-cluster metadata deadline (default 30; shared with backfill)
  (all OPINIONS_* funnel knobs and the ANTHROPIC_STATUS preflight are inherited from update.py)

Run via .github/workflows/queue.yml (push to queue.txt, or workflow_dispatch).
"""
import os, re, sys, json, time, datetime
from urllib.parse import urlparse
import update             # daily funnel: screen/triage/summarize, cl_get, text, constants
import backfill           # cluster resolver (seed_result): cluster -> court/text-url, audited
import render             # single source of truth renderer
import cl_rate            # shared CourtListener REST budget (limits, pacing, defer)
import safeio             # crash-safe atomic writes
import treatment_core     # adverse-treatment flagging (flag_caution, NEGATIVE_KINDS)

QUEUE_PATH = os.path.join(update.REPO, "queue.txt")
PR_PATH    = os.path.join(update.REPO, "scripts", "queue_cases_pr_body.md")

DRY_RUN      = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
RESOLVE_ONLY = os.environ.get("QUEUE_RESOLVE_ONLY", "") in ("1", "true", "True", "yes")
QUEUE_MAX    = int(os.environ.get("QUEUE_MAX", "25"))
BUDGET_SEC   = int(os.environ.get("QUEUE_BUDGET_SEC", "1500"))
CL_DEADLINE  = int(os.environ.get("OPINIONS_CL_DEADLINE_SEC", "30"))

OPINION_RE = re.compile(r"/opinion/(\d+)")
CLUSTER_RE = re.compile(r"^\d+$")
PAIR_RE    = re.compile(r"^(\d+):([A-Za-z0-9]+)$")


def parse_line(raw):
    """Classify one queue.txt line.

    Returns (kind, payload):
      ("blank",   line)  -> preserve as-is on rewrite
      ("comment", line)  -> preserve as-is on rewrite (full-line or inline-only comment)
      ("entry",   dict)  -> dict(cid, token, court, force, raw); cid is None if the
                            token is not a CourtListener URL, bare cluster id, or
                            cluster:court pair.
    """
    line = raw.rstrip("\n")
    stripped = line.strip()
    if not stripped:
        return ("blank", line)
    if stripped.startswith("#"):
        return ("comment", line)
    # Strip an inline comment. CourtListener URLs, cluster ids, and cluster:court
    # pairs never contain "#", so splitting on it is safe.
    body = stripped.split("#", 1)[0].strip()
    if not body:
        return ("comment", line)
    force = body.endswith("!")
    if force:
        body = body[:-1].strip()
    token, cid, court = body, None, None
    # classify by the parsed host, not a substring; normalize a missing scheme so a
    # bare paste (www.courtlistener.com/opinion/...) still resolves, and a look-alike
    # host (courtlistener.com.evil.tld) does not.
    u = token if "://" in token else "https://" + token
    host = (urlparse(u).hostname or "").lower()
    if host == "courtlistener.com" or host.endswith(".courtlistener.com"):
        m = OPINION_RE.search(token)
        if m:
            cid = int(m.group(1))
    elif CLUSTER_RE.match(token):
        cid = int(token)
    else:
        mp = PAIR_RE.match(token)
        if mp:
            cid, court = int(mp.group(1)), mp.group(2).lower()
    return ("entry", {"cid": cid, "token": token, "court": court, "force": force, "raw": line})


def render_report(rows, added, treat_flags, audit_notes, aborted_cfg):
    L = []
    if aborted_cfg:
        L += ["> Run stopped on a configuration error (bad/expired key, depleted credit, or a "
              "retired model id); nothing was committed and the queue was left intact. Fix and re-run.",
              ""]
    L += ["## Georgia Appellate Watch: queue run -- %d card(s) added" % len(added), ""]
    if added:
        for e in added:
            cl = render.COURT_LABELS[e["court"]]
            L.append("- **%s** (%s, %s): %s. areas: %s. Read: %s"
                     % (e["name"], cl, e["date"], e["disposition"] or "(none)",
                        ", ".join(e["areas"]), e["url"]))
    else:
        L.append("No new cards added this run.")

    if treat_flags or audit_notes:
        L += ["", "Treatment flags this run (existing cards; confirm on Shepard\u2019s before relying):"]
        for cardnm, newnm, kind in treat_flags:
            L.append("- **%s** -- possibly %s by the queued decision %s. Raised to caution; confirm, then "
                     "set `treatment` to negative or superseded, or back to ok." % (cardnm, kind, newnm))
        for cardnm, newnm, rev in audit_notes:
            if rev:
                L.append("- audit -- the **%s** card may need an edit in light of %s: %s" % (cardnm, newnm, rev))

    other = [r for r in rows if r[1] != "carded"]
    if other:
        L += ["", "Queue outcomes (not carded):"]
        for name, status, detail in other:
            L.append("- %s -- **%s**: %s" % (name, status, detail))

    L += ["", "_Lines that were carded, gated out, declined, or already on the site were removed from "
              "queue.txt. Lines deferred on the CourtListener budget were left active to retry; lines that "
              "could not be resolved were parked as comments with the reason._"]
    return "\n".join(L) + "\n"


def _write_pr(report):
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write(report)


def run():
    if not RESOLVE_ONLY and not update.KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)
    if not update.CL_TOKEN:
        print("  ! warning: COURTLISTENER_TOKEN not set; metadata lookups may be rate-limited or denied.")

    # The PR step reads PR_PATH as its body. Guarantee the file exists on every exit
    # path; it is gitignored and not in the PR add-paths, so a no-op run never fails on it.
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No queue run.\n")

    if not os.path.exists(QUEUE_PATH):
        print("queue.txt not found; nothing to do."); return
    raw_lines = open(QUEUE_PATH, encoding="utf-8").read().split("\n")
    if raw_lines and raw_lines[-1] == "":       # drop the trailing element from a final newline
        raw_lines = raw_lines[:-1]
    parsed = [parse_line(l) for l in raw_lines]
    active = [(i, p[1]) for i, p in enumerate(parsed) if p[0] == "entry"]
    if not active:
        print("queue.txt has no active entries; nothing to process."); return

    # Anthropic status preflight: on a confirmed outage, leave the queue intact and skip
    # cleanly so the next push retries. Fail-open (unknown/unreachable never blocks).
    if not RESOLVE_ONLY:
        slevel, sdesc = update.anthropic_status()
        print("Anthropic status: %s%s" % (sdesc, "" if slevel in ("operational", "unknown") else " [%s]" % slevel))
        if slevel == "outage" and update.STATUS_MODE == "on":
            print("  ! Anthropic API is in a reported outage; leaving the queue intact and skipping this run.")
            return

    entries = json.load(open(update.JSON_PATH, encoding="utf-8")) if os.path.exists(update.JSON_PATH) else []
    have  = {int(e["cluster_id"]) for e in entries if e.get("cluster_id")}
    by_id = {int(e["cluster_id"]): e for e in entries if e.get("cluster_id")}
    feed_index = "\n".join("%d: %s" % (int(e["cluster_id"]), e.get("name", ""))
                           for e in entries
                           if e.get("cluster_id") and (e.get("treatment") or "ok") != "superseded")
    print("queue: %d active line(s) | archive has %d card(s) | screen=off(curated) triage=%s summarize=%s"
          % (len(active), len(have), update.TRIAGE_MODEL or "off", update.MODEL))

    # first_seen for a queued card: today if the decision falls inside the public feed's
    # rolling window (so it appears on /opinions and the digest's #op- anchor resolves),
    # otherwise its filing date. Older cards render only in /archive, like backfilled ones,
    # so stamping them "today" would announce them as new this week with a link that misses.
    today_iso = datetime.date.today().isoformat()
    try:
        win_cutoff = datetime.date.today().replace(
            year=datetime.date.today().year - render.WINDOW_YEARS).isoformat()
    except ValueError:                          # Feb 29 -> Feb 28 in a non-leap target year
        win_cutoff = datetime.date.today().replace(
            year=datetime.date.today().year - render.WINDOW_YEARS, day=28).isoformat()

    # Per source-line outcome: line_outcome[idx] = (action, detail)
    #   "remove" -> drop the line (decided; reported in the PR body)
    #   "keep"   -> leave the line active (deferred; retried on the next run)
    #   "park"   -> rewrite the line as a "# ... -- reason" comment (unresolved; editor fixes it)
    line_outcome = {}
    added, treat_flags, audit_notes, report_rows = [], [], [], []
    treatment_changed = cfg_error = False
    run_start, processed = time.time(), 0

    for idx, ent in active:
        token, force = ent["token"], ent["force"]
        if processed >= QUEUE_MAX or (time.time() - run_start) > BUDGET_SEC:
            line_outcome[idx] = ("keep", None)
            report_rows.append((token, "deferred", "run cap reached; will process on the next run"))
            continue
        cid, court = ent["cid"], ent["court"]
        if cid is None:
            line_outcome[idx] = ("park", "not a CourtListener URL or cluster id; paste the opinion URL")
            report_rows.append((token, "unresolved", "not a CourtListener URL or cluster id"))
            continue
        if cid in have:
            line_outcome[idx] = ("remove", None)
            report_rows.append((token, "already on the site", "cluster %d is already carded" % cid))
            continue
        if cl_rate.remaining() <= 0:
            line_outcome[idx] = ("keep", None)
            report_rows.append((token, "deferred", "CourtListener budget reached; will retry next run"))
            continue
        processed += 1

        # ---- resolve cluster -> court / text source (audited resolver; court re-derived from docket) ----
        try:
            r = backfill.seed_result(cid, court)   # court=None -> derived; explicit court -> used as fallback
        except cl_rate.RateBudgetExceeded:
            line_outcome[idx] = ("keep", None)
            report_rows.append((token, "deferred", "CourtListener throttled; will retry next run"))
            continue
        except Exception as e:
            if getattr(e, "code", None) == 429:
                line_outcome[idx] = ("keep", None)
                report_rows.append((token, "deferred", "CourtListener 429; will retry next run"))
                continue
            line_outcome[idx] = ("park", "could not resolve cluster %d: %s" % (cid, str(e)[:100]))
            report_rows.append((token, "error", "resolve failed: %s" % str(e)[:120]))
            continue

        name = (r.get("caseName") or "").strip() or "(cluster %d)" % cid
        court_id = r.get("court_id")
        docket = r.get("docketNumber") or ""
        date_filed = (r.get("dateFiled") or "")[:10]
        url = "https://www.courtlistener.com" + (r.get("absolute_url") or ("/opinion/%d/" % cid))
        court = update.COURT_MAP.get(court_id)
        if not court:
            line_outcome[idx] = ("park",
                "could not determine a supported court for cluster %d; if it is Ga., Ga. App., 11th Cir., "
                "or SCOTUS, replace this line with \"%d:gactapp\" (or ga|ca11|scotus)" % (cid, cid))
            report_rows.append((name, "out of scope", "court %r not in {ga,gactapp,ca11,scotus}" % (court_id or "?")))
            continue

        # ---- opinion text: PDF enclosure first (no REST quota), REST fallback ----
        tdl = time.time() + CL_DEADLINE
        text = update.pdf_text(r.get("pdf_url"), deadline=tdl)
        if not update._pdf_ok(text):
            text = ""    # blank sub-quality PDF junk so it can't reach triage (see update.main)
            try:
                rest = update.opinion_text_full(r, deadline=tdl)
                if update._pdf_ok(rest):
                    text = rest
            except cl_rate.RateBudgetExceeded:
                line_outcome[idx] = ("keep", None)
                report_rows.append((name, "deferred", "CourtListener throttled during text fetch; retry next run"))
                continue
        if not text:
            line_outcome[idx] = ("park", "no opinion text retrieved for cluster %d; retry or check the cluster" % cid)
            report_rows.append((name, "error", "no opinion text"))
            continue

        if RESOLVE_ONLY:
            line_outcome[idx] = ("keep", None)   # resolve-only never alters the queue or calls a model
            report_rows.append((name, "resolved",
                "court=%s date=%s docket=%s text=%d chars force=%s" % (court, date_filed, docket, len(text), force)))
            continue

        # ---- funnel: triage (with adverse-treatment escalation), then summarize ----
        try:
            time.sleep(0.4)
            t = update.triage(name, docket, text, feed_index)
            # Forward escalation, identical to the daily run: if this opinion treats a
            # carded case negatively, confirm with an Opus audit and raise that card to
            # caution, whether or not this opinion itself earns a place in the feed.
            for tr in (t.get("treats") or []):
                try:
                    card = by_id.get(int(tr.get("id")))
                except (TypeError, ValueError):
                    card = None
                if not card or (card.get("treatment") or "ok") == "superseded":
                    continue
                try:
                    a = update.treatment_audit(name, text, card)
                except update.ConfigError:
                    raise
                except Exception as ae:
                    print("  ! treatment audit failed for card %s citing %s: %s"
                          % (card.get("cluster_id"), name, ae))
                    continue
                akind = (a.get("kind") or "").lower().strip() or None
                if (a.get("treatment") or "").lower() == "negative" and a.get("affects_proposition") \
                        and akind in treatment_core.NEGATIVE_KINDS:
                    citer = {"cluster_id": cid, "name": name, "court": court,
                             "date": date_filed, "kind": akind, "note": (a.get("note") or "").strip()}
                    treatment_core.flag_caution(card, citer)
                    treatment_changed = True
                    treat_flags.append((card.get("name", ""), name, akind))
                    print("  ~ adverse: %s treated by %s (%s)"
                          % (card.get("name", "")[:40], name[:40], akind))
                if a.get("card_review"):
                    audit_notes.append((card.get("name", ""), name, (a.get("card_review_note") or "").strip()))

            gated = (not t.get("relevant")) or (t.get("significance") or "").lower() == "low"
            if gated and not force:
                line_outcome[idx] = ("remove", None)
                report_rows.append((name, "gated out",
                    "triage: %s (add \"!\" to force)" % (t.get("reason") or "not relevant / low significance")))
                continue
            note = t.get("note") or ""
            time.sleep(0.4)
            v = update.summarize(court_id, name, docket, date_filed, text, note,
                                 cl_status=r.get("precedential_status", ""))
        except update.ConfigError as e:
            print("  ! configuration error, stopping this run so it surfaces (nothing committed): %s" % e)
            cfg_error = True
            break
        except Exception as e:
            line_outcome[idx] = ("keep", None)   # transient model/network error: retry next run
            report_rows.append((name, "error", "processing failed (will retry): %s" % str(e)[:120]))
            continue

        # The summarizer is the final editor and can still decline. A force flag overrides
        # the narrow-feed triage gate, but a summarizer "not relevant" almost always means
        # the wrong cluster was queued, so it is respected (reported, not carded) even forced.
        if not v.get("relevant"):
            line_outcome[idx] = ("remove", None)
            report_rows.append((name, "declined", "summarizer judged it not a relevant opinion (check the URL/cluster)"))
            continue
        if (v.get("significance") or "").lower() == "low" and not force:
            line_outcome[idx] = ("remove", None)
            report_rows.append((name, "gated out", "summarizer: low significance (add \"!\" to force)"))
            continue

        areas = [a for a in (v.get("areas") or []) if a in update.VALID_AREAS]
        dockets = [str(d).strip() for d in (v.get("dockets") or []) if str(d).strip()] or ([docket] if docket else [])
        disp = (v.get("disposition") or "").strip().lower()
        synopsis = (v.get("synopsis") or "").strip()
        why = (v.get("why") or "").strip()
        problems = []
        if not areas: problems.append("no valid practice area")
        if not dockets: problems.append("no docket number")
        if not (synopsis and why): problems.append("empty synopsis or why")
        if (v.get("confidence") or "").lower() == "low": problems.append("low confidence")
        if update.CITE_RE.search(synopsis) or update.CITE_RE.search(why):
            problems.append("reporter-style citation in summary")
        if not (areas and synopsis and why):
            line_outcome[idx] = ("remove", None)
            report_rows.append((name, "no card", "; ".join(problems) or "missing required fields"))
            continue

        card = {"cluster_id": cid, "name": (v.get("name") or name).strip(), "court": court,
                "division": (v.get("division") or None), "date": date_filed,
                "dockets": dockets or [""], "disposition": disp, "areas": areas, "url": url,
                "synopsis": synopsis, "why": why,
                "precedential": (v.get("precedential") or "unknown"),
                "first_seen": today_iso if date_filed >= win_cutoff else date_filed}
        added.append(card)
        have.add(cid)                              # dedupe within this run
        line_outcome[idx] = ("remove", None)
        report_rows.append((name, "carded",
            "%s, %s%s" % (", ".join(areas), disp or "(no disposition)",
                          ("  REVIEW: " + "; ".join(problems)) if problems else "")))
        print("  + %s [%s] %s areas=%s%s" % (card["name"], date_filed, disp or "(none)",
              ",".join(areas), ("  REVIEW: " + "; ".join(problems)) if problems else ""))

    report = render_report(report_rows, added, treat_flags, audit_notes, cfg_error)
    print("\n" + report)
    print("CourtListener REST calls: %d" % cl_rate.PACER.calls)

    if cfg_error:
        _write_pr(report)            # the failed step skips the PR step anyway; nothing is committed
        print("Stopped on a configuration error; queue and files left intact. Exiting non-zero.")
        sys.exit(1)

    if RESOLVE_ONLY:
        print("QUEUE_RESOLVE_ONLY: resolution only; queue and files unchanged.")
        return
    if DRY_RUN:
        _write_pr(report)
        print("DRY_RUN: nothing written. %d card(s) would be added." % len(added))
        if added:
            print(json.dumps(added, indent=2, ensure_ascii=False))
        return

    # Rewrite queue.txt from the per-line outcomes, preserving blank lines and comments.
    new_lines = []
    for i, (kind, payload) in enumerate(parsed):
        if kind in ("blank", "comment"):
            new_lines.append(payload); continue
        action, detail = line_outcome.get(i, ("keep", None))
        if action == "remove":
            continue
        if action == "park":
            new_lines.append("# %s   -- %s" % (payload.strip(), detail))
            continue
        new_lines.append(payload)                  # keep (deferred) or untouched
    new_text = ("\n".join(new_lines).rstrip("\n") + "\n") if new_lines else ""
    queue_changed = new_text != open(QUEUE_PATH, encoding="utf-8").read()

    _write_pr(report)

    if added or treatment_changed:
        if added:
            entries += added
        safeio.atomic_write_json(update.JSON_PATH, entries)
        n, _total = render.render(entries)   # render returns (recent_shown, total)
        print("rendered %d entries; added %d, treatment %d." % (n, len(added), len(treat_flags)))
    if queue_changed:
        safeio.atomic_write_text(QUEUE_PATH, new_text)
        print("queue.txt rewritten.")
    if not (added or treatment_changed or queue_changed):
        print("nothing to add; files unchanged.")


if __name__ == "__main__":
    run()
