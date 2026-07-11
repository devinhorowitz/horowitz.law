#!/usr/bin/env python3
"""Publish planner for the funnel's write-to-main step (opinions.yml).

The bookkeeping that pushes a funnel run's output to main used to live entirely in a bash step, and
that is where this project's two worst incidents came from: `git add` is atomic, so one missing
conditionally-written file made it stage NOTHING and the run failed on a dirty-tree rebase; and the
hand-maintained CONTENT list drifted from what render() actually writes, so a re-stamped static page
(footer year on Jan 1) was left unstaged and aborted the push. Both are file-selection logic, which
belongs in tested Python, not untested bash.

This module owns that decision. It is a thin, PURE core (`plan`) over inputs any test can construct,
plus a `main` that gathers those inputs from git/disk and prints the plan for the workflow to act on:

    line 1            mode: "auto" (new site content to publish), "bookkeeping" (only run-state
                      files changed), or "noop" (nothing to commit)
    lines 2..N        the existing paths to `git add`, one per line

The workflow does only git plumbing with that: stage the listed paths, commit with the mode's
message, and push -- so a missing file can never strand the tree (only existing paths are listed)
and the CONTENT set cannot drift (it is derived from render.OUTPUT_PATHS, the single source of truth
for what render owns).

Run by the workflow as `python scripts/publish.py`; importable for tests without side effects.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import render  # noqa: E402  -- for OUTPUT_PATHS, the authoritative render-owned set

# What a run may write to main, in two classes:
#   CONTENT -- the data (opinions.json) plus every file render() owns. A change here is a real
#              publish: it goes to main WITHOUT [skip ci] so CI re-validates the deployed pages.
#   BOOK    -- run-state ledgers. Several are written CONDITIONALLY (no held cases -> no
#              pending_review/redraft; Fable review off -> no fable log), so they may not exist;
#              `present()` filters to what actually does. A bookkeeping-only change is committed
#              with [skip ci] since it never affects the site.
CONTENT = ["opinions.json"] + list(render.OUTPUT_PATHS)
BOOK = ["opinions_state.json", "opinions_pending_review.json", "opinions_redraft.jsonl",
        "opinions_pipeline_log.jsonl", "opinions_rejections.jsonl", "opinions_fable_review.jsonl",
        "skill_alert_state.json"]

AUTO_MSG = "opinions: publish new appellate decisions"
BOOK_MSG = "opinions: record run state [skip ci]"


def present(paths, root):
    """The subset of `paths` that exists on disk under `root`. Directories (public/o, public/areas)
    count as present when the directory exists; git add stages their contents recursively."""
    return [p for p in paths if os.path.exists(os.path.join(root, p))]


def plan(content_changed, book_changed, present_content, present_book):
    """Pure publish decision. Given whether any CONTENT / BOOK path shows as changed in git, and
    which CONTENT/BOOK paths exist, return (mode, stage, message):

      * content changed -> "auto": stage all present content + book, publish (no [skip ci]).
      * only book changed -> "bookkeeping": stage present book, commit is [skip ci].
      * nothing changed  -> "noop": stage nothing, no commit (a quiet scan stays silent, matching
                            the prior step's "No bookkeeping changes").

    Staging the full present set (not just the changed paths) is deliberate and matches the prior
    behavior: git figures out what actually differs at commit time, and listing only existing paths
    is what keeps `git add` from choking on an absent conditional file."""
    if content_changed:
        return ("auto", list(present_content) + list(present_book), AUTO_MSG)
    if book_changed:
        return ("bookkeeping", list(present_book), BOOK_MSG)
    return ("noop", [], "")


def _changed(root, paths):
    """True if git reports any of `paths` as modified or untracked. `git status --porcelain` lists
    untracked files (a brand-new o/<id>.html permalink shows as `??`) by default, so a new
    generated file is detected without the intent-to-add dance the bash version needed."""
    out = subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "--"] + list(paths),
        capture_output=True, text=True, check=True).stdout
    return bool(out.strip())


def main(root=None):
    root = root or REPO
    mode, stage, _ = plan(_changed(root, CONTENT), _changed(root, BOOK),
                          present(CONTENT, root), present(BOOK, root))
    lines = [mode] + stage
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
