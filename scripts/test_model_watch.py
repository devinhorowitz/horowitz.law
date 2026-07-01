#!/usr/bin/env python3
"""Self-tests for scripts/model_watch.py. Standard library only; no network, no API key.

Covers the detection logic that decides whether to open a model-bump PR: tier parsing,
the recency comparison (created_at, with a version-number fallback), the within-tier-only
rule, the higher-tier and deprecation notes, and the exact-string pin rewrite. All on
synthetic model lists built here, so the guard is pinned without touching the live API.

  python scripts/test_model_watch.py     # prints each case; exits nonzero on any failure
"""
import datetime
import os
import sys

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


def test_parse_dt():
    assert model_watch._parse_dt("2026-06-30T12:00:00Z") is not None
    assert model_watch._parse_dt("2026-06-30T12:00:00+00:00") is not None
    assert model_watch._parse_dt("") is None
    assert model_watch._parse_dt("not-a-date") is None
    print("  ok  created_at parsing (Z and offset forms, bad input -> None)")


TESTS = [test_tier, test_vkey, test_detect_one_upgrade, test_detect_no_upgrade_when_current,
         test_alias_same_date_not_upgrade, test_higher_tier_reported_not_proposed,
         test_deprecation_note_and_replacement, test_version_fallback_when_no_dates,
         test_bump_text, test_parse_dt]


def main():
    print("model_watch detection + rewrite:")
    for t in TESTS:
        t()
    print("\nALL TESTS PASSED (%d cases)" % len(TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
