#!/usr/bin/env python3
"""Phase 4 tag backfill (scripts/tagfill.py).

One-time, cheap pass that fills the Phase 4 taxonomy fields on EXISTING cards
from their own published digests: first_impression, tort_reform, and (for
federal cards) law_applied. New cards get these from the Tier-3 summarizer; the
archive predates the fields, so this fills the gap the way ROADMAP.md Phase 4
specifies: from the existing synopses with Haiku, for cents, PR-gated.

Deliberately conservative, in both directions:
  - The model decides from the card's digest text ALONE (name, synopsis, why,
    additional holdings), never the opinion. A digest that does not itself show
    the condition yields false (or null), never a guess. Synopsis-based
    backfill therefore under-tags; the PR review is where a human adds what a
    digest under-sold.
  - It only ever ADDS: an existing true (possibly hand-set) is never removed,
    and editor_note is never touched. That field is human-only everywhere.

Env:
  ANTHROPIC_API_KEY        required to tag; without it the script prints the
                           worklist and stops (exit 0 dry, exit 1 with APPLY=1)
  APPLY=1                  write opinions.json (default: dry run, print only)
  IDS                      optional comma-separated cluster ids to limit the pass
  OPINIONS_TAGFILL_MODEL   model (default claude-haiku-4-5-20251001)

Run via .github/workflows/tagfill.yml (workflow_dispatch). The workflow
re-renders and opens a PR; nothing lands on main without review.
"""
import os, sys, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update          # anthropic_json (retries + error taxonomy) and the API key
import jurisdictions
import safeio          # crash-safe atomic writes, same as the pipeline

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(REPO, "opinions.json")

MODEL = os.environ.get("OPINIONS_TAGFILL_MODEL", "claude-haiku-4-5-20251001")
APPLY = os.environ.get("APPLY", "") in ("1", "true", "True", "yes")
IDS   = {int(x) for x in os.environ.get("IDS", "").replace(" ", "").split(",") if x}

SYSTEM = (
    "You classify one entry from a curated digest of court decisions for a Georgia "
    "civil-litigation and insurance audience. You see ONLY the published digest of the "
    "decision (its name, court, date, synopsis, why-it-matters, and any additional "
    "holdings), not the opinion itself. Decide three things from this digest text "
    "alone, and when the digest does not itself show a condition, answer false (or "
    "null). Never guess beyond the text.\n\n"
    "  - first_impression: true ONLY when the digest states that the court resolved a "
    "question of first impression, or expressly states that no controlling precedent "
    "decided the question.\n"
    "  - tort_reform: true ONLY when a holding in the digest construes or applies "
    "Georgia's recent tort-reform legislation: the 2025 SB 68 omnibus (for example "
    "O.C.G.A. sections 51-3-50 through 51-3-57 on negligent security, section 9-10-184 "
    "on anchoring, section 51-12-1.1 on medical damages, or its seatbelt, "
    "dismissal-timing, or bifurcation provisions), the 2025 SB 69 litigation-funding "
    "act, the 2024 SB 426 motor-carrier direct-action restriction, or the 2022 HB 961 "
    "amendment to O.C.G.A. section 51-12-33. A passing mention is not enough; a "
    "holding must turn on it.\n"
    "  - law_applied: for a FEDERAL decision only (Eleventh Circuit or U.S. Supreme "
    "Court), the body of substantive law the first holding turns on: 'federal' for a "
    "federal-question holding, or 'ga', 'fl', or 'al' when the holding applies that "
    "state's substantive law, for example under Erie in diversity. null when the "
    "digest does not make it clear, and null for any state-court decision.\n\n"
    "Output ONLY a JSON object: {\"first_impression\": true or false, \"tort_reform\": "
    "true or false, \"law_applied\": \"federal\", \"ga\", \"fl\", \"al\", or null}."
)


def digest_text(e):
    parts = ["Case: %s" % e.get("name", ""),
             "Court: %s" % e.get("court", ""),
             "Date: %s" % e.get("date", ""),
             "Disposition: %s" % e.get("disposition", ""),
             "Synopsis: %s" % e.get("synopsis", ""),
             "Why it matters: %s" % e.get("why", "")]
    for i, h in enumerate(e.get("additional_holdings") or [], 2):
        parts.append("Holding %d synopsis: %s" % (i, h.get("synopsis", "")))
        parts.append("Holding %d why: %s" % (i, h.get("why", "")))
    return "\n".join(parts)


def main():
    entries = json.load(open(JSON_PATH, encoding="utf-8"))
    work = [e for e in entries if not IDS or int(e.get("cluster_id", 0)) in IDS]
    print("tagfill: %d card(s) in scope (model %s, %s)"
          % (len(work), MODEL, "APPLY" if APPLY else "dry run"))
    if not update.KEY:
        print("no ANTHROPIC_API_KEY set; stopping after the worklist:")
        for e in work:
            print("  - %s  %s" % (e.get("cluster_id"), (e.get("name", "") or "")[:70]))
        sys.exit(0 if not APPLY else 1)

    fed_ok = {"federal"} | set(jurisdictions.JURISDICTIONS)
    changed = 0
    for e in work:
        v = update.anthropic_json(
            {"model": MODEL, "max_tokens": 200, "system": SYSTEM,
             "messages": [{"role": "user", "content": digest_text(e)}]},
            "tagfill")
        adds = []
        if v.get("first_impression") is True and not e.get("first_impression"):
            e["first_impression"] = True; adds.append("first_impression")
        if v.get("tort_reform") is True and not e.get("tort_reform"):
            e["tort_reform"] = True; adds.append("tort_reform")
        la = v.get("law_applied")
        la = la.strip().lower() if isinstance(la, str) else ""
        if (e.get("court") in ("ca11", "scotus") and la in fed_ok
                and not e.get("law_applied")):
            e["law_applied"] = la; adds.append("law_applied=%s" % la)
        print("  %s %-58s %s" % ("+" if adds else ".",
                                 (e.get("name", "") or "")[:58],
                                 ", ".join(adds) if adds else "no change"))
        changed += bool(adds)
        time.sleep(0.3)
    print("tagfill: %d card(s) gained fields" % changed)
    if not APPLY:
        print("dry run; nothing written. Re-run with apply to write opinions.json.")
        return
    if changed:
        safeio.atomic_write_json(JSON_PATH, entries)
        print("opinions.json written (render runs separately).")
    else:
        print("nothing to write.")


if __name__ == "__main__":
    main()
