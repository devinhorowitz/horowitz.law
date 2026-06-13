#!/usr/bin/env python3
"""
Backfill the Georgia Appellate Watch archive (scripts/backfill.py).

SEED MODE (this file): given an explicit list of CourtListener cluster IDs --
the decisions harvested from the QPWB skill library, i.e. the cases the practice
actually relies on -- run each through the SAME three-tier funnel as the daily
pipeline and write a card for each into opinions.json.

Because the seed is pre-vetted (every case was pulled from a skill), the Tier-1
screen and Tier-2 triage are run as a RECALL TEST: their verdicts are recorded,
not used to gate. A known-relevant case the screen would have dropped is a false
negative, and seeing those before trusting the screen on a multi-year open sweep
is the point. The Tier-3 summarizer writes the card.

Reuses update.py (identical prompts and models) and render.py, so the archive
matches the daily output exactly. first_seen is stamped with the FILING DATE,
not the run date, so backfilled cards are never treated as "new this week" by
the email digest.

Text comes from the PDF enclosure on storage.courtlistener.com (Phase 2, no REST
quota), falling back to the REST API only when extraction fails. Per case the
only REST calls are the cluster/opinion/docket metadata lookups (about three).

Env:
  ANTHROPIC_API_KEY     required (same secret as the daily pipeline)
  COURTLISTENER_TOKEN   recommended (cluster/opinion/docket metadata lookups)
  DRY_RUN=1             print the recall report and drafted cards; write nothing
  OPINIONS_SEED         optional override of the seed list, comma-separated
                        "cluster:court_id" pairs; court_id in {ga,gactapp,ca11,scotus}
  (all OPINIONS_* funnel knobs are inherited from update.py)

Run via .github/workflows/backfill.yml (workflow_dispatch).
"""
import os, re, sys, json, time
import update
import render
import cl_rate           # shared CourtListener REST budget (limits, pacing, defer)
import safeio            # crash-safe atomic writes

STORAGE = "https://storage.courtlistener.com/"
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
PR_PATH = os.path.join(update.REPO, "scripts", "backfill_pr_body.md")
# Cap how long one cluster's metadata lookups may take. cl_get honors a 429
# Retry-After header literally, and CourtListener sets it to the full daily-reset
# window (often hours); passing a deadline makes cl_get raise the 429 fast instead
# of sleeping on it. Normal short retries for transient 5xx still happen.
CL_DEADLINE_SEC = int(os.environ.get("OPINIONS_CL_DEADLINE_SEC", "30"))

# Sweep mode (the 12-month windowed backfill): set BACKFILL_SWEEP=1 to discover via a
# date-windowed published search per court and run the SAME gating funnel as the daily
# pipeline (screen -> triage -> summarize), instead of the pre-vetted skill seed below.
# Window and courts come from BACKFILL_FROM / BACKFILL_TO / BACKFILL_COURTS.
SWEEP = os.environ.get("BACKFILL_SWEEP", "") in ("1", "true", "True", "yes")

# The post-2014, four-court decisions harvested from the QPWB skills, resolved to
# CourtListener clusters. (cluster_id, court_id). Comments name the case and the
# skill it came out of; court_id is re-derived from the docket at run time.
SEED = [
    # Retry set: the four seed cases that did NOT card in the first backfill run (PR #3).
    # Ordered with Aspen first: it was the only one lost to a retrieval error (HTTP 429 on
    # the last case last time), so it leads to guarantee it is fetched before any budget
    # ceiling. The other three were funnel declines on the merits and are expected to drop
    # again; they are re-tested here for the record, not forced in.
    (9391147,  "ca11"),     # Aspen American Ins. v. Landstar Ranger (2023) -- broker FAAAA; expected to card (first run errored on HTTP 429 before scoring)
    (4426951,  "ca11"),     # Slater v. U.S. Steel (2017) -- judicial estoppel / bankruptcy; first run declined (screen+triage+summarize, low)
    (4433323,  "gactapp"),  # Anderson v. Laureano (2017) -- jurisdictional dismissal, no merits; first run declined (correct)
    (4446479,  "gactapp"),  # H&E Innovation v. Shinhan Bank America (2017) -- first run declined (low / out of lane)
]


def seed_list():
    raw = os.environ.get("OPINIONS_SEED", "").strip()
    if not raw:
        return list(SEED)
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        cid, _, court = tok.partition(":")
        out.append((int(cid), (court or "ga").strip()))
    return out


