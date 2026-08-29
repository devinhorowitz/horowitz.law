#!/usr/bin/env python3
"""Hermetic unit tests for scripts/audit_log.py -- durable audit provenance on the rejection log.

Standard library only; no network and no API key. Every test runs against a temporary log, never
the repo's own.

What these pin is the reason the script exists: an audit finding has to survive in the repo, and it
has to be safe to re-run. A batch that half-applies, a stamp that clobbers a stronger prior
finding, or a silent no-match on a mistyped cluster id would each quietly put the log back to being
a record of decisions with no record of what anyone later established.

Run directly: `python scripts/test_audit_log.py`.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_log   # noqa: E402  (sys.path shim must run first)

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


RECORDS = [
    {"stage": "screen", "cluster_id": 111, "name": "A v. B", "reason": "probate"},
    {"stage": "screen", "cluster_id": 222, "name": "C v. D", "reason": "family"},
    {"stage": "triage", "cluster_id": 333, "name": "E v. F", "reason": "criminal"},
]


def write_log(records=None):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "rej.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in (RECORDS if records is None else records):
            f.write(json.dumps(r) + "\n")
    return p


def read_log(p):
    return [json.loads(l) for l in open(p, encoding="utf-8").read().splitlines() if l.strip()]


def test_stamp_and_persist():
    p = write_log()
    rc = audit_log.main(["--cluster", "111", "--verdict", "confirmed", "--depth", "full_opinion",
                         "--by", "tool/x", "--note", "bare PCA", "--path", p])
    check("exit 0 on a clean stamp", rc == 0)
    recs = read_log(p)
    a = recs[0].get("audit") or {}
    check("verdict written", a.get("verdict") == "confirmed")
    check("depth written", a.get("depth") == "full_opinion")
    check("attribution written", a.get("by") == "tool/x")
    check("note written", a.get("note") == "bare PCA")
    check("untouched records keep no audit", not recs[1].get("audit") and not recs[2].get("audit"))
    check("record count is unchanged", len(recs) == 3)


def test_rerun_is_safe():
    """Re-running the same batch must not weaken or duplicate anything -- a scheduled job or a
    retried command should be free to repeat."""
    p = write_log()
    audit_log.main(["--cluster", "111", "--verdict", "confirmed", "--by", "x", "--path", p])
    first = read_log(p)[0]["audit"]
    audit_log.main(["--cluster", "111", "--verdict", "confirmed", "--by", "x", "--path", p])
    again = read_log(p)[0]["audit"]
    check("re-running keeps one audit block", isinstance(again, dict))
    check("re-running keeps the same verdict", again["verdict"] == first["verdict"])
    # A reason-only pass arriving later must not erase the full read.
    audit_log.main(["--cluster", "111", "--verdict", "recovered", "--depth", "reason_only",
                    "--by", "smell", "--path", p])
    after = read_log(p)[0]["audit"]
    check("a weaker later pass cannot overwrite a full read",
          after["verdict"] == "confirmed" and after["by"] == "x")


def test_batch_file():
    p = write_log()
    d = tempfile.mkdtemp()
    tsv = os.path.join(d, "a.tsv")
    open(tsv, "w", encoding="utf-8").write(
        "# comment line\n\n111\tconfirmed\tbare PCA\n222\trecovered\tbelonged in the feed\n")
    rc = audit_log.main(["--from-file", tsv, "--depth", "full_opinion", "--by", "b", "--path", p])
    recs = read_log(p)
    check("batch exit 0", rc == 0)
    check("batch stamps the first row", recs[0]["audit"]["verdict"] == "confirmed")
    check("batch stamps the second row", recs[1]["audit"]["verdict"] == "recovered")
    check("batch carries per-row notes", recs[1]["audit"]["note"] == "belonged in the feed")
    check("comments and blank lines skipped", not recs[2].get("audit"))


def test_missing_cluster_is_loud():
    """A mistyped cluster id must not look like success: silently stamping nothing is how an audit
    convinces itself work is recorded when it is not."""
    p = write_log()
    rc = audit_log.main(["--cluster", "999", "--verdict", "confirmed", "--by", "x", "--path", p])
    check("unmatched cluster exits non-zero", rc == 1)
    check("unmatched cluster writes nothing", all(not r.get("audit") for r in read_log(p)))


def test_dry_run_writes_nothing():
    p = write_log()
    audit_log.main(["--cluster", "111", "--verdict", "confirmed", "--by", "x",
                    "--dry-run", "--path", p])
    check("dry run leaves the log alone", all(not r.get("audit") for r in read_log(p)))


def test_bad_batch_row_raises():
    p = write_log()
    d = tempfile.mkdtemp()
    tsv = os.path.join(d, "bad.tsv")
    open(tsv, "w", encoding="utf-8").write("111\n")     # no verdict column
    try:
        audit_log.main(["--from-file", tsv, "--by", "x", "--path", p])
        check("a malformed batch row is rejected", False)
    except ValueError as e:
        check("a malformed batch row is rejected", "expected" in str(e))
    check("a malformed batch writes nothing", all(not r.get("audit") for r in read_log(p)))


def test_summary_and_listing():
    p = write_log()
    audit_log.main(["--cluster", "111", "--verdict", "confirmed", "--by", "x", "--path", p])
    recs = read_log(p)
    v, d, un = audit_log.summarize(recs)
    check("summary counts verdicts", v.get("confirmed") == 1)
    check("summary counts depths", d.get("full_opinion") == 1)
    check("summary counts what is left", un == 2)
    check("listing exits 0", audit_log.main(["--list-unaudited", "--path", p]) == 0)
    check("stage filter is accepted",
          audit_log.main(["--list-unaudited", "--stage", "triage", "--path", p]) == 0)


def test_corrupt_line_survives():
    """A corrupt line must not abort the run, and must not be rewritten back into the log."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "rej.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(RECORDS[0]) + "\n{not json\n" + json.dumps(RECORDS[1]) + "\n")
    rc = audit_log.main(["--cluster", "111", "--verdict", "confirmed", "--by", "x", "--path", p])
    recs = read_log(p)
    check("corrupt line does not abort", rc == 0)
    check("corrupt line is dropped on rewrite", len(recs) == 2)
    check("good records survive", recs[0]["audit"]["verdict"] == "confirmed")



