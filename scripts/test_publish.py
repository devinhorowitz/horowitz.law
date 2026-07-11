#!/usr/bin/env python3
"""Hermetic unit test for the funnel's publish planner (scripts/publish.py), no network.

publish.plan() is the file-selection + auto/bookkeeping decision that used to live in an untested
bash step and caused both of this project's write-to-main incidents (an atomic `git add` choking on
a missing conditional file; a hand-maintained CONTENT list drifting from render's output). This pins
that logic: the pure decision across every mode, that only EXISTING paths are ever staged, and -- in
a throwaway git repo -- that main() detects content changes (including a brand-new untracked
permalink) and emits a plan the workflow can act on.

Run directly: `python scripts/test_publish.py`.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish  # noqa: E402
import render   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def _git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True)


def test_plan_pure():
    content = ["opinions.json", "public/opinions.html"]
    book = ["opinions_state.json", "opinions_pipeline_log.jsonl"]

    mode, stage, msg = publish.plan(True, False, content, book)
    check("plan: content changed -> auto", mode == "auto")
    check("plan: auto stages content + book", stage == content + book)
    check("plan: auto message has no [skip ci]", msg == publish.AUTO_MSG and "[skip ci]" not in msg)

    mode, stage, msg = publish.plan(False, True, content, book)
    check("plan: only book changed -> bookkeeping", mode == "bookkeeping")
    check("plan: bookkeeping stages only book", stage == book)
    check("plan: bookkeeping message is [skip ci]", msg == publish.BOOK_MSG and "[skip ci]" in msg)

    mode, stage, msg = publish.plan(False, False, content, book)
    check("plan: nothing changed -> noop even though files exist", mode == "noop" and stage == [])

    # The bug class: a bookkeeping file absent must never appear in the stage list (git add would
    # otherwise abort atomically). plan() stages exactly what present() passes it, nothing more.
    mode, stage, _ = publish.plan(True, False, content, ["opinions_state.json"])
    check("plan: absent conditional book file is not staged", stage == content + ["opinions_state.json"])


def test_present(tmp):
    open(os.path.join(tmp, "here.json"), "w").close()
    os.makedirs(os.path.join(tmp, "adir"))
    got = publish.present(["here.json", "gone.jsonl", "adir"], tmp)
    check("present: keeps existing file and dir, drops missing", got == ["here.json", "adir"])


def test_content_set_derived():
    # CONTENT must be opinions.json + exactly render's output set -- derived, not hand-copied, so it
    # cannot drift from what render writes (the Jan-1 regression).
    check("CONTENT = opinions.json + render.OUTPUT_PATHS",
          publish.CONTENT == ["opinions.json"] + list(render.OUTPUT_PATHS))


def test_main_integration(tmp):
    """Drive main() in a real (throwaway) git repo, with CONTENT/BOOK pointed at small test paths."""
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "t@t.co")
    _git(tmp, "config", "user.name", "t")
    # Seed: one committed content file, one committed book file, an o/ dir render manages.
    for rel in ("opinions.json", "state.json"):
        with open(os.path.join(tmp, rel), "w") as f:
            f.write("[]")
    os.makedirs(os.path.join(tmp, "public", "o"))
    with open(os.path.join(tmp, "public", "o", "1.html"), "w") as f:
        f.write("x")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-qm", "seed")

    saved = (publish.CONTENT, publish.BOOK)
    publish.CONTENT = ["opinions.json", "public/o"]
    publish.BOOK = ["state.json", "absent_ledger.jsonl"]  # absent_ledger never exists
    try:
        # Call main(root=tmp) directly (main() uses module CONTENT/BOOK, which we patched) and
        # capture what it prints for the workflow.
        import io
        import contextlib

        def plan_lines():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                publish.main(root=tmp)
            return buf.getvalue().strip().split("\n")

        # Clean tree -> nothing changed -> noop (a quiet scan stays silent).
        lines = plan_lines()
        check("main: clean tree -> noop", lines == ["noop"], "got %r" % lines)

        # Change only a book file -> bookkeeping, stages only the existing ledger.
        with open(os.path.join(tmp, "state.json"), "w") as f:
            f.write('{"seen": [1]}')
        lines = plan_lines()
        check("main: only book changed -> bookkeeping", lines[0] == "bookkeeping", "got %r" % lines)
        check("main: bookkeeping stages only the existing ledger", lines[1:] == ["state.json"])

        # Modify a content file -> auto; stage list excludes the absent ledger.
        with open(os.path.join(tmp, "opinions.json"), "w") as f:
            f.write('[{"cluster_id": 1}]')
        lines = plan_lines()
        check("main: content change -> auto", lines[0] == "auto", "got %r" % lines)
        check("main: auto stage list has no missing file", "absent_ledger.jsonl" not in lines)
        check("main: auto stages the changed content + existing book",
              set(lines[1:]) == {"opinions.json", "public/o", "state.json"}, "got %r" % lines)

        # A brand-new untracked permalink under o/ must register as a content change. Restore
        # opinions.json to its committed content first so the ONLY change is the new file.
        _git(tmp, "checkout", "--", "opinions.json")
        with open(os.path.join(tmp, "public", "o", "2.html"), "w") as f:
            f.write("new")              # untracked new permalink
        lines = plan_lines()
        check("main: a new untracked permalink is detected as a content change", lines[0] == "auto", "got %r" % lines)
    finally:
        publish.CONTENT, publish.BOOK = saved


def main():
    print("publish planner:")
    test_plan_pure()
    test_content_set_derived()
    for t in (test_present, test_main_integration):
        tmp = tempfile.mkdtemp(prefix="publish_test_")
        try:
            t(tmp)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
