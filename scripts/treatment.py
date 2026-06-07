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

Scope and budget. Only citers from the feed's own courts (Supreme Court of
Georgia, Court of Appeals of Georgia, Eleventh Circuit, U.S. Supreme Court) are
considered: only a court in the case's own hierarchy can treat it adversely, and
that also bounds the work. A card is swept across its full citation history the
first time (capped per run) and only for recent citers thereafter. Per-card and
per-run caps, plus a hard CourtListener call ceiling, keep one run well inside the
daily budget; anything not reached rolls to the next run. Built to run on a
weekend, when the daily updater is idle and the REST budget is free.

State (treatment_state.json) holds, per card, the resolved lead opinion id and the
set of citing clusters already evaluated. It changes only when new citers are
seen, so a quiet week writes nothing and opens no PR.

Reuses update.py (CourtListener + Anthropic plumbing), render.py, and
treatment_core.py (the shared flag model).

Env:
  ANTHROPIC_API_KEY        required
  COURTLISTENER_TOKEN      recommended (citation search + opinion text)
  TREATMENT_MODEL          classifier (default claude-sonnet-4-6)
  TREATMENT_LOOKBACK_DAYS  recent-citer window for already-swept cards (default 200)
  TREATMENT_PER_CARD       max new citers classified per card per run (default 6)
  TREATMENT_PER_RUN        max new citers classified per run, all cards (default 40)
  TREATMENT_CL_CALLS       hard CourtListener call ceiling per run (default 100)
  TREATMENT_PAGES          citer search pages per card, 20 per page (default 3)
  TREATMENT_MAXCHARS       citing-opinion characters sent to the classifier (default 9000)
  DRY_RUN=1                evaluate and print; write nothing, open no PR
  OPINIONS_DEBUG=1         verbose (inherited from update.py)

