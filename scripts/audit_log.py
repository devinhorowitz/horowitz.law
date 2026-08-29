#!/usr/bin/env python3
"""audit_log.py -- record what an audit ESTABLISHED about a logged drop, durably, in the repo.

The rejection log says what the funnel decided. Until now it did not say what anyone later found
out. Two audits (issues #283 and #293) read 37 opinions in full and recovered nothing, and that
result lived only in GitHub comments -- so the next tool to look at the same log would re-read the
same 37 opinions and still could not tell a drop nobody has checked from one that was checked hard
and confirmed. This script closes that gap: it writes an `audit` block (see update.record_audit)
onto the matching records so the finding travels with the data.

The point is accumulation across tools, not less work. A verdict recorded by a 2026 model with a
CourtListener read is still the strongest claim on that drop in 2029 unless someone does better,
and `--depth` is how a later reader knows how much to trust it.

Usage:

  # one drop
  python scripts/audit_log.py --cluster 10934781 --verdict confirmed \\
      --depth full_opinion --by descrybe+cl/2026-08-29 --note "bare PCA, no merits"

  # a batch: TSV of  cluster_id <TAB> verdict <TAB> note   (blank lines and # comments skipped)
  python scripts/audit_log.py --from-file audits.tsv --depth full_opinion --by me

  # what is still unread
  python scripts/audit_log.py --list-unaudited --stage screen --limit 20

  # what is known so far
  python scripts/audit_log.py --summary

--dry-run prints the changes and writes nothing. Exit 0 on success, 1 on a usage or match error.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update    # noqa: E402  (sys.path shim must run first)
import safeio    # noqa: E402


def load(path):
    """Every record in the log, in order. A corrupt line is dropped rather than aborting the run --
    the same tolerance select_records has, and the rewrite below then removes it for good."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f.read().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def save(path, records):
    safeio.atomic_write_text(
        path, "\n".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False)
                        for r in records) + "\n")


