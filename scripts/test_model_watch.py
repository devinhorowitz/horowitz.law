#!/usr/bin/env python3
"""Self-tests for scripts/model_watch.py. Standard library only; no network, no API key.

Covers the detection logic that decides whether to open a model-bump PR: tier parsing,
the recency comparison (created_at, with a version-number fallback), the within-tier-only
rule, the higher-tier and deprecation notes, and the whole-id pin rewrite. All on
synthetic model lists built here, so the guard is pinned without touching the live API.

Also pins model-watch.yml's side of the contract: the bump PR's add-paths is exactly
PIN_FILES, and the eval step reads only golden_check's exit 1 as a regression. Those parse
the workflow with pyyaml (CI installs it) and skip without it.

  python scripts/test_model_watch.py     # prints each case; exits nonzero on any failure
"""
import datetime
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import model_watch  # after sys.path, mirroring the other scripts' import-by-sibling-name pattern
import update  # noqa: E402 -- the effort rule model_watch cross-checks


def _m(model_id, y=2026, mo=1, d=1, display=""):
    """A synthetic Models-API entry: id, display name, and a parsed created_at."""
    return {"id": model_id, "display_name": display or model_id,
            "dt": datetime.datetime(y, mo, d)}


def test_tier():
    assert model_watch._tier("claude-sonnet-5") == "sonnet"
    assert model_watch._tier("claude-opus-4-8") == "opus"
    assert model_watch._tier("claude-haiku-4-5-20251001") == "haiku"
    assert model_watch._tier("claude-fable-5") == "fable"
    assert model_watch._tier("claude-mythos-5") == "mythos"
    assert model_watch._tier("claude-3-5-sonnet-20241022") is None, "old middle-tier naming is ignored"
    assert model_watch._tier("gpt-4o") is None
    print("  ok  tier parsing (current naming, ignores old 3.x and non-Claude)")


def test_vkey():
    assert model_watch._vkey("claude-sonnet-4-6") == (4, 6)
    assert model_watch._vkey("claude-sonnet-5") == (5, 0)
    assert model_watch._vkey("claude-haiku-4-5-20251001") == (4, 5), "8-digit date dropped"
    assert model_watch._vkey("claude-opus-4-8") == (4, 8)
    assert model_watch._vkey("claude-sonnet-5") > model_watch._vkey("claude-sonnet-4-6")
    print("  ok  version key (major, minor; drops the date snapshot)")


def test_detect_one_upgrade():
    """Sonnet has a newer release; Opus and Haiku are current -> exactly one upgrade."""
    models = [
        _m("claude-opus-4-8", 2026, 5, 1),
        _m("claude-sonnet-4-6", 2026, 2, 17),
        _m("claude-sonnet-5", 2026, 6, 30, "Claude Sonnet 5"),
        _m("claude-haiku-4-5-20251001", 2025, 10, 15),
    ]
    pins = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"}
    up, notes = model_watch.detect(models, pins)
    assert len(up) == 1, up
    assert up[0]["tier"] == "sonnet" and up[0]["old"] == "claude-sonnet-4-6" and up[0]["new"] == "claude-sonnet-5"
    assert not any(n.startswith("DEPRECATION") for n in notes)
    assert model_watch._tier("claude-sonnet-5") == "sonnet"
    print("  ok  detects the one in-tier upgrade (sonnet 4-6 -> 5), leaves current tiers alone")


def test_detect_no_upgrade_when_current():
    models = [
        _m("claude-opus-4-8", 2026, 5, 1),
        _m("claude-sonnet-4-6", 2026, 2, 17),
        _m("claude-haiku-4-5-20251001", 2025, 10, 15),
    ]
    pins = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"}
    up, _ = model_watch.detect(models, pins)
    assert up == [], "no newer model in any tier"
    print("  ok  no upgrade when every pin is the newest in its tier")


def test_alias_same_date_not_upgrade():
    """An alias of the pinned model (same created_at) is not a newer release."""
    models = [
        _m("claude-haiku-4-5-20251001", 2025, 10, 15),
        _m("claude-haiku-4-5", 2025, 10, 15, "Claude Haiku 4.5"),   # alias, same date
    ]
    pins = {"haiku": "claude-haiku-4-5-20251001"}
    up, _ = model_watch.detect(models, pins)
    assert up == [], "same created_at is the same model, not an upgrade"
    print("  ok  an alias with the same release date is not treated as an upgrade")


def test_canon():
    assert model_watch._canon("claude-haiku-4-5-20251001") == "claude-haiku-4-5", "date snapshot stripped"
    assert model_watch._canon("claude-haiku-4-5") == "claude-haiku-4-5", "undated id unchanged"
    assert model_watch._canon("claude-opus-4-8") == "claude-opus-4-8"
    assert model_watch._canon("claude-haiku-4-5-20251001") == model_watch._canon("claude-haiku-4-5")
    print("  ok  canonical id strips the -YYYYMMDD snapshot (undated alias == dated snapshot)")


def test_undated_pin_dated_listing_is_current():
    """The production scenario: the funnel pins the UNDATED alias (claude-haiku-4-5), but the Models
    API lists Haiku only under its DATED snapshot (claude-haiku-4-5-20251001). This must NOT read as
    a deprecation, and the snapshot must NOT read as an upgrade (bumping to it would re-pin the
    expiry the undated alias exists to avoid). Opus/Sonnet are listed undated and stay quiet."""
    models = [
        _m("claude-opus-4-8", 2026, 5, 1),
        _m("claude-sonnet-5", 2026, 6, 30),
        _m("claude-haiku-4-5-20251001", 2025, 10, 15),   # dated snapshot only; no undated alias listed
    ]
    pins = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
    up, notes = model_watch.detect(models, pins)
    assert not any(n.startswith("DEPRECATION") for n in notes), notes
    assert up == [], "the dated snapshot of the pinned model is not an upgrade: %r" % up
    print("  ok  an undated pin matched only by its dated snapshot is current (no false deprecation/upgrade)")


def test_undated_pin_real_upgrade_still_fires():
    """The canon match must not mask a genuine new version: pin claude-haiku-4-5, a newer
    claude-haiku-5 appears -> that IS an upgrade (different canonical id)."""
    models = [
        _m("claude-haiku-4-5-20251001", 2025, 10, 15),
        _m("claude-haiku-5", 2026, 6, 1, "Claude Haiku 5"),
    ]
    up, notes = model_watch.detect(models, {"haiku": "claude-haiku-4-5"})
    assert len(up) == 1 and up[0]["new"] == "claude-haiku-5", up
    assert not any(n.startswith("DEPRECATION") for n in notes), notes
    print("  ok  a genuinely newer version still fires for an undated pin")


def test_higher_tier_reported_not_proposed():
    models = [
        _m("claude-opus-4-8", 2026, 5, 1),
        _m("claude-fable-5", 2026, 6, 9, "Claude Fable 5"),
        _m("claude-mythos-5", 2026, 6, 9),
    ]
    pins = {"opus": "claude-opus-4-8"}
    up, notes = model_watch.detect(models, pins)
    assert up == [], "a tier above Opus is never an automatic upgrade"
    assert any("above Opus" in n for n in notes), notes
    print("  ok  a higher tier (Fable/Mythos) is reported, never auto-proposed")


def test_deprecation_note_and_replacement():
    """Pinned id no longer offered -> a deprecation note, and the newest in-tier as its replacement."""
    models = [
        _m("claude-sonnet-5", 2026, 6, 30, "Claude Sonnet 5"),   # 4-6 retired, not listed
    ]
    pins = {"sonnet": "claude-sonnet-4-6"}
    up, notes = model_watch.detect(models, pins)
    assert any(n.startswith("DEPRECATION") for n in notes), notes
    assert len(up) == 1 and up[0]["new"] == "claude-sonnet-5", up
    print("  ok  flags a retired pin and proposes the newest in-tier as replacement")


def test_version_fallback_when_no_dates():
    """With created_at absent, recency falls back to the parsed version number."""
    models = [
        {"id": "claude-sonnet-4-6", "display_name": "", "dt": None},
        {"id": "claude-sonnet-5", "display_name": "", "dt": None},
    ]
    pins = {"sonnet": "claude-sonnet-4-6"}
    up, _ = model_watch.detect(models, pins)
    assert len(up) == 1 and up[0]["new"] == "claude-sonnet-5", "version 5 > 4.6 by the fallback"
    print("  ok  version-number fallback when the API omits created_at")


def test_bump_text():
    src = (
        "TRIAGE_MODEL = os.environ.get(\"OPINIONS_TRIAGE_MODEL\", \"claude-sonnet-4-6\")\n"
        "# tier 2 default is claude-sonnet-4-6 until a newer Sonnet ships\n"
        "SCREEN_MODEL = \"claude-haiku-4-5-20251001\"  # leave this one alone\n"
    )
    up = [{"old": "claude-sonnet-4-6", "new": "claude-sonnet-5"}]
    out, n = model_watch._bump_text(src, up)
    assert n == 2, "both sonnet occurrences rewritten"
    assert "claude-sonnet-5" in out and "claude-sonnet-4-6" not in out
    assert "claude-haiku-4-5-20251001" in out, "an unrelated tier is untouched"
    print("  ok  pin rewrite replaces only the targeted id, counts occurrences")


def test_bump_text_whole_ids_only():
    """The new id usually extends the old one, so the plain substring replace this used to be
    also rewrote the old id inside any LONGER id already in the file: bumping claude-opus-5 to
    claude-opus-5-5 turned an existing claude-opus-5-5 into claude-opus-5-5-5."""
    src = (
        "MODEL = os.environ.get(\"OPINIONS_MODEL\", \"claude-opus-5\")\n"
        "# the audit already runs claude-opus-5-5; claude-opus-5-20260101 is a dated snapshot\n"
        "# the card writer defaults to claude-opus-5.\n"
    )
    up = [{"old": "claude-opus-5", "new": "claude-opus-5-5"}]
    out, n = model_watch._bump_text(src, up)
    assert "claude-opus-5-5-5" not in out, out
    assert n == 2, "only the two whole occurrences count, not the prefix of a longer id: %d" % n
    assert "\"claude-opus-5-5\")" in out, "the pin itself moved"
    assert "claude-opus-5-20260101" in out, "a dated snapshot is a different id; left alone"
    assert "defaults to claude-opus-5-5.\n" in out, "a sentence-ending period is still a boundary"
    again, n2 = model_watch._bump_text(out, up)
    assert (again, n2) == (out, 0), "a second pass finds nothing to rewrite"
    print("  ok  pin rewrite moves whole ids only (no claude-opus-5-5-5 from a prefix match)")


WORKFLOW = os.path.join(os.path.dirname(HERE), ".github", "workflows", "model-watch.yml")


def _workflow_steps():
    """model-watch.yml's steps, or None when pyyaml is not installed."""
    try:
        import yaml
    except ImportError:                                # pragma: no cover
        return None
    doc = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    return [st for job in (doc.get("jobs") or {}).values() for st in (job.get("steps") or [])]


def _run_step(name, outputs, rcs=None, memos=None):
    """Run one model-watch.yml step's shell as Actions does (bash -eo pipefail), with each
    ``${{ steps.X.outputs.Y }}`` filled from outputs["X.Y"] and a stub `python` on PATH that
    exits rcs[mode] (default 0) for `python scripts/golden_check.py <mode>` and, as the real one
    does for a verdict (exit 0 or 1), writes memo_<mode>=memos[mode] (default "miss") to the step
    output. Everything lands in a temp dir. Returns {rc, out, body, summary, calls}, or None
    without pyyaml."""
    steps = _workflow_steps()
    if steps is None:
        return None
    run = next(st["run"] for st in steps if st.get("name") == name)
    run = re.sub(r"\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}",
                 lambda m: outputs["%s.%s" % (m.group(1), m.group(2))], run)
    assert "${{" not in run, "an expression in %r was left unfilled" % name
    with tempfile.TemporaryDirectory() as d:
        p = {k: os.path.join(d, k) for k in ("out", "body", "summary", "calls", "args", "step.sh")}
        for k in ("out", "body", "summary", "calls", "args"):
            open(p[k], "w").close()
        with open(p["step.sh"], "w") as f:
            f.write(run)
        stub = os.path.join(d, "python")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$2" >> "$CALLS"\necho "$*" >> "$ARGS"\n'
                    'case "$2" in check|summarize) ;; *) exit 0 ;; esac\n'
                    'eval "rc=\\${RC_$2:-0}"\n'
                    'eval "memo=\\${MEMO_$2:-miss}"\n'
                    'if [ "$rc" -le 1 ]; then echo "memo_$2=$memo" >> "$GITHUB_OUTPUT"; fi\n'
                    'exit "$rc"\n')
        os.chmod(stub, 0o755)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""),
                   GITHUB_OUTPUT=p["out"], BODY=p["body"], GITHUB_STEP_SUMMARY=p["summary"],
                   CALLS=p["calls"], ARGS=p["args"])
        env.update({"RC_" + mode: str(rc) for mode, rc in (rcs or {}).items()})
        env.update({"MEMO_" + mode: m for mode, m in (memos or {}).items()})
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", p["step.sh"]],
                           cwd=d, env=env, capture_output=True, text=True)
        got = {k: open(p[k], encoding="utf-8").read() for k in ("out", "body", "summary")}
        got["calls"] = open(p["calls"], encoding="utf-8").read().split()
        got["args"] = open(p["args"], encoding="utf-8").read().splitlines()
    got["rc"] = r.returncode
    got["log"] = r.stdout + r.stderr
    return got


def test_add_paths_match_pin_files():
    """The bump PR commits only add-paths, so a PIN_FILES entry missing from it is rewritten on
    the runner, runs in the golden eval, and is then left out of the PR: the silent half-bump
    PIN_FILES exists to prevent, one layer down. add-paths still named the six workflow files
    PIN_FILES dropped on 2026-08-18 and none of the four watch scripts it gained on 2026-08-19."""
    steps = _workflow_steps()
    if steps is None:
        print("  ..  pyyaml not available; skipping the add-paths check")
        return
    pr = [st for st in steps if "create-pull-request" in str(st.get("uses", ""))]
    assert len(pr) == 1, "expected one create-pull-request step, found %d" % len(pr)
    paths = [ln.strip() for ln in str((pr[0].get("with") or {}).get("add-paths", "")).splitlines()
             if ln.strip()]
    assert len(paths) == len(set(paths)), "add-paths names a file twice: %s" % paths
    assert set(paths) == set(model_watch.PIN_FILES), \
        "add-paths %s != PIN_FILES %s" % (sorted(paths), sorted(model_watch.PIN_FILES))
    print("  ok  the bump PR's add-paths is exactly PIN_FILES (%d files)" % len(paths))


def test_eval_step_reads_only_exit_1_as_regression():
    """golden_check exits 1 on a regression and 3 on a ConfigError (bad key, no credit, retired
    model). The eval step read ANY nonzero exit as "regressed", so a credit outage was reported as
    a regression on every golden case -- and since the failure reporter skips a regressed run, the
    outage never reached the tracking issue. Only 1 may set regressed=true; any other code must
    fail the step so the reporter runs."""
    step = "Golden-set check against the candidate"
    both = {"watch.run_check": "true", "watch.run_summarize": "true"}
    r = _run_step(step, both)
    if r is None:
        print("  ..  pyyaml not available; skipping the eval-step check")
        return
    assert r["rc"] == 0 and "regressed=false" in r["out"], r
    assert r["calls"] == ["check", "summarize"] and "REGRESSION" not in r["body"], r

    for mode in ("check", "summarize"):
        r = _run_step(step, both, {mode: 1})
        assert r["rc"] == 0, "a regression is captured, not fatal, so the PR still opens: %r" % r
        assert "regressed=true" in r["out"] and "REGRESSION" in r["body"], r
        r = _run_step(step, both, {mode: 3})
        assert r["rc"] != 0, "a ConfigError from %s must fail the step: %r" % (mode, r)
        assert "regressed=true" not in r["out"], "a broken run is not a regression: %r" % r

    r = _run_step(step, both, {"check": 3})
    assert r["calls"] == ["check"], "a broken check stops before summarize: %r" % r["calls"]
    r = _run_step(step, both, {"check": 1, "summarize": 3})
    assert r["rc"] != 0 and "regressed=true" not in r["out"], \
        "a regression followed by a broken run is still a broken run: %r" % r

    for mode in ("check", "summarize"):
        r = _run_step(step, both, {mode: 4})
        assert r["rc"] == 4 and "regressed=true" not in r["out"], \
            "an inconclusive %s (exit 4) is an infrastructure failure, not a regression: %r" % (mode, r)
        assert "inconclusive" in r["log"] and "not a regression" in r["log"], r["log"]
        assert "inconclusive" in r["body"] and "REGRESSION" not in r["body"], r["body"]
    print("  ok  the eval step reads only exit 1 as a regression; any other code fails the step")


def test_only_a_new_regression_fails_the_run():
    """The run failed red every day from 2026-09-23 on the same remembered candidate. A regression
    the memo already holds was reported the first time; only a NEW one (memo miss) may fail."""
    step = "Golden-set check against the candidate"
    both = {"watch.run_check": "true", "watch.run_summarize": "true"}
    r = _run_step(step, both, {"summarize": 1}, {"check": "hit", "summarize": "miss"})
    if r is None:
        print("  ..  pyyaml not available; skipping the fresh-regression check")
        return
    assert "regressed=true" in r["out"] and "fresh_regression=true" in r["out"], r
    assert "(new verdict)" in r["body"] and "(remembered verdict)" in r["body"], r["body"]
    r = _run_step(step, both, {"summarize": 1}, {"check": "hit", "summarize": "hit"})
    assert "regressed=true" in r["out"] and "fresh_regression=false" in r["out"], r
    r = _run_step(step, both, {"check": 1}, {"check": "hit", "summarize": "miss"})
    assert "fresh_regression=false" in r["out"], "a fresh PASS beside a remembered regression: %r" % r
    for st in _workflow_steps():
        if "golden_check.py" in (st.get("run") or ""):
            for line in st["run"].splitlines():
                if "golden_check.py" in line:
                    assert "--memo" in line, "the eval must run memoized: %r" % line
    print("  ok  only a new (unremembered) regression fails the run; the eval always runs memoized")


def test_step_order_reports_after_the_failure():
    """The tracking-issue step must run after the fail step (so run_watchdog sees a report
    follow the failure) and before the memo commit (so `reported` is persisted); the failure
    reporter must stand down only when a new regression was actually reported and recorded."""
    steps = _workflow_steps()
    if steps is None:
        print("  ..  pyyaml not available; skipping the step-order check")
        return
    names = [st.get("name") for st in steps]
    i_fail = names.index("Fail on a new regression")
    i_issue = names.index("Report the candidate verdict on the tracking issue")
    i_rec = names.index("Record the golden verdicts on main")
    i_rep = names.index("Report a failed run")
    assert i_fail < i_issue < i_rec < i_rep, names
    by = {st.get("name"): st for st in steps}
    assert str(by[names[i_issue]].get("if", "")).startswith("always()"), "the report must run after the failure"
    assert str(by[names[i_rec]].get("if", "")).startswith("always()"), "the memo must persist after the failure"
    assert by[names[i_fail]].get("if") == "steps.eval.outputs.fresh_regression == 'true'"
    assert by["Report a failed run"].get("if") == (
        "failure() && (steps.regression.outcome != 'failure' || steps.issue.outcome != 'success' "
        "|| steps.record.outcome == 'failure')"), by["Report a failed run"].get("if")
    rec = by[names[i_rec]]["run"]
    assert "git worktree add" in rec and "PUSH_MAIN_REGENERATE" in rec and "model_watch_state.json" in rec
    import golden_check
    assert os.path.basename(golden_check.MEMO_STATE_PATH) == "model_watch_state.json"
    print("  ok  verdict report follows the failure, precedes the memo commit; reporter excludes only a reported regression")


def test_no_pat_note_follows_the_result():
    """With no PAT the run summary is the only report, and it said the model "was validated
    against the golden set" even when it had just regressed."""
    step = "Note when no PAT is configured"
    ok = _run_step(step, {"eval.regressed": "false"})
    if ok is None:
        print("  ..  pyyaml not available; skipping the no-PAT note check")
        return
    bad = _run_step(step, {"eval.regressed": "true"})
    assert ok["rc"] == 0 and bad["rc"] == 0, (ok, bad)
    assert "passed the golden set" in ok["summary"] and "REGRESSED" not in ok["summary"], ok
    assert "REGRESSED" in bad["summary"] and "passed" not in bad["summary"], bad
    print("  ok  the no-PAT summary reports the golden-set result it actually got")


def test_parse_dt():
    assert model_watch._parse_dt("2026-06-30T12:00:00Z") is not None
    assert model_watch._parse_dt("2026-06-30T12:00:00+00:00") is not None
    assert model_watch._parse_dt("") is None
    assert model_watch._parse_dt("not-a-date") is None
    print("  ok  created_at parsing (Z and offset forms, bad input -> None)")


def _pin_literals(path, ids):
    """The ids in `ids` that appear in `path` as a BARE string literal -- the only form a bump
    has to rewrite. Parsing rather than grepping is what makes the reverse check below usable:
    prose names a model inside a longer sentence (a docstring line, a `# comment`), and a
    comment is not in the AST at all while a docstring's value is the whole paragraph, never
    the bare id. So this sees `os.environ.get("ETHICS_MODEL", "claude-opus-5")` and does not
    see the sentence explaining what that default is."""
    import ast as _ast
    tree = _ast.parse(open(path, encoding="utf-8").read())
    return {n.value for n in _ast.walk(tree)
            if isinstance(n, _ast.Constant) and isinstance(n.value, str) and n.value in ids}


def test_pin_files_all_actually_hold_a_pin():
    """PIN_FILES is the list a bump rewrites. Both directions of it can rot, and both have.

    FORWARD -- an entry that no longer contains a model id is a silent no-op: the bump reports
    success while leaving that file untouched, and the list stops describing where the pins
    live. It drifted exactly that way. The list carried six workflow files because each
    restated its tier's pin as `${{ vars.X || 'claude-...' }}`; those 25 restatements were
    removed on 2026-08-18 and the entries became dead weight -- while the comment above them
    still explained that a bump edits the workflow fallbacks, and model-watch.yml still asked
    for a PAT with Workflows scope to do it.

    REVERSE -- a file holding a bare literal of a pinned id but NOT listed gets left on the old
    id by a bump, which is worse than not bumping: half the repo moves and nothing errors. Four
    watch scripts were in exactly that state when this test was written (see PIN_FILES).

    The reverse direction checks only the ids TIER_PINS actually manages, and only where they
    appear as bare literals. A watch pinning some other tier (claude-fable-5) is not a bump
    target and is not flagged; neither is prose that names an id in a sentence."""
    import re as _re
    import glob as _glob
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pin_re = _re.compile(r"claude-(?:opus|sonnet|haiku|fable|mythos)-[0-9a-z-]+")

    for rel in model_watch.PIN_FILES:
        path = os.path.join(root, rel)
        assert os.path.exists(path), "PIN_FILES names a file that does not exist: %s" % rel
        assert pin_re.search(open(path, encoding="utf-8").read()), \
            "PIN_FILES entry holds no model pin (dead entry): %s" % rel

    managed = set(model_watch.TIER_PINS.values())
    assert managed, "TIER_PINS resolved empty; the reverse check below would pass vacuously"
    listed = set(model_watch.PIN_FILES)

    stray = []
    for path in sorted(_glob.glob(os.path.join(root, "scripts", "*.py"))):
        rel = os.path.relpath(path, root)
        base = os.path.basename(rel)
        if rel in listed or base.startswith(("test_", "stress_")):
            continue    # fixtures carry ids on purpose and are not rewrite targets
        found = _pin_literals(path, managed)
        if found:
            stray.append("%s %s" % (rel, sorted(found)))
    assert not stray, ("a currently pinned id is a bare literal outside PIN_FILES, so a bump "
                       "would rewrite some pins and leave these on the old id: %s" % stray)

    # Workflows must hold NO pin at all. The 25 `|| 'claude-...'` fallbacks removed on
    # 2026-08-18 could each silently outrank the repo via a UI Variable and severed the
    # inheritances the scripts build; this fails if one is ever reintroduced, and it is also
    # what lets model-watch.yml run without `workflow` scope on its token.
    wf = [os.path.relpath(p, root)
          for p in sorted(_glob.glob(os.path.join(root, ".github", "workflows", "*.yml")))
          if pin_re.search(open(p, encoding="utf-8").read())]
    assert not wf, ("a workflow restates a model pin again; keep pins in the scripts so the "
                    "inheritance holds and no repo Variable can outrank them: %s" % wf)

    print("  ok  PIN_FILES matches where the pins actually live (%d files, both directions; "
          "no workflow holds a pin)" % len(model_watch.PIN_FILES))


def _caps(supported, **levels):
    """A Models-API capabilities object with the given effort support."""
    eff = {"supported": supported}
    eff.update({k: {"supported": v} for k, v in levels.items()})
    return {"effort": eff}


def test_effort_conflicts():
    """The effort rule in update.py is checked against the Models API's own capabilities before
    anything is judged: either disagreement makes the comparison unfair or the request a 400."""
    pins = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
    base = [dict(_m("claude-opus-5", 2026, 5, 1), caps=_caps(True, high=True)),
            dict(_m("claude-sonnet-5", 2026, 6, 1), caps=_caps(True, high=True)),
            dict(_m("claude-haiku-4-5", 2025, 10, 1), caps=_caps(False))]
    assert model_watch.effort_conflicts(base, [], pins) == [], "the current pins agree with the rule"
    cand = dict(_m("claude-opus-5-5", 2026, 9, 1), caps=_caps(True, high=True))
    up = [{"tier": "opus", "old": "claude-opus-5", "new": "claude-opus-5-5"}]
    assert model_watch.effort_conflicts(base + [cand], up, pins) == [], "a documented candidate agrees"
    # A model the documented rule does not cover (Sonnet 5.5 here) is never extrapolated to: the
    # API decides, and when the API is silent too, NO effort is sent and the run warns.
    saved_caps = dict(update._MODEL_CAPS)
    try:
        no_caps = dict(_m("claude-sonnet-5-5", 2026, 9, 29), caps=None)
        up2 = [{"tier": "sonnet", "old": "claude-sonnet-5", "new": "claude-sonnet-5-5"}]
        assert model_watch.effort_conflicts(base + [no_caps], up2, pins) == [], \
            "undocumented + no capabilities is not a conflict (nothing to disagree with)"
        w = model_watch.effort_unconfirmed(base + [no_caps], up2, pins)
        assert len(w) == 1 and "claude-sonnet-5-5" in w[0] and "unmatched" in w[0], w
        assert update.effort_params("triage", "claude-sonnet-5-5") == {}, \
            "undocumented and unconfirmed by the API: no effort is sent"
        says_no = dict(_m("claude-sonnet-5-5", 2026, 9, 29), caps=_caps(True, high=False))
        assert model_watch.effort_conflicts(base + [says_no], up2, pins) == [], "the API decides"
        assert update.effort_params("triage", "claude-sonnet-5-5") == {}, "the API said no: none sent"
        says_yes55 = dict(_m("claude-sonnet-5-5", 2026, 9, 29), caps=_caps(True, high=True))
        assert model_watch.effort_conflicts(base + [says_yes55], up2, pins) == []
        assert model_watch.effort_unconfirmed(base + [says_yes55], up2, pins) == []
        assert update.effort_params("triage", "claude-sonnet-5-5") == {"output_config": {"effort": "high"}}, \
            "the API confirmed it: the candidate runs at the incumbent's effort"
        # A documented "no" that the API contradicts is a conflict (the rule is out of date) ...
        old_sonnet = dict(_m("claude-sonnet-4-5", 2025, 9, 29), caps=_caps(True, high=True))
        up3 = [{"tier": "sonnet", "old": "claude-sonnet-5", "new": "claude-sonnet-4-5"}]
        c = model_watch.effort_conflicts(base + [old_sonnet], up3, pins)
        assert len(c) == 1 and "says no" in c[0] and "API says yes" in c[0], c
        # ... and so is a documented "yes" the API denies.
        denied = [dict(base[0], caps=_caps(True, high=False))] + base[1:]
        c = model_watch.effort_conflicts(denied, [], pins)
        assert len(c) == 1 and "claude-opus-5" in c[0] and "summarize" in c[0] and "says yes" in c[0], c
    finally:
        update._MODEL_CAPS.clear()
        update._MODEL_CAPS.update(saved_caps)
    haiku_eff = dict(_m("claude-haiku-5", 2026, 9, 29), caps=_caps(True, high=True))
    up4 = [{"tier": "haiku", "old": "claude-haiku-4-5", "new": "claude-haiku-5"}]
    assert model_watch.effort_conflicts(base + [haiku_eff], up4, pins) == [], \
        "the Haiku tiers configure no effort, so there is nothing to disagree about"
    assert model_watch._api_effort(None, "high") is None
    assert model_watch._api_effort({"effort": {"supported": True}}, "high") is True
    print("  ok  the effort rule is cross-checked against the Models API in both directions")


class _GH:
    """A stub `gh`: records each call and answers `issue list` / `issue create`."""
    def __init__(self, open_issues=()):
        self.calls, self.open = [], list(open_issues)

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            import json as _json
            return _json.dumps(self.open)
        if args[:2] == ["issue", "create"]:
            self.open.append({"number": 77, "title": args[args.index("--title") + 1]})
            return "https://github.com/o/r/issues/77\n"
        return ""

    def posts(self):
        return [c for c in self.calls if c[:2] in (["issue", "create"], ["issue", "comment"])]


def _memo_with(path, entries):
    import golden_check
    golden_check.save_memo({"verdicts": dict(entries)}, path)


def _fake_keys(monkey):
    """Make golden_check.memo_key return a fixed key per mode, restoring afterwards."""
    import golden_check
    saved = golden_check.memo_key
    golden_check.memo_key = lambda mode: (monkey[mode], {"mode": mode})
    return lambda: setattr(golden_check, "memo_key", saved)


def _entry(mode, verdict, failures=()):
    return {"mode": mode, "models": {"summarize": "claude-opus-5-5"} if mode == "summarize"
            else {"screen": "claude-haiku-4-5", "triage": "claude-sonnet-5-5"},
            "effort": {"summarize": "high"} if mode == "summarize" else {"screen": "", "triage": "high"},
            "verdict": verdict, "ok": 13, "failures": list(failures), "uncached": [],
            "evaluated": "2026-10-02", "run": "", "reported": ""}


def test_report_issue_posts_once():
    """One tracking issue, one post per verdict: a remembered verdict that was already reported
    is not posted again (the daily loop this replaces), a new verdict comments on the open issue,
    and with none open it opens one."""
    import golden_check
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "memo.json")
        _memo_with(path, {"k1": _entry("summarize", "regression", ["Cannon: missing auto after 4 tries"])})
        restore = _fake_keys({"summarize": "k1", "check": "k2"})
        try:
            gh = _GH()
            assert model_watch.report_issue(["summarize"], gh, path) == 0
            assert len(gh.posts()) == 1 and gh.posts()[0][:2] == ["issue", "create"], gh.calls
            create = gh.posts()[0]
            assert create[create.index("--title") + 1] == model_watch.ISSUE_TITLE
            body = create[create.index("--body") + 1]
            assert "REGRESSED" in body and "Cannon" in body and "claude-opus-5-5" in body, body
            assert "effort `high`" in body, body
            assert golden_check.load_memo(path)["verdicts"]["k1"]["reported"] == "#77"

            again = _GH(open_issues=[{"number": 77, "title": model_watch.ISSUE_TITLE}])
            assert model_watch.report_issue(["summarize"], again, path) == 0
            assert again.calls == [], "a reported verdict makes no gh call at all: %r" % again.calls

            # A new verdict (new key: a prompt, effort or golden edit) comments on the open issue,
            # matched on the EXACT title, not on a search hit with a similar one.
            data = golden_check.load_memo(path)
            data["verdicts"]["k2"] = _entry("check", "pass")
            golden_check.save_memo(data, path)
            gh3 = _GH(open_issues=[{"number": 5, "title": "Model watch: candidate model evaluation (old)"},
                                   {"number": 77, "title": model_watch.ISSUE_TITLE}])
            assert model_watch.report_issue(["check", "summarize"], gh3, path) == 0
            posts = gh3.posts()
            assert len(posts) == 1 and posts[0][:3] == ["issue", "comment", "77"], gh3.calls
            v = golden_check.load_memo(path)["verdicts"]
            assert v["k1"]["reported"] == "#77" and v["k2"]["reported"] == "#77", v

            gh4 = _GH()
            assert model_watch.report_issue(["", "recall"], gh4, path) == 0 and gh4.calls == [], \
                "a mode with no memo (or none at all) reports nothing"
        finally:
            restore()

        # No verdict in the memo for the current keys (the eval broke before judging): no post.
        restore = _fake_keys({"summarize": "missing", "check": "missing2"})
        try:
            gh5 = _GH()
            assert model_watch.report_issue(["summarize"], gh5, path) == 0 and gh5.calls == []
        finally:
            restore()
    print("  ok  the tracking issue gets one post per verdict (create, then comment; never repeat)")


def test_report_issue_text():
    ok = model_watch.issue_text([("check", "k2", _entry("check", "pass"))])
    assert "passed the golden set" in ok and "REGRESS" not in ok and "MODEL_WATCH_TOKEN" in ok, ok
    assert "screen `claude-haiku-4-5` (no effort parameter)" in ok, ok
    assert "<!-- model-watch keys: k2 -->" in ok
    bad = model_watch.issue_text([("summarize", "k1", _entry("summarize", "regression",
                                                             ["Giles v. Greenhouse: missing negsec"]))],
                                 pr_url="https://github.com/o/r/pull/9")
    assert "REGRESSED" in bad and "FAIL Giles v. Greenhouse: missing negsec" in bad, bad
    assert "https://github.com/o/r/pull/9" in bad
    print("  ok  the issue text names the models, the effort, the verdict and every failing case")


def test_issue_step_passes_the_eval_keys():
    """The issue step names the verdicts by the keys golden_check reported, not by recomputing
    them from a working tree the PR step may have touched; and report_issue honours them."""
    step = "Report the candidate verdict on the tracking issue"
    r = _run_step(step, {"eval.key_check": "", "eval.key_summarize": "abc123"})
    if r is None:
        print("  ..  pyyaml not available; skipping the issue-step check")
        return
    assert r["rc"] == 0 and r["args"] == ["scripts/model_watch.py --report-issue summarize=abc123"], r
    r = _run_step(step, {"eval.key_check": "k2", "eval.key_summarize": "k1"})
    assert r["args"] == ["scripts/model_watch.py --report-issue check=k2,summarize=k1"], r
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "memo.json")
        _memo_with(path, {"k1": _entry("summarize", "regression", ["Cannon: missing auto"])})
        restore = _fake_keys({"summarize": "not-the-key", "check": "nope"})
        try:
            gh = _GH()
            model_watch.report_issue(["summarize=k1"], gh, path)
            assert len(gh.posts()) == 1, "an explicit key is used as given: %r" % gh.calls
        finally:
            restore()
    print("  ok  the issue step reports the verdicts by the keys the eval step produced")


def test_close_issue():
    gh = _GH(open_issues=[{"number": 77, "title": model_watch.ISSUE_TITLE}])
    assert model_watch.close_issue(gh) == 0
    assert ["issue", "close", "77"] in gh.calls, gh.calls
    none = _GH()
    assert model_watch.close_issue(none) == 0 and none.posts() == [] and \
        not any(c[:2] == ["issue", "close"] for c in none.calls)
    print("  ok  all pins current closes the tracking issue, and is a no-op when none is open")


TESTS = [test_tier, test_vkey, test_canon, test_detect_one_upgrade, test_detect_no_upgrade_when_current,
         test_alias_same_date_not_upgrade, test_undated_pin_dated_listing_is_current,
         test_undated_pin_real_upgrade_still_fires, test_higher_tier_reported_not_proposed,
         test_deprecation_note_and_replacement, test_version_fallback_when_no_dates,
         test_bump_text, test_bump_text_whole_ids_only, test_parse_dt,
         test_pin_files_all_actually_hold_a_pin, test_add_paths_match_pin_files,
         test_eval_step_reads_only_exit_1_as_regression, test_no_pat_note_follows_the_result,
         test_only_a_new_regression_fails_the_run, test_step_order_reports_after_the_failure,
         test_effort_conflicts, test_report_issue_posts_once, test_report_issue_text,
         test_issue_step_passes_the_eval_keys, test_close_issue]


def main():
    print("model_watch detection + rewrite:")
    for t in TESTS:
        t()
    print("\nALL TESTS PASSED (%d cases)" % len(TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
