#!/usr/bin/env python3
"""smell_check.py -- retro audit of logged drop REASONS (the "smell test", offline half).

The daily funnel now audits each run's triage-drop reasons in-line (update.py tier 2.5) and
escalates suspects to the summarizer in the same run. This script covers everything that pass
cannot reach: the backlog of drops logged BEFORE the in-run audit existed, and any run where the
in-run pass was skipped or failed (it is fail-open by design). It re-reads opinions_rejections.jsonl,
sends the un-audited triage-stage reasons to the smell model in chunks (Message Batches API at 50%
when OPINIONS_SMELL_BATCH is on, via update.smell_reasons), and:

  - annotates each audited record in place ("smell", "smell_note"; suspects additionally get
    "smell_outcome": "review") and rewrites the log atomically -- the workflow commits it as
    bookkeeping, so a record is audited ONCE and the verdict travels with the log;
  - writes scripts/smell_suspects.md when suspects are found: a human report plus ready-to-paste
    queue.txt lines (bare cluster id + "!"), so the editor's "cursory double check" is one
    copy-paste away from the queue's force path -- the same recovery Queen v. Berkley took;
  - prints the row-by-row report and a GitHub step summary.

The escalation itself stays HUMAN here, unlike the in-run pass: these drops are weeks old and
already marked evaluated, so re-reading them costs an editor decision, not an automatic Opus call.

Progress persists after every chunk (the annotated log and the suspects report are rewritten
as the run goes), and the run stops cleanly at SMELL_BUDGET_SEC -- so a crash, a ConfigError,
or the workflow watchdog discards at most one chunk's verdicts, and the leftovers simply roll
to the next weekly run. Records whose in-run escalation was deferred (smell_outcome
"deferred") are re-audited here, so a deferred second opinion is postponed, never lost.

Env:
  ANTHROPIC_API_KEY        required
  OPINIONS_SMELL_MODEL     the audit model (update.py default: OPINIONS_AUDIT_MODEL -> Opus)
  SMELL_STAGES             comma list of stages to audit; OVERRIDE only -- the value lives in
                           siteconfig.SMELL_STAGES. It read "triage" alone on the premise that
                           screen/pretriage reasons are category labels and so safe by
                           construction. False for screen: it judges a caption and an opening
                           excerpt, and 12 "In re: A v. B" captions were dropped on the prefix
                           alone, two of them Alabama Supreme Court insurance decisions.
  SMELL_ALL=1              re-audit records that already carry a smell verdict
  SMELL_LIMIT              most-recent records to consider per run (default 500)
  SMELL_BUDGET_SEC         soft wall clock for one run (default 1200, under the 30-min watchdog)
  DRY_RUN=1                report only; do not rewrite the log or write the suspects file

Run directly: `python scripts/smell_check.py`. Exit 0 unless a configuration error.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update     # noqa: E402  (sys.path shim must run first)
import safeio     # noqa: E402
import siteconfig # noqa: E402

_STAGES_ENV = os.environ.get("SMELL_STAGES", "")
STAGES  = ([s.strip() for s in _STAGES_ENV.split(",") if s.strip()]
           or list(siteconfig.SMELL_STAGES))   # config file is the source of truth; env overrides for one run
ALL     = os.environ.get("SMELL_ALL", "") in ("1", "true", "True", "yes")
LIMIT   = int(os.environ.get("SMELL_LIMIT", "500"))
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
BUDGET  = int(os.environ.get("SMELL_BUDGET_SEC", "1200"))   # soft wall clock for one run; kept
                                                            # under the workflow's 30-min watchdog
                                                            # so progress persists, never a kill
CHUNK   = 40      # reasons per model request; keeps each verdict list well inside max_tokens
OUT     = os.path.join(update.REPO, "scripts", "smell_suspects.md")


def select_records(lines, stages=None, all_flag=False, limit=None):
    """Parse rejection-log lines and pick the records to audit: the given stages, minus records
    already carrying a smell verdict (unless all_flag) -- EXCEPT records whose in-run escalation
    was deferred (smell_outcome "deferred": the funnel promised a summarizer read that never
    happened, so the retro audit re-audits them and puts them back in front of the editor.
    Keeps the most recent `limit`. Returns (records, all_parsed) where records reference the
    SAME dicts as all_parsed, so annotating a selected record mutates the full list for rewrite."""
    parsed = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue      # a corrupt line (never machine-written) is dropped on the annotate rewrite
        parsed.append(r)
    stages = stages if stages is not None else STAGES
    picked = [r for r in parsed if r.get("stage") in stages
              and (all_flag or "smell" not in r or r.get("smell_outcome") == "deferred")]
    if limit is None:
        limit = LIMIT
    return picked[-limit:], parsed


def queue_line(rec, note):
    """A ready-to-paste queue.txt line forcing this drop past the gate for editor review. Bare
    cluster id (the queue re-derives the court from the docket) + the force flag."""
    return "%s !  # smell: %s -- %s (%s %s)" % (
        rec.get("cluster_id"), note or "suspect reason",
        (rec.get("name") or "").strip(), rec.get("court") or "?", rec.get("date") or "?")


def main():
    if not update.SMELL_MODEL:
        print("OPINIONS_SMELL_MODEL is empty; the smell test is off. Nothing to do.")
        return 0
    if not update.KEY:
        print("ANTHROPIC_API_KEY is not set.")
        return 1
    if not os.path.exists(update.REJECT_PATH):
        print("no rejections log at %s; nothing to audit." % update.REJECT_PATH)
        return 0
    with open(update.REJECT_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()
    records, parsed = select_records(lines)
    if not records:
        print("no un-audited %s-stage rejection record(s); nothing to do." % "/".join(STAGES))
        return 0
    print("auditing %d drop reason(s) [stages: %s] with %s%s"
          % (len(records), "/".join(STAGES), update.SMELL_MODEL, " (DRY_RUN)" if DRY_RUN else ""))

    def persist(audited_so_far):
        """Rewrite the annotated log and the suspects report NOW. Called after every chunk, so a
        crash, a ConfigError, or the workflow watchdog can discard at most one chunk's worth of
        already-billed verdicts; everything persisted stays audited-once."""
        if DRY_RUN or not audited_so_far:
            return
        new = "\n".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in parsed)
        safeio.atomic_write_text(update.REJECT_PATH, new + "\n")
        if suspects:
            L = ["## Drop-reason audit: %d suspect drop(s) of %d audited" % (len(suspects), audited_so_far), ""]
            L += ["The smell model read each logged drop REASON on its face (not the opinions) against "
                  "the feed's triage standard. The reasons below state no recognized disqualifier, so "
                  "the drops deserve one full read. To escalate, paste the line(s) into queue.txt: the "
                  "`!` forces the case past the triage gate and the summarizer (the final editor) cards "
                  "or declines it -- the editorial PR is still your review gate.", ""]
            for r, note in suspects:
                L += ["- **%s** (%s %s) -- dropped for: “%s” -- smell: %s"
                      % ((r.get("name") or "").strip(), r.get("court") or "?", r.get("date") or "?",
                         r.get("reason") or "(none)", note or "suspect reason"),
                      "  ```", "  " + queue_line(r, note), "  ```", ""]
            open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")

    suspects = []
    audited = 0
    run_start = time.time()
    for base in range(0, len(records), CHUNK):
        remaining = run_start + BUDGET - time.time()
        if remaining < 60:
            print("  . run budget spent; %d record(s) roll to the next run (progress is saved)"
                  % (len(records) - base))
            break
        chunk = records[base:base + CHUNK]
        items = [{"name": r.get("name"), "court": r.get("court"), "date": r.get("date"),
                  "reason": r.get("reason")} for r in chunk]
        try:
            verdicts = update.smell_reasons(
                items, deadline=time.time() + min(update.SMELL_BATCH_SEC, remaining))
        except update.ConfigError:
            persist(audited)
            raise
        except Exception as e:
            print("  ! audit unavailable for records %d-%d (%s); they stay un-audited"
                  % (base + 1, base + len(chunk), e))
            continue
        _, annot = update.smell_select(chunk, verdicts, cap=len(chunk))
        for i, a in annot.items():
            r = chunk[i]
            audited += 1
            r["smell"] = a["verdict"]
            if a["note"]:
                r["smell_note"] = a["note"]
            if r.get("smell_outcome") == "deferred":
                del r["smell_outcome"]   # the deferred escalation is now resolved by this audit
            row = "%s %-52s %s" % (r.get("date") or "?", (r.get("name") or "")[:52],
                                   (r.get("reason") or "(none)")[:70])
            if a["verdict"] == "suspect":
                r["smell_outcome"] = "review"
                suspects.append((r, a["note"]))
                print("  SUS  %s  -- %s" % (row, a["note"] or "suspect reason"))
            else:
                print("  ok   %s" % row)
        skipped_n = len(chunk) - len(annot)
        if skipped_n:
            print("  . %d record(s) in this chunk got no verdict; they stay un-audited" % skipped_n)
        persist(audited)

    print("\n%d audited, %d suspect." % (audited, len(suspects)))
    if not DRY_RUN and audited:
        print("rejections log annotated (%d record(s))." % audited)
    if suspects and not DRY_RUN:
        print("suspects report written to %s." % OUT)

    summary = ["### Drop-reason audit", "",
               "- %d reason(s) audited, %d suspect" % (audited, len(suspects))]
    summary += ["- SUSPECT: %s -- %s" % ((r.get("name") or "")[:60], n or "suspect reason")
                for r, n in suspects[:20]]
    safeio.step_summary("\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