def seed_result(cid, court_id):
    """Assemble a funnel-input dict for a cluster, shaped like a CourtListener
    search result so update.py's helpers consume it unchanged. The PDF text is
    fetched later from storage and costs no REST quota; here we spend at most the
    cluster, lead-opinion, and docket metadata lookups."""
    dl = time.time() + CL_DEADLINE_SEC
    cl = update.cl_get("/api/rest/v4/clusters/%d/" % cid, deadline=dl)
    name = (cl.get("case_name") or cl.get("case_name_full") or "").strip()
    date_filed = (cl.get("date_filed") or "")[:10]

    oids = []
    for s in (cl.get("sub_opinions") or []):
        if isinstance(s, int):
            oids.append(s)
        else:
            m = re.search(r"/opinions/(\d+)/", s) if isinstance(s, str) else None
            if m:
                oids.append(int(m.group(1)))
    oid = oids[0] if oids else None     # lead (first) sub-opinion drives the PDF url

    local_path, download_url = "", ""
    if oid:
        op = update.cl_get("/api/rest/v4/opinions/%d/" % oid, deadline=dl)
        local_path = op.get("local_path") or ""
        download_url = op.get("download_url") or ""
    pdf_url = (STORAGE + local_path) if local_path else (
        download_url if download_url.lower().startswith("http") else "")

    # Docket lookup gives the docket number and the authoritative court id.
    docket_num = ""
    durl = cl.get("docket")
    if durl:
        try:
            d = update.cl_get(durl, deadline=dl)
            docket_num = (d.get("docket_number") or "").strip()
            court_url = d.get("court") or ""
            m = re.search(r"/courts/([^/]+)/", court_url) if isinstance(court_url, str) else None
            derived = m.group(1) if m else (d.get("court_id") or "")
            if derived in update.COURT_MAP:
                court_id = derived
        except Exception as e:
            update._dbg("docket lookup failed for %d: %s" % (cid, e))

    return {
        "cluster_id": cid,
        "caseName": name,
        "court_id": court_id,
        "docketNumber": docket_num,
        "dateFiled": date_filed,
        "absolute_url": cl.get("absolute_url") or ("/opinion/%d/" % cid),
        "pdf_url": pdf_url,
        "precedential_status": (cl.get("precedential_status") or ""),
        "opinions": [{"id": i} for i in oids],
    }


def _flag(label):
    return {True: "pass", False: "DROP", None: "err"}.get(label, str(label))


