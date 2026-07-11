#!/usr/bin/env python3
"""Reconcile this run's freshly staged review cases into the rebuilt review branch.

Called by the funnel (opinions.yml) in place of a blind `cp`, so the "which files survive" logic
lives in tested Python (scripts/test_review.py) rather than workflow shell -- the same posture as
review_apply.py on the apply side. The git fetch/checkout/force-with-lease/push stay in the
workflow; this only does the file-level union, and it refuses to resurrect a case a human already
vetoed or declined on the branch.

Usage: python scripts/review_stage.py <new_root> <branch_root>
  <new_root>    this run's freshly staged review/ tree (copied aside before the branch rebuild)
  <branch_root> the rebuilt branch's review/ tree (prior batch + veto/decline markers)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_store  # noqa: E402  (sys.path shim must run first)


def main(argv):
    if len(argv) != 2:
        print("usage: review_stage.py <new_root> <branch_root>", file=sys.stderr)
        return 2
    added, skipped = review_store.merge_new_into_branch(argv[0], argv[1])
    print("review reconcile: staged %d new case-file(s), skipped %d already vetoed/declined"
          % (len(added), len(skipped)))
    for s in skipped:
        print("  . not resurrecting a dropped case: %s" % s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
