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


def _run_step(name, outputs, rcs=None):
    """Run one model-watch.yml step's shell as Actions does (bash -eo pipefail), with each
    ``${{ steps.X.outputs.Y }}`` filled from outputs["X.Y"] and a stub `python` on PATH that
    exits rcs[mode] (default 0) for `python scripts/golden_check.py <mode>`. Everything lands in
    a temp dir. Returns {rc, out, body, summary, calls}, or None without pyyaml."""
    steps = _workflow_steps()
    if steps is None:
        return None
    run = next(st["run"] for st in steps if st.get("name") == name)
    run = re.sub(r"\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}",
                 lambda m: outputs["%s.%s" % (m.group(1), m.group(2))], run)
    assert "${{" not in run, "an expression in %r was left unfilled" % name
    with tempfile.TemporaryDirectory() as d:
        p = {k: os.path.join(d, k) for k in ("out", "body", "summary", "calls", "step.sh")}
        for k in ("out", "body", "summary", "calls"):
            open(p[k], "w").close()
        with open(p["step.sh"], "w") as f:
            f.write(run)
        stub = os.path.join(d, "python")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$2" >> "$CALLS"\neval "exit \\${RC_$2:-0}"\n')
        os.chmod(stub, 0o755)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""),
                   GITHUB_OUTPUT=p["out"], BODY=p["body"], GITHUB_STEP_SUMMARY=p["summary"],
                   CALLS=p["calls"])
        env.update({"RC_" + mode: str(rc) for mode, rc in (rcs or {}).items()})
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", p["step.sh"]],
                           cwd=d, env=env, capture_output=True, text=True)
        got = {k: open(p[k], encoding="utf-8").read() for k in ("out", "body", "summary")}
        got["calls"] = open(p["calls"], encoding="utf-8").read().split()
    got["rc"] = r.returncode
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

    report = next(st for st in _workflow_steps() if st.get("name") == "Report a failed run")
    assert report.get("if") == "failure() && steps.eval.outputs.regressed != 'true'", report.get("if")
    print("  ok  the eval step reads only exit 1 as a regression; any other code fails the step")


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


TESTS = [test_tier, test_vkey, test_canon, test_detect_one_upgrade, test_detect_no_upgrade_when_current,
         test_alias_same_date_not_upgrade, test_undated_pin_dated_listing_is_current,
         test_undated_pin_real_upgrade_still_fires, test_higher_tier_reported_not_proposed,
         test_deprecation_note_and_replacement, test_version_fallback_when_no_dates,
         test_bump_text, test_bump_text_whole_ids_only, test_parse_dt,
         test_pin_files_all_actually_hold_a_pin, test_add_paths_match_pin_files,
         test_eval_step_reads_only_exit_1_as_regression, test_no_pat_note_follows_the_result]


def main():
    print("model_watch detection + rewrite:")
    for t in TESTS:
        t()
    print("\nALL TESTS PASSED (%d cases)" % len(TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