def render_recall(rows, new_cards):
    scored = [r for r in rows if r.get("status") in ("card", "no-card")]
    passed = [r for r in scored if r.get("screen_pass") is True]
    dropped = [r for r in scored if r.get("screen_pass") is False]
    errd = [r for r in scored if r.get("screen_pass") is None]
    run_n = len([r for r in rows if r.get("status") != "skip-exists"])

    L = ["## Backfill seed run: recall test + drafted cards", ""]
    L.append("Seed of %d known-relevant decisions from the QPWB skills, run through the live "
             "three-tier funnel (screen / triage / summarize). Screen and triage are recorded, "
             "not gating; the summarizer drafts each card." % run_n)
    L.append("")
    L.append("**Recall (Tier-1 screen on known-relevant cases): passed %d, dropped %d, error %d of %d scored.**"
             % (len(passed), len(dropped), len(errd), len(scored)))
    if dropped:
        L.append("")
        L.append("**Screen DROPPED these known-relevant cases (false-negative candidates -- examine before the open sweep):**")
        for r in dropped:
            L.append("- %s (%s): %s" % (r["name"], r.get("date", ""), r.get("screen_reason", "")))
    elif scored:
        L.append("")
        L.append("No false negatives: the screen passed every known-relevant case scored. Recall is clean.")
    L.append("")
    L.append("**Cards drafted: %d. No-card: %d. Errors: %d.**"
             % (len(new_cards),
                len([r for r in rows if r.get("status") == "no-card"]),
                len([r for r in rows if r.get("status") == "error"])))
    L.append("")
    L.append("| case | date | court | screen | triage (sig) | summ (sig) | areas | status |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("status") == "skip-exists":
            L.append("| %s | | | | | | | already in archive |" % r.get("name", r["cid"]))
            continue
        if r.get("status") == "error":
            L.append("| %s | | | | | | | error: %s |" % (r.get("name", ""), r.get("detail", "")))
            continue
        L.append("| %s | %s | %s | %s | %s/%s | %s/%s | %s | %s |" % (
            r["name"], r.get("date", ""), r.get("court") or "?",
            _flag(r.get("screen_pass")),
            _flag(r.get("triage_relevant")), r.get("triage_sig") or "-",
            _flag(r.get("summ_relevant")), r.get("summ_sig") or "-",
            ",".join(r.get("areas", [])) or "-",
            r.get("status", "")))
    probs = [(r["name"], r["problems"]) for r in rows if r.get("problems")]
    if probs:
        L.append("")
        L.append("**Review flags:**")
        for n, p in probs:
            L.append("- %s: %s" % (n, "; ".join(p)))
    return "\n".join(L) + "\n"


def _write_pr_body(report):
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write(report)


def run():
    if not update.KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)
    if not update.CL_TOKEN:
        print("  ! warning: COURTLISTENER_TOKEN not set; metadata lookups may be rate-limited or denied.")

    # Guarantee the PR-body file exists on every exit path (dry run, 429 abort,
    # no cards), so the workflow's PR step never fails on a missing file. The
    # daily pipeline does the same. It is overwritten with the real report below.
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No backfill this run.\n")

    seed = seed_list()
    entries = json.load(open(update.JSON_PATH, encoding="utf-8")) if os.path.exists(update.JSON_PATH) else []
    have = {int(e["cluster_id"]) for e in entries if e.get("cluster_id")}
    print("seed: %d cluster(s) | archive has %d card(s) | screen=%s triage=%s summarize=%s"
          % (len(seed), len(have), update.SCREEN_MODEL or "off", update.TRIAGE_MODEL or "off", update.MODEL))

    rows, new_cards, aborted = [], [], False
    for cid, court_id in seed:
        if cid in have:
            print("  - %d already in archive; skipping" % cid)
            rows.append({"cid": cid, "name": str(cid), "status": "skip-exists"})
            continue
        if cl_rate.remaining() <= 0:
            note = cl_rate.PACER.defer_note()
            print("\nABORT: CourtListener hourly budget reached%s. "
                  "Re-dispatch after the budget resets to continue the seed."
                  % ((" -- " + note) if note else ""))
            aborted = True
            break
        try:
            r = seed_result(cid, court_id)
        except cl_rate.RateBudgetExceeded:
            note = cl_rate.PACER.defer_note()
            print("\nABORT: CourtListener throttled%s. "
                  "Stopping now instead of sleeping on the reset window. "
                  "Re-dispatch after the budget resets." % ((" -- " + note) if note else ""))
            rows.append({"cid": cid, "name": "(cluster %d)" % cid, "status": "error",
                         "detail": "CourtListener throttled: %s" % (note or "budget exhausted")})
            aborted = True
            break
        except Exception as e:
            if getattr(e, "code", None) == 429:
                note = cl_rate.PACER.defer_note()
                print("\nABORT: CourtListener returned HTTP 429%s. "
                      "Stopping now instead of sleeping on the Retry-After window. "
                      "Re-dispatch after the CourtListener budget resets." % ((" -- " + note) if note else ""))
                rows.append({"cid": cid, "name": "(cluster %d)" % cid, "status": "error",
                             "detail": "HTTP 429 (%s)" % (note or "CourtListener budget exhausted")})
                aborted = True
                break
            print("  ! metadata fetch failed for %d: %s" % (cid, e))
            rows.append({"cid": cid, "name": "(cluster %d)" % cid, "status": "error", "detail": str(e)[:160]})
            continue

        name = r["caseName"]; docket = r["docketNumber"]; date_filed = r["dateFiled"]; court_id = r["court_id"]
        url = "https://www.courtlistener.com" + r["absolute_url"]

        tdl = time.time() + CL_DEADLINE_SEC
        text = update.pdf_text(r.get("pdf_url"), deadline=tdl)
        src = "pdf"
        if not update._pdf_ok(text):
            try:
                rest = update.opinion_text_full(r, deadline=tdl)
            except cl_rate.RateBudgetExceeded:
                note = cl_rate.PACER.defer_note()
                print("\nABORT: CourtListener throttled during text fetch%s. "
                      "Re-dispatch after the budget resets." % ((" -- " + note) if note else ""))
                rows.append({"cid": cid, "name": name, "status": "error",
                             "detail": "CourtListener throttled: %s" % (note or "budget exhausted")})
                aborted = True
                break
            if rest:
                text, src = rest, "rest"
        if not text:
            print("  ! no opinion text for %s (%d)" % (name, cid))
            rows.append({"cid": cid, "name": name, "status": "error", "detail": "no opinion text"})
            continue
        snippet = text[:1500]

        # Tier 1 -- screen (RECALL TEST: recorded, not gating)
        try:
            s = update.screen(name, docket, snippet)
        except Exception as e:
            s = {"pass": None, "reason": "screen error: %s" % str(e)[:120]}
        time.sleep(0.4)

        # Tier 2 -- triage (recorded, not gating)
        try:
            t = update.triage(name, docket, text)
        except Exception as e:
            t = {"relevant": None, "significance": "", "reason": "triage error: %s" % str(e)[:120], "note": ""}
        note = t.get("note") or ""
        time.sleep(0.4)

        # Tier 3 -- summarize (drafts the card)
        try:
            v = update.summarize(court_id, name, docket, date_filed, text, note,
                                 cl_status=r.get("precedential_status", ""))
        except Exception as e:
            print("  ! summarize failed for %s (%d): %s" % (name, cid, e))
            rows.append({"cid": cid, "name": name, "status": "error", "detail": "summarize: %s" % str(e)[:140]})
            continue

        areas = [a for a in (v.get("areas") or []) if a in update.VALID_AREAS]
        court = update.COURT_MAP.get(court_id) or (
            v.get("court") if v.get("court") in update.VALID_KEYS else None)
        dockets = [str(d).strip() for d in (v.get("dockets") or []) if str(d).strip()] or ([docket] if docket else [])
        disp = (v.get("disposition") or "").strip().lower()
        synopsis = (v.get("synopsis") or "").strip()
        why = (v.get("why") or "").strip()

        problems = []
        if not areas: problems.append("no valid practice area")
        if not court: problems.append("unrecognized court id %s" % court_id)
        if not dockets: problems.append("no docket number")
        if not (synopsis and why): problems.append("empty synopsis or why")
        if update.CITE_RE.search(synopsis) or update.CITE_RE.search(why):
            problems.append("reporter-style citation in summary")

        row = {"cid": cid, "name": (v.get("name") or name).strip(), "date": date_filed, "court": court,
               "src": src, "screen_pass": s.get("pass"), "screen_reason": s.get("reason", ""),
               "triage_relevant": t.get("relevant"), "triage_sig": (t.get("significance") or ""),
               "summ_relevant": v.get("relevant"), "summ_sig": (v.get("significance") or ""),
               "areas": areas, "problems": problems}

        # SEED is pre-vetted, so relevant/significance are NOT gated; a card is
        # built whenever the renderer's required fields are present.
        if not (areas and court and synopsis and why):
            row["status"] = "no-card"
            rows.append(row)
            print("  x %s: no card (%s)" % (name, "; ".join(problems) or "missing fields"))
            continue

        # Phase-4 parity (HANDOFF item 7): build the card through update.assemble_entry
        # so seeded cards carry the same taxonomy as the daily feed (first_impression,
        # tort_reform, law_applied, additional_holdings). first_seen is the filing date,
        # so the digest never treats a backfilled card as new this week.
        card = update.assemble_entry(v, cid, name, court, areas, docket, date_filed, url, date_filed)
        new_cards.append(card)
        row["status"] = "card"
        rows.append(row)
        print("  + %s [%s] screen=%s triage=%s/%s areas=%s%s"
              % (name, date_filed, _flag(s.get("pass")), _flag(t.get("relevant")),
                 (t.get("significance") or "-"), ",".join(areas),
                 ("  REVIEW: " + "; ".join(problems)) if problems else ""))

    report = render_recall(rows, new_cards)
    if aborted:
        note = cl_rate.PACER.defer_note()
        report = ("> Run aborted early: CourtListener budget reached%s, so not all seed cases were "
                  "processed. Re-run after it resets.\n\n" % ((" (" + note + ")") if note else "")) + report
    print("\n" + report)
    print("CourtListener REST calls: %d" % cl_rate.PACER.calls)

    if DRY_RUN:
        print("DRY_RUN: nothing written. %d card(s) would be added.\n" % len(new_cards))
        print(json.dumps(new_cards, indent=2, ensure_ascii=False))
        return

    if not new_cards:
        _write_pr_body(report)
        print("No new cards; nothing written.")
        return

    merged = entries + new_cards
    safeio.atomic_write_json(update.JSON_PATH, merged)
    n, _total = render.render(merged)   # render returns (recent_shown, total)
    _write_pr_body(report)
    print("wrote %d new card(s); opinions.json now %d; rendered %d into opinions.html and opinions.xml."
          % (len(new_cards), len(merged), n))


