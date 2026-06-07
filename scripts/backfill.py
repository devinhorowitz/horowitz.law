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

STORAGE = "https://storage.courtlistener.com/"
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
PR_PATH = os.path.join(update.REPO, "scripts", "backfill_pr_body.md")
# Cap how long one cluster's metadata lookups may take. cl_get honors a 429
# Retry-After header literally, and CourtListener sets it to the full daily-reset
# window (often hours); passing a deadline makes cl_get raise the 429 fast instead
# of sleeping on it. Normal short retries for transient 5xx still happen.
CL_DEADLINE_SEC = int(os.environ.get("OPINIONS_CL_DEADLINE_SEC", "30"))

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

    oid = None
    for s in (cl.get("sub_opinions") or []):
        if isinstance(s, int):
            oid = s
            break
        m = re.search(r"/opinions/(\d+)/", s) if isinstance(s, str) else None
        if m:
            oid = int(m.group(1))
            break

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
        "opinions": [{"id": oid}] if oid else [],
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
        try:
            r = seed_result(cid, court_id)
        except Exception as e:
            if getattr(e, "code", None) == 429:
                print("\nABORT: CourtListener returned HTTP 429 (daily 125-request budget exhausted). "
                      "Stopping now instead of sleeping on the Retry-After window. "
                      "Re-dispatch after the CourtListener budget resets.")
                rows.append({"cid": cid, "name": "(cluster %d)" % cid, "status": "error",
                             "detail": "HTTP 429 (CourtListener budget exhausted)"})
                aborted = True
                break
            print("  ! metadata fetch failed for %d: %s" % (cid, e))
            rows.append({"cid": cid, "name": "(cluster %d)" % cid, "status": "error", "detail": str(e)[:160]})
            continue

        name = r["caseName"]; docket = r["docketNumber"]; date_filed = r["dateFiled"]; court_id = r["court_id"]
        url = "https://www.courtlistener.com" + r["absolute_url"]

        text = update.pdf_text(r.get("pdf_url"))
        src = "pdf"
        if not update._pdf_ok(text):
            oid = update.opinion_id_of(r)
            rest = update.opinion_text(oid) if oid else ""
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
            v = update.summarize(court_id, name, docket, date_filed, text, note)
        except Exception as e:
            print("  ! summarize failed for %s (%d): %s" % (name, cid, e))
            rows.append({"cid": cid, "name": name, "status": "error", "detail": "summarize: %s" % str(e)[:140]})
            continue

        areas = [a for a in (v.get("areas") or []) if a in update.VALID_AREAS]
        court = update.COURT_MAP.get(court_id) or (
            v.get("court") if v.get("court") in ("scotga", "ctapp", "ca11", "scotus") else None)
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

        card = {"cluster_id": cid, "name": (v.get("name") or name).strip(), "court": court,
                "division": (v.get("division") or None), "date": date_filed,
                "dockets": dockets or [""], "disposition": disp, "areas": areas, "url": url,
                "synopsis": synopsis, "why": why,
                "first_seen": date_filed}  # filing date, so the digest does not treat it as new
        new_cards.append(card)
        row["status"] = "card"
        rows.append(row)
        print("  + %s [%s] screen=%s triage=%s/%s areas=%s%s"
              % (name, date_filed, _flag(s.get("pass")), _flag(t.get("relevant")),
                 (t.get("significance") or "-"), ",".join(areas),
                 ("  REVIEW: " + "; ".join(problems)) if problems else ""))

    report = render_recall(rows, new_cards)
    if aborted:
        report = ("> Run aborted early: CourtListener daily request budget (HTTP 429) was exhausted, "
                  "so not all seed cases were processed. Re-run after it resets.\n\n") + report
    print("\n" + report)

    if DRY_RUN:
        print("DRY_RUN: nothing written. %d card(s) would be added.\n" % len(new_cards))
        print(json.dumps(new_cards, indent=2, ensure_ascii=False))
        return

    if not new_cards:
        _write_pr_body(report)
        print("No new cards; nothing written.")
        return

    merged = entries + new_cards
    json.dump(merged, open(update.JSON_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = render.render(merged)
    _write_pr_body(report)
    print("wrote %d new card(s); opinions.json now %d; rendered %d into opinions.html and opinions.xml."
          % (len(new_cards), len(merged), n))


if __name__ == "__main__":
    run()