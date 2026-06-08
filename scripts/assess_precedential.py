#!/usr/bin/env python3
"""Backfill `precedential` on existing Georgia Appellate Watch cards (one-off).

The precedential label (published / unpublished / physical precedent / unknown) was
added to the funnel after the archive already held cards, so older cards carry no
assessment and render with no caveat, reading as binding authority. This stamps the
field from CourtListener's authoritative `precedential_status` metadata (one cluster
lookup per card that lacks the field), re-renders, and opens the editorial PR.

Idempotent: a card that already carries a non-empty `precedential` is skipped, so a
budget-deferred partial run can simply be re-dispatched to finish, and once every card
is labeled the workflow is a no-op.

Scope note: CourtListener metadata gives the published/unpublished binary reliably. It
does not mark a Court of Appeals of Georgia opinion as "physical precedent only" (Rule
33.2) -- only the opinion text shows that -- so such opinions map to 'published' here.
The editor can correct any card by hand in opinions.json; only 'unpublished' renders a
caveat, so the binary is what matters for reliance.

Env:
  COURTLISTENER_TOKEN       recommended (cluster metadata lookups)
  DRY_RUN=1                 print the labels in the log; write nothing, open no PR
  OPINIONS_CL_DEADLINE_SEC  per-cluster metadata deadline (default 30)

Run via .github/workflows/assess-precedential.yml (workflow_dispatch).
"""
import os, sys, json, time
import update            # cl_get, cluster_id_of, constants, ConfigError
import render            # re-render after stamping
import cl_rate           # shared CourtListener REST budget
import safeio            # crash-safe atomic writes

DRY_RUN     = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
PR_PATH     = os.path.join(update.REPO, "scripts", "assess_precedential_pr_body.md")
CL_DEADLINE = int(os.environ.get("OPINIONS_CL_DEADLINE_SEC", "30"))

# CourtListener precedential_status -> feed vocabulary. Only 'unpublished' renders a
# caveat; 'published' and 'unknown' both render no badge, so the mapping only needs to
# identify the unpublished cases correctly.
_MAP = {
    "published": "published",
    "unpublished": "unpublished",
    "errata": "published",        # an errata to a published opinion
    "separate": "unknown",
    "in-chambers": "unknown",
    "relating-to": "unknown",
    "unknown": "unknown",
}


def classify_status(cl_status):
    return _MAP.get((cl_status or "").strip().lower().replace(" ", "-"), "unknown")


def run():
    if not update.CL_TOKEN:
        print("  ! warning: COURTLISTENER_TOKEN not set; cluster metadata lookups may be rate-limited or denied.")
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No precedential assessment this run.\n")

    entries = json.load(open(update.JSON_PATH, encoding="utf-8"))
    todo = [e for e in entries if not (e.get("precedential") or "").strip() and e.get("cluster_id")]
    print("cards: %d | needing assessment: %d" % (len(entries), len(todo)))
    if not todo:
        print("every card already has a precedential label; nothing to do."); return

    rows, changed, deferred = [], 0, 0
    for e in todo:
        if cl_rate.remaining() <= 0:
            deferred += 1; continue
        cid = int(e["cluster_id"])
        try:
            cl = update.cl_get("/api/rest/v4/clusters/%d/" % cid, deadline=time.time() + CL_DEADLINE)
        except cl_rate.RateBudgetExceeded:
            deferred += 1; continue
        except update.ConfigError:
            raise
        except Exception as ex:
            print("  ! lookup failed for %s (%s): %s" % (cid, e.get("name", "?"), ex))
            rows.append((e.get("name", ""), "error", str(ex)[:80])); continue
        raw = (cl.get("precedential_status") or "").strip()
        label = classify_status(raw)
        e["precedential"] = label
        changed += 1
        rows.append((e.get("name", ""), label, "CL: %s" % (raw or "(blank)")))
        print("  + %s -> %s (CL: %s)" % (e.get("name", "")[:46], label, raw or "(blank)"))

    nonpub = [r for r in rows if r[1] == "unpublished"]
    L = ["## Georgia Appellate Watch: precedential backfill", "",
         "Stamped `precedential` on %d existing card(s) from CourtListener metadata%s."
         % (changed, (" (%d deferred on the CourtListener budget; re-dispatch to finish)" % deferred) if deferred else ""),
         ""]
    if nonpub:
        L.append("**Now flagged as not binding precedent (verify and adjust by hand if needed):**")
        L += ["- %s" % nm for nm, lab, src in nonpub]
        L.append("")
    L += ["| case | precedential | source |", "|---|---|---|"]
    L += ["| %s | %s | %s |" % (nm, lab, src) for nm, lab, src in rows]
    report = "\n".join(L) + "\n"
    print("\n" + report)
    print("CourtListener REST calls: %d" % cl_rate.PACER.calls)

    if DRY_RUN:
        open(PR_PATH, "w", encoding="utf-8").write(report)
        print("DRY_RUN: nothing written. %d card(s) would be labeled." % changed); return
    if not changed:
        open(PR_PATH, "w", encoding="utf-8").write(report)
        print("no changes; nothing written."); return

    safeio.atomic_write_json(update.JSON_PATH, entries)
    render.render(entries)
    open(PR_PATH, "w", encoding="utf-8").write(report)
    print("wrote opinions.json (%d labeled%s) and re-rendered."
          % (changed, ("; %d deferred to a re-run" % deferred) if deferred else ""))


if __name__ == "__main__":
    run()