Run via .github/workflows/treatment.yml (weekend cron + manual dispatch).
"""
import os, re, sys, json, time, datetime
import urllib.parse
import update            # CourtListener + Anthropic plumbing and helpers
import render            # single source of truth renderer
import treatment_core    # shared treatment-flag model

JSON_PATH  = update.JSON_PATH
STATE_PATH = os.path.join(update.REPO, "treatment_state.json")
PR_PATH    = os.path.join(update.REPO, "scripts", "treatment_pr_body.md")

KEY          = update.KEY
MODEL        = os.environ.get("TREATMENT_MODEL", "claude-sonnet-4-6")
LOOKBACK_DAYS= int(os.environ.get("TREATMENT_LOOKBACK_DAYS", "200"))
PER_CARD     = int(os.environ.get("TREATMENT_PER_CARD", "6"))
PER_RUN      = int(os.environ.get("TREATMENT_PER_RUN", "40"))
CL_CALLS_MAX = int(os.environ.get("TREATMENT_CL_CALLS", "100"))
PAGES        = int(os.environ.get("TREATMENT_PAGES", "3"))
MAXCHARS     = int(os.environ.get("TREATMENT_MAXCHARS", "9000"))
DRY_RUN      = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")

# CourtListener court ids whose decisions can bind or treat a card in our feed.
SCOPE_COURTS = ["ga", "gactapp", "ca11", "scotus"]
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


class Budget:
    """Hard ceiling on CourtListener calls for one run."""
    def __init__(self, n):
        self.left = n
    def take(self):
        if self.left <= 0:
            raise RuntimeError("courtlistener call ceiling reached")
        self.left -= 1


def lead_opinion_id(cluster_id, budget):
    """Lead (first) sub-opinion id for a cluster, via the clusters endpoint."""
    budget.take()
    cl = update.cl_get("/api/rest/v4/clusters/%d/" % int(cluster_id))
    for s in (cl.get("sub_opinions") or []):
        m = re.search(r"/opinions/(\d+)/", s) if isinstance(s, str) else None
        if m:
            return int(m.group(1))
    return None


def citing_results(opinion_id, since, budget):
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
        budget.take()
        data = update.cl_get(url)
        out += data.get("results", [])
        url = data.get("next")
        pages += 1
        time.sleep(0.5)
    return out


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


def main():
    if not KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)

    # Guarantee the PR body exists on every exit path (the workflow reads it).
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No treatment changes this run.\n")

    entries = json.load(open(JSON_PATH, encoding="utf-8"))
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    if not isinstance(state, dict):
        state = {}

    budget = Budget(CL_CALLS_MAX)
    classified = 0
    report = []        # (card_name, citing_name, citing_date, verdict_str)
    new_flags = []     # card dicts newly raised to caution this run
    changed = False    # any tracked-file change (state grew, or a flag changed)
    ceiling = False

    # Never-swept cards first (full history), oldest first within each group so the
    # most-cited landmarks are worked through across the early runs.
    order = sorted(entries, key=lambda e: (str(e.get("cluster_id")) in state, e.get("date", "")))

    for card in order:
        if classified >= PER_RUN or budget.left <= 2:
            break
        cid = card.get("cluster_id")
        if not cid:
            continue
        if (card.get("treatment") or "ok") == "superseded":
            continue                                   # already dead; stop spending on it
        key = str(int(cid))
        st = state.get(key) or {}
        seen = set(st.get("seen", []))
        first_time = key not in state

        try:
            oid = st.get("oid")
            if oid is None:
                oid = lead_opinion_id(int(cid), budget)
            if not oid:
                state[key] = {"oid": None, "seen": sorted(seen)}   # do not refetch weekly
                changed = True
                continue
            since = (card["date"] if first_time
                     else (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat())
            citers = citing_results(int(oid), since, budget)
        except RuntimeError as e:
            if "ceiling" in str(e):
                ceiling = True
                break
            print("  ! citation lookup failed for %s (%s): %s" % (cid, card.get("name"), e))
            continue
        except Exception as e:
            print("  ! citation lookup failed for %s (%s): %s" % (cid, card.get("name"), e))
            continue

        before = set(seen)
        per_card = 0
        for r in citers:                               # newest first
            if classified >= PER_RUN or per_card >= PER_CARD or budget.left <= 2:
                break
            ccid = update.cluster_id_of(r)
            if not ccid or ccid == int(cid) or ccid in seen:
                continue
            if (r.get("court_id") or "") not in SCOPE_COURTS:
                continue
            cname = r.get("caseName") or r.get("caseNameFull") or "(unnamed)"
            cdate = (r.get("dateFiled") or "")[:10]
            ccourt = r.get("court_id") or ""
            try:
                budget.take()
                oid2 = update.opinion_id_of(r)
                ctext = update.opinion_text(oid2) if oid2 else ""
                v = classify(card, cname, ctext)
            except RuntimeError as e:
                if "ceiling" in str(e):
                    ceiling = True
                    break
                print("  ! classify failed citing=%s: %s" % (ccid, e))
                continue
            except Exception as e:
                print("  ! classify failed citing=%s: %s" % (ccid, e))
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
            state[key] = {"oid": oid, "seen": sorted(seen)}
            changed = True
        if ceiling:
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
    if ceiling:
        lines += ["", "_CourtListener call ceiling reached; remaining cards roll to the next run._"]
    pr_body = "\n".join(lines) + "\n"

    print("\nclassified %d citing opinion(s); new flags: %d; CL calls left: %d%s"
          % (classified, len(new_flags), budget.left, " (ceiling hit)" if ceiling else ""))

    if DRY_RUN:
        print("\n--- DRY RUN, nothing written ---\n" + pr_body)
        return

    open(PR_PATH, "w", encoding="utf-8").write(pr_body)
    if not changed and not new_flags:
        print("no new citing activity; files unchanged.")
        return

    # Persist progress always; rewrite opinions.json and re-render only when a flag
    # actually changed a card (new_flags) -- state-only weeks still record progress.
    json.dump({k: state[k] for k in sorted(state)}, open(STATE_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    if new_flags:
        json.dump(entries, open(JSON_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        render.render(entries)
        print("wrote treatment_state.json + opinions.json; re-rendered.")
    else:
        print("wrote treatment_state.json (progress only; no card changed).")


if __name__ == "__main__":
    main()