def parse_batch(path):
    """TSV rows of cluster_id, verdict, and an optional note."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, ln in enumerate(f.read().splitlines(), 1):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("\t")]
            if len(parts) < 2:
                raise ValueError("line %d: expected 'cluster_id<TAB>verdict[<TAB>note]', got %r" % (n, ln))
            rows.append((int(parts[0]), parts[1], parts[2] if len(parts) > 2 else ""))
    return rows


def apply_audits(records, rows, depth, by, dry_run=False):
    """Stamp each (cluster_id, verdict, note) onto its record. Returns (applied, skipped, missing)."""
    index = {}
    for r in records:
        cid = r.get("cluster_id")
        if cid is not None:
            index.setdefault(int(cid), []).append(r)
    applied, skipped, missing = [], [], []
    for cid, verdict, note in rows:
        hits = index.get(int(cid))
        if not hits:
            missing.append(cid)
            continue
        for r in hits:
            before = json.dumps(r.get("audit"), sort_keys=True)
            update.record_audit(r, verdict, depth=depth, by=by, note=note)
            if json.dumps(r.get("audit"), sort_keys=True) == before:
                skipped.append(cid)      # a stronger claim already stands; record_audit refused
            else:
                applied.append((cid, verdict, r.get("name") or ""))
    if dry_run:
        pass    # caller does not save
    return applied, skipped, missing


def categories(records, stage=None):
    """Drop counts per category token, and the invalid ones. This is the whole point of storing a
    token instead of prose: 'how many probate drops last month, and from which court' is a question
    free text cannot answer, and a category whose rate jumps is how a prompt regression announces
    itself before anyone reads a reason."""
    counts, invalid, untokened = {}, {}, 0
    for r in records:
        if stage and r.get("stage") != stage:
            continue
        if r.get("stage") not in update.STAGE_CATEGORIES:
            continue                      # a stage with no closed list has nothing to count
        bad = r.get("category_invalid")
        cat = (r.get("category") or "").strip()
        if bad:
            invalid[bad] = invalid.get(bad, 0) + 1
        if not cat:
            untokened += 1
            continue
        counts[cat] = counts.get(cat, 0) + 1
    return counts, invalid, untokened


def summarize(records):
    by_verdict, by_depth, unaudited = {}, {}, 0
    for r in records:
        a = r.get("audit")
        if not a:
            unaudited += 1
            continue
        by_verdict[a.get("verdict")] = by_verdict.get(a.get("verdict"), 0) + 1
        by_depth[a.get("depth")] = by_depth.get(a.get("depth"), 0) + 1
    return by_verdict, by_depth, unaudited


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cluster", type=int, action="append", default=[],
                    help="cluster id to stamp (repeatable)")
    ap.add_argument("--verdict", choices=update.AUDIT_VERDICTS,
                    help="what the audit established; required with --cluster")
    ap.add_argument("--depth", choices=update.AUDIT_DEPTHS, default="full_opinion",
                    help="how the finding was reached (default: full_opinion)")
    ap.add_argument("--by", default="", help="tool, model or person that did the reading")
    ap.add_argument("--note", default="", help="one line on what was found")
    ap.add_argument("--from-file", help="TSV batch: cluster_id<TAB>verdict[<TAB>note]")
    ap.add_argument("--list-unaudited", action="store_true", help="print drops carrying no audit")
    ap.add_argument("--summary", action="store_true", help="print what is known so far")
    ap.add_argument("--categories", action="store_true",
                    help="drop counts per closed-list token, and any invalid ones")
    ap.add_argument("--stage", help="restrict --list-unaudited to one stage")
    ap.add_argument("--limit", type=int, default=40, help="rows for --list-unaudited (default 40)")
    ap.add_argument("--path", default=update.REJECT_PATH, help="rejection log (default: the repo's)")
    ap.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    a = ap.parse_args(argv)

    records = load(a.path)
    if not records:
        print("no records in %s" % a.path)
        return 1

    if a.summary:
        v, d, un = summarize(records)
        print("%d record(s); %d unaudited" % (len(records), un))
        for k in sorted(v):
            print("  verdict %-14s %d" % (k, v[k]))
        for k in sorted(d):
            print("  depth   %-14s %d" % (k, d[k]))
        return 0

    if a.categories:
        counts, invalid, untokened = categories(records, a.stage)
        total = sum(counts.values())
        print("%d tokened drop(s)%s" % (total, " in stage %s" % a.stage if a.stage else ""))
        for k in sorted(counts, key=lambda x: -counts[x]):
            print("  %-24s %4d  (%.0f%%)" % (k, counts[k], 100.0 * counts[k] / total) if total
                  else "  %-24s %4d" % (k, counts[k]))
        if untokened:
            print("\n%d drop(s) carry no token (logged before the category contract, or omitted)"
                  % untokened)
        if invalid:
            print("\ninvalid tokens:")
            for k in sorted(invalid, key=lambda x: -invalid[x]):
                print("  %-24s %4d" % (k, invalid[k]))
        return 0

    if a.list_unaudited:
        n = 0
        for r in records:
            if r.get("audit"):
                continue
            if a.stage and r.get("stage") != a.stage:
                continue
            n += 1
            if n > a.limit:
                continue
            print("%-10s %-9s %s  %-46s %s" % (r.get("cluster_id"), r.get("stage"),
                                               r.get("date") or "?", (r.get("name") or "")[:46],
                                               (r.get("reason") or "")[:52]))
        print("\n%d unaudited%s" % (n, " (showing %d)" % a.limit if n > a.limit else ""))
        return 0

    rows = []
    if a.from_file:
        rows += parse_batch(a.from_file)
    if a.cluster:
        if not a.verdict:
            ap.error("--verdict is required with --cluster")
        rows += [(c, a.verdict, a.note) for c in a.cluster]
    if not rows:
        ap.error("nothing to do: pass --cluster, --from-file, --list-unaudited, "
                 "--categories or --summary")

    applied, skipped, missing = apply_audits(records, rows, a.depth, a.by, a.dry_run)
    for cid, verdict, name in applied:
        print("  %-10s %-13s %s" % (cid, verdict, name[:60]))
    if skipped:
        print("  . %d unchanged (a full_opinion finding already stands): %s"
              % (len(skipped), ", ".join(str(c) for c in skipped[:10])))
    if missing:
        print("  ! %d cluster id(s) not in the log: %s"
              % (len(missing), ", ".join(str(c) for c in missing[:10])))
    if a.dry_run:
        print("\nDRY RUN: %d record(s) would be stamped; nothing written." % len(applied))
        return 1 if missing else 0
    if applied:
        save(a.path, records)
        print("\n%d record(s) stamped in %s." % (len(applied), a.path))
    else:
        print("\nnothing to write.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