def _norm_https(u):
    """Normalize an opinion-PDF URL to https; empty string if it is not http(s)."""
    u = (u or "").strip()
    if u.lower().startswith("http://"):
        u = "https://" + u[len("http://"):]
    return u if u.lower().startswith("https://") else ""


def _norm_docket(d):
    """Trim and drop a leading 'No. ' so the same docket compares equal across
    clusters regardless of how CourtListener formatted the docketNumber."""
    d = (d or "").strip()
    if d[:4].lower() == "no. ":
        d = d[4:].strip()
    return d


def _ident_keys(card):
    """Case-identity keys for cross-cluster dedup. CourtListener files one opinion
    under more than one cluster (an older ingestion and a newer one sharing the
    docket and filing date), so the cluster_id is not a reliable case identity.
    Key on (court, docket) for each docket on the card; fall back to (court, name,
    date) only when the card carries no usable docket. Same court is required, so
    a docket number reused at a different court never collides."""
    court = (card.get("court") or "").strip()
    keys = set()
    for d in (card.get("dockets") or []):
        nd = _norm_docket(d)
        if nd:
            keys.add(("d", court, nd))
    if not keys:
        nm = (card.get("name") or "").strip().lower()
        dt = (card.get("date") or "").strip()
        if nm and dt:
            keys.add(("nd", court, nm, dt))
    return keys