def test_categories_report():
    """Counting drops per token is the reason a token exists at all -- free text cannot be grouped,
    and a category whose rate jumps is how a prompt regression shows itself before anyone reads a
    reason. Untokened history must read as untokened, never as invalid."""
    recs = [
        {"stage": "screen", "cluster_id": 1, "category": "probate"},
        {"stage": "screen", "cluster_id": 2, "category": "probate"},
        {"stage": "screen", "cluster_id": 3, "category": "criminal"},
        {"stage": "screen", "cluster_id": 4, "category": "landlord_tenant",
         "category_invalid": "landlord_tenant"},
        {"stage": "screen", "cluster_id": 5},                       # predates the contract
        {"stage": "pretriage", "cluster_id": 6, "category": "bankruptcy"},
        {"stage": "triage", "cluster_id": 7},                       # no closed list, not counted
    ]
    counts, invalid, untokened = audit_log.categories(recs)
    check("counts group by token", counts.get("probate") == 2 and counts.get("criminal") == 1)
    check("an invalid token is reported separately", invalid.get("landlord_tenant") == 1)
    check("untokened history is untokened, not invalid", untokened == 1)
    check("a stage with no closed list is excluded", "triage" not in counts and counts.get(None) is None)
    check("pretriage tokens are counted", counts.get("bankruptcy") == 1)
    only, _, _ = audit_log.categories(recs, stage="pretriage")
    check("stage filter narrows the count", only == {"bankruptcy": 1})
    p = write_log()
    check("--categories exits 0", audit_log.main(["--categories", "--path", p]) == 0)


def main():
    print("stamping:")
    test_stamp_and_persist()
    test_rerun_is_safe()
    print("batch:")
    test_batch_file()
    test_bad_batch_row_raises()
    print("failure modes:")
    test_missing_cluster_is_loud()
    test_dry_run_writes_nothing()
    test_corrupt_line_survives()
    print("reporting:")
    test_summary_and_listing()
    test_categories_report()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