def _holding_count(card):
    """Primary holding plus any structured additional holdings. Used to keep the
    richer of two clusters when the funnel structured a secondary holding for only
    one of them, so dedup never silently drops a holding."""
    return 1 + len(card.get("additional_holdings") or [])


def render_sweep_report(after, before, courts, rows, new_cards,
                        n_screen, n_triage, n_opus, aborted, abort_reason):
    by = lambda s: [r for r in rows if r.get("status") == s]
    screen_drop, triage_drop = by("screen-drop"), by("triage-drop")
    summ_drop, errors, skips = by("summ-drop"), by("error"), by("skip-exists")
    dups = by("skip-dup")
    seen = len([r for r in rows if r.get("status") != "skip-exists"])

    L = ["## Georgia Appellate Watch: backfill sweep", ""]
    L.append("Window %s to %s; courts: %s." % (after, before, ", ".join(courts)))
    L.append("")
    if aborted:
        L.append("> Stopped early: %s. Re-dispatch the same window to resume; clusters already "
                 "carded are skipped, so a re-run only re-screens the drops." % (abort_reason or "budget reached"))
        L.append("")
    L.append("Discovered %d new published candidate(s) (clusters already in the archive excluded). "
             "Gating funnel, identical to the daily pipeline:" % seen)
    L.append("- screen: %d run, %d dropped" % (n_screen, len(screen_drop)))
    L.append("- triage: %d run, %d dropped" % (n_triage, len(triage_drop)))
    L.append("- summarize: %d run, %d dropped" % (n_opus, len(summ_drop)))
    L.append("- no opinion text / fetch error: %d" % len(errors))
    L.append("- already in archive: %d" % len(skips))
    if dups:
        L.append("- duplicate cluster, same case under a second cluster id (deduplicated): %d" % len(dups))
    L.append("")
    L.append("**Cards added: %d.**" % len(new_cards))

    cardrows = [r for r in rows if r.get("status") == "card"]
    if cardrows:
        L.append("")
        L.append("| case | date | court | disposition | areas | review |")
        L.append("|---|---|---|---|---|---|")
        for r in cardrows:
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                r.get("name", ""), r.get("date", ""), r.get("court") or "?",
                r.get("disp") or "(none)", ",".join(r.get("areas", [])) or "-",
                "; ".join(r.get("problems", [])) or ""))

    # Screen/triage drops are the recall surface for a backfill: list a capped sample
    # so a known-relevant case the funnel wrongly dropped is visible on review.
    drops = screen_drop + triage_drop
    if drops:
        CAP = 50
        L.append("")
        L.append("<details><summary>Dropped at screen/triage: %d (spot-check for recall)</summary>" % len(drops))
        L.append("")
        for r in drops[:CAP]:
            L.append("- [%s] %s (%s): %s" % (r.get("status"), r.get("name", ""),
                                             r.get("date", ""), (r.get("reason", "") or "")[:140]))
        if len(drops) > CAP:
            L.append("- ...and %d more." % (len(drops) - CAP))
        L.append("")
        L.append("</details>")
    if errors:
        L.append("")
        L.append("**Fetch errors (no card; transient ones retry on re-dispatch):**")
        for r in errors:
            L.append("- %s: %s" % (r.get("name", ""), r.get("detail", "")))
    return "\n".join(L) + "\n"


def run_sweep():
    """The 12-month windowed backfill. Discovers PUBLISHED opinions per court via a
    date-windowed CourtListener search (update.search_window), then runs each through
    the SAME gating funnel as the daily pipeline (screen -> triage -> summarize) and
    writes a card for each survivor. This is the gating counterpart to run() above: run()
    treats a pre-vetted skill seed as a recall test and does not gate, whereas a windowed
    sweep is unvetted, so screen and triage gate exactly as they do in update.main().
    Cards are assembled through update.assemble_entry (Phase-4 parity) and stamped with
    the filing date, so the digest never treats a backfilled card as new this week.

    Resumable by chunking, not by a state file: the run is bounded by a wall-clock budget
    and aborts cleanly on the CourtListener budget or a 429; clusters already in
    opinions.json are skipped, so re-dispatching the same (or an adjacent) window resumes
    without re-carding. Drive it a window at a time via the workflow inputs."""
    if not update.KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)
    if not update.CL_TOKEN:
        print("  ! warning: COURTLISTENER_TOKEN not set; the windowed search may be rate-limited or denied.")

    # Guarantee the PR-body file exists on every exit path, like run() and the daily pipeline.
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No backfill this run.\n")

    after = os.environ.get("BACKFILL_FROM", "2024-06-12").strip()
    before = os.environ.get("BACKFILL_TO", "2025-05-31").strip()
    # Validate the window: a mistyped or reversed date otherwise sweeps nothing silently,
    # since CourtListener just returns an empty result for a bad range. Fail loud instead.
    for _lbl, _v in (("BACKFILL_FROM", after), ("BACKFILL_TO", before)):
        try:
            time.strptime(_v, "%Y-%m-%d")
        except ValueError:
            print("ERROR: %s must be a real date as YYYY-MM-DD; got %r." % (_lbl, _v)); sys.exit(1)
    if after > before:
        print("ERROR: window is reversed: BACKFILL_FROM (%s) is after BACKFILL_TO (%s)." % (after, before)); sys.exit(1)
    _today = time.strftime("%Y-%m-%d")
    if before > _today:
        print("ERROR: BACKFILL_TO (%s) is in the future; nothing is filed past today (%s)." % (before, _today)); sys.exit(1)
    courts_env = os.environ.get("BACKFILL_COURTS", "").strip()
    # CourtListener's court filter is case sensitive (court=GA returns nothing, court=ga
    # returns the docket), so normalize to lowercase and validate against the known ids.
    # A stray capital or typo would otherwise silently yield an empty sweep. Blank = all.
    requested = [c.strip().lower() for c in courts_env.split(",") if c.strip()]
    if requested:
        unknown = [c for c in requested if c not in update.COURTS]
        if unknown:
            print("  ! ignoring unknown court id(s): %s (valid: %s)"
                  % (", ".join(unknown), ", ".join(update.COURTS)))
        courts = [c for c in requested if c in update.COURTS]
        if not courts:
            print("ERROR: no valid court ids in BACKFILL_COURTS=%r; valid ids are %s."
                  % (courts_env, ", ".join(update.COURTS))); sys.exit(1)
    else:
        courts = list(update.COURTS)
    budget = int(os.environ.get("BACKFILL_BUDGET_SEC", "3000"))
    breaker = int(os.environ.get("BACKFILL_BREAKER", "4"))

    entries = json.load(open(update.JSON_PATH, encoding="utf-8")) if os.path.exists(update.JSON_PATH) else []
    have = {int(e["cluster_id"]) for e in entries if e.get("cluster_id")}
    # Cross-cluster dedup over what is already carded: (court, docket) keys, name+date
    # fallback, so a case already in opinions.json under one cluster is not re-carded
    # when the sweep finds it under a different cluster id.
    seen_existing = set()
    for _e in entries:
        seen_existing |= _ident_keys(_e)
    print("sweep: window %s..%s | courts %s | archive has %d card(s) | screen=%s triage=%s summarize=%s"
          % (after, before, ",".join(courts), len(have),
             update.SCREEN_MODEL or "off", update.TRIAGE_MODEL or "off", update.MODEL))

    run_start = time.time()
    rows, new_cards = [], []
    run_index = {}   # cross-cluster dedup within this run: ident-key -> index into new_cards
    n_screen = n_triage = n_opus = 0
    aborted, abort_reason, consec = False, "", 0

    for court in courts:
        if aborted:
            break
        # Discovery: one windowed search per court (cursor-paginated, ~20/page). The
        # only REST cost is the search pages; the PDF text below is free. Bounded by
        # the wall-clock budget so a long court cannot starve the rest of the run.
        try:
            cands = update.search_window(court, after, before, deadline=run_start + budget)
        except cl_rate.RateBudgetExceeded:
            note = cl_rate.PACER.defer_note()
            abort_reason = "CourtListener throttled during %s discovery%s" % (court, (" -- " + note) if note else "")
            print("\nABORT: %s. Re-dispatch after the budget resets." % abort_reason)
            aborted = True; break
        except Exception as e:
            if getattr(e, "code", None) == 429:
                note = cl_rate.PACER.defer_note()
                abort_reason = "CourtListener 429 during %s discovery%s" % (court, (" -- " + note) if note else "")
                print("\nABORT: %s." % abort_reason); aborted = True; break
            print("  ! search failed for %s: %s" % (court, e))
            rows.append({"cid": 0, "name": "(%s search)" % court, "status": "error", "detail": str(e)[:160]})
            continue
        print("  %s: %d candidate(s) in window" % (court, len(cands)))

        for r in cands:
            if time.time() - run_start > budget:
                abort_reason = "wall-clock budget reached (%ds)" % budget
                print("\n  ! %s; finalizing with %d card(s) collected" % (abort_reason, len(new_cards)))
                aborted = True; break

            cid = r["cluster_id"]
            name = r["caseName"]; docket = r["docketNumber"]; date_filed = r["dateFiled"]
            court_id = r["court_id"]; url = "https://www.courtlistener.com" + r["absolute_url"]
            if cid in have:
                rows.append({"cid": cid, "name": name, "status": "skip-exists"}); continue

            try:
                # Tier 1 -- screen (GATE). Uses the search snippet (free), so candidates
                # are screened BEFORE the opinion is fetched, the same order as the daily
                # pipeline: the ~90% the screen drops never trigger a download.
                if update.SCREEN_MODEL:
                    n_screen += 1
                    s = update.screen(name, docket, r.get("snippet") or "")
                    if not s.get("pass"):
                        rows.append({"cid": cid, "name": name, "date": date_filed, "court_id": court_id,
                                     "status": "screen-drop", "reason": s.get("reason", "")})
                        consec = 0; continue
                    time.sleep(0.4)

                # Full opinion text, fetched only for screen survivors: PDF enclosure first
                # (free CourtListener storage), REST fallback only if the PDF is unusable,
                # gated by the shared budget, with the same _pdf_ok check on both so junk
                # text can never silently reach triage (the recall-hole fix).
                tdl = run_start + budget
                text = update.pdf_text(r.get("pdf_url"), deadline=tdl)
                if not update._pdf_ok(text):
                    text = ""
                    if cl_rate.remaining() > 0:
                        rest = update.opinion_text_full(r, deadline=tdl)
                        if update._pdf_ok(rest):
                            text = rest
                if not text:
                    rows.append({"cid": cid, "name": name, "status": "error", "detail": "no opinion text"})
                    consec = 0; continue

                # Tier 2 -- triage (GATE). Reads the full opinion text.
                note = ""
                if update.TRIAGE_MODEL:
                    n_triage += 1
                    t = update.triage(name, docket, text)
                    if not t.get("relevant") or (t.get("significance") or "").lower() == "low":
                        rows.append({"cid": cid, "name": name, "date": date_filed, "court_id": court_id,
                                     "status": "triage-drop", "reason": t.get("reason", ""),
                                     "triage_sig": (t.get("significance") or "")})
                        consec = 0; continue
                    note = t.get("note") or ""
                    time.sleep(0.4)
                # Tier 3 -- summarize (gated on relevance / significance / area / court below)
                n_opus += 1
                v = update.summarize(court_id, name, docket, date_filed, text, note,
                                     cl_status=r.get("precedential_status", ""))
                consec = 0
            except cl_rate.RateBudgetExceeded:
                note = cl_rate.PACER.defer_note()
                abort_reason = "CourtListener throttled during %s text fetch%s" % (court, (" -- " + note) if note else "")
                print("\nABORT: %s. Re-dispatch after the budget resets." % abort_reason)
                aborted = True; break
            except update.ConfigError as e:
                abort_reason = "configuration error: %s" % e
                print("  ! %s (nothing committed)" % abort_reason); aborted = True; break
            except Exception as e:
                print("  ! error on cluster %s (%s): %s" % (cid, name, e))
                rows.append({"cid": cid, "name": name, "status": "error", "detail": str(e)[:160]})
                consec += 1
                if consec >= breaker:
                    abort_reason = "%d consecutive failures (API likely throttled)" % consec
                    print("  ! %s; stopping early" % abort_reason); aborted = True; break
                continue

            # Summarizer gates, matching update.main().
            reason = ""
            if not v.get("relevant"):
                reason = "summarizer: not relevant"
            elif (v.get("significance") or "").lower() == "low":
                reason = "summarizer: low significance"
            areas = [a for a in (v.get("areas") or []) if a in update.VALID_AREAS]
            court = update.COURT_MAP.get(court_id) or (v.get("court") if v.get("court") in update.VALID_KEYS else None)
            if not reason and not areas:
                reason = "no recognized practice area"
            if not reason and not court:
                reason = "unrecognized court id %s" % court_id
            if reason:
                rows.append({"cid": cid, "name": (v.get("name") or name), "date": date_filed,
                             "court_id": court_id, "status": "summ-drop", "reason": reason})
                continue

            entry = update.assemble_entry(v, cid, name, court, areas, docket, date_filed, url, date_filed)
            # Official-link enrichment, free: the search result already carried the court's
            # own PDF download_url, so no extra REST. Set it for the federal courts (the
            # daily pipeline's ca11/scotus source); scotga's official link comes from a
            # different source and is left to the daily path, fail-open.
            if entry["court"] in ("ca11", "scotus"):
                ou = _norm_https(r.get("download_url"))
                if ou:
                    entry["official_url"] = ou

            problems = []
            if update.CITE_RE.search(entry["synopsis"]) or update.CITE_RE.search(entry["why"]):
                problems.append("reporter-style citation in summary")
            if not entry["disposition"]:
                problems.append("no disposition")
            if (v.get("confidence") or "").lower() == "low":
                problems.append("low confidence")

            # --- Cross-cluster dedup -------------------------------------------------
            # CourtListener files one opinion under more than one cluster (an older
            # ingestion and a newer one sharing the docket and filing date), and the
            # windowed published search returns both. The cid check above only catches
            # the SAME cluster, so the same case under a second cluster cards twice
            # (seen on the SCOTUS sweep: Waetzig and Royal Canin each carded twice). Key
            # on (court, docket), name+date fallback. A case already in the archive is
            # skipped; two clusters of one case found in THIS run collapse to the one
            # with more holdings, so a secondary holding the funnel structured for only
            # one of the two is never dropped.
            keys = _ident_keys(entry)
            if keys & seen_existing:
                rows.append({"cid": cid, "name": entry["name"], "status": "skip-dup",
                             "detail": "same case already in the archive under another cluster"})
                print("  = %s: already carded under another cluster; skipped" % entry["name"])
                continue
            dup_idx = next((run_index[k] for k in keys if k in run_index), None)
            if dup_idx is not None:
                prior = new_cards[dup_idx]
                if _holding_count(entry) > _holding_count(prior):
                    new_cards[dup_idx] = entry
                    have.add(cid)
                    for k in keys:
                        run_index[k] = dup_idx
                    print("  ~ %s: duplicate cluster, kept the richer copy (%d holdings vs %d, dropped cluster %s)"
                          % (entry["name"], _holding_count(entry), _holding_count(prior), prior.get("cluster_id")))
                else:
                    print("  = %s: duplicate cluster, kept the earlier copy (cluster %s)"
                          % (entry["name"], prior.get("cluster_id")))
                rows.append({"cid": cid, "name": entry["name"], "status": "skip-dup",
                             "detail": "duplicate of another cluster discovered this run"})
                continue

            for k in keys:
                run_index[k] = len(new_cards)
            new_cards.append(entry); have.add(cid)
            rows.append({"cid": cid, "name": entry["name"], "date": date_filed, "court": entry["court"],
                         "disp": entry["disposition"], "areas": areas,
                         "sig": (v.get("significance") or ""), "status": "card", "problems": problems})
            print("  + %s [%s] %s areas=%s sig=%s%s"
                  % (entry["name"], date_filed, entry["disposition"] or "(none)", ",".join(areas),
                     v.get("significance"), ("  REVIEW: " + "; ".join(problems)) if problems else ""))

    report = render_sweep_report(after, before, courts, rows, new_cards,
                                 n_screen, n_triage, n_opus, aborted, abort_reason)
    print("\n" + report)
    print("CourtListener REST calls: %d" % cl_rate.PACER.calls)

    if DRY_RUN:
        print("DRY_RUN: nothing written. %d card(s) would be added.\n" % len(new_cards))
        print(json.dumps(new_cards, indent=2, ensure_ascii=False))
        return

    if not new_cards:
        _write_pr_body(report)
        print("No new cards; nothing written.")
        return

    merged = entries + new_cards
    safeio.atomic_write_json(update.JSON_PATH, merged)
    n, _total = render.render(merged)
    _write_pr_body(report)
    print("wrote %d new card(s); opinions.json now %d; rendered %d into opinions.html and opinions.xml."
          % (len(new_cards), len(merged), n))


if __name__ == "__main__":
    if SWEEP:
        run_sweep()
    else:
        run()