#!/usr/bin/env python3
"""Model-watch: a Dependabot for the funnel's Claude model pins.

The Georgia Appellate Watch funnel pins a model per tier (Opus summarizer, Sonnet
triage, Haiku screen). From the 4.6 generation onward an Anthropic model id is a
fixed snapshot, not an evergreen pointer: a newer model ships under a NEW id
(claude-sonnet-5, say) and the old id keeps serving the old weights. So a pin never
drifts on its own, and it never upgrades on its own either. This watches the Models
API for a newer model in each tier's own family and, when one appears, rewrites the
pin and (via the workflow) opens a review PR, the same shape as Dependabot: it
proposes, the golden set checks, a human merges. It never deploys on its own.

In one run:
  1. List every currently available model from the Anthropic Models API.
  2. Read the funnel's current pins from update.py (the Opus/Sonnet/Haiku tier reps;
     audit reuses the summarizer, crosscheck/completeness and treatment reuse Sonnet,
     and pretriage reuses Haiku, so these three ids cover every pin).
  3. For each of those three tiers, find the newest model in the SAME family (by the
     API's created_at) and, if it is strictly newer than the pin, record an upgrade.
     A tier above Opus (Fable/Mythos) is a deliberate human choice: reported, never
     auto-proposed.
  4. With --apply, rewrite the old id to the new id everywhere it is pinned in the
     repo (the update.py / treatment.py defaults and the ``|| 'id'`` fallbacks in the
     funnel workflows) so the eval below tests the candidate and a merged PR actually
     takes effect, and write a PR body.
  5. Flag any pinned id the API no longer lists (a retired model the funnel would fail
     on), so a deprecation is caught before a run breaks rather than after.

The workflow runs this, then runs golden_check against the bumped pin (update.py reads
the model from its now-edited default, so the eval exercises the candidate), appends
the result to the PR body, and opens the PR. The golden set is the real gate: whether
the new model still keeps and drops the right cases on our own opinions, not merely
that a newer version exists.

  python scripts/model_watch.py            # detect and report only (no edits)
  python scripts/model_watch.py --apply    # detect, rewrite the pins, write the PR body

Needs ANTHROPIC_API_KEY (read via update.py). Pure standard library otherwise.

Outputs (written to $GITHUB_OUTPUT when present, for the workflow):
  upgrade        true if at least one tier has a newer model
  run_check      true if a screen/pretriage/triage tier (Haiku/Sonnet) changed
  run_summarize  true if the summarizer tier (Opus) changed
  body_path      path to the written PR body markdown
"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # the funnel's pins (MODEL/TRIAGE_MODEL/SCREEN_MODEL), repo root, and API auth (KEY, VERSION)
import safeio  # crash-safe writes for the files this rewrites and the PR body

API = "https://api.anthropic.com/v1/models"

# The three tier representatives the funnel pins, read from update.py's resolved
# defaults. Evaluated at import; detect() compares the API's newest-in-tier against
# these, so a bump is staged only after this snapshot of the current pins is taken.
TIER_PINS = {
    "opus":   update.MODEL,         # tier 3 summarizer (and treatment audit)
    "sonnet": update.TRIAGE_MODEL,  # tier 2 triage (and crosscheck/completeness, treatment classifier)
    "haiku":  update.SCREEN_MODEL,  # tier 1 screen (and pretriage)
}

# Every place a model id is pinned. A bump rewrites the old id to the new one in all
# of them so a merged PR is complete: the source defaults AND each workflow's
# ``|| 'id'`` fallback, the value that actually runs when no repo Variable overrides.
# Editing a workflow file needs a token with `workflow` scope (see model-watch.yml).
PIN_FILES = [
    "scripts/update.py",
    "scripts/treatment.py",
    ".github/workflows/opinions.yml",
    ".github/workflows/backfill.yml",
    ".github/workflows/queue.yml",
    ".github/workflows/golden-check.yml",
    ".github/workflows/treatment.yml",
    ".github/workflows/maintain.yml",
]

TIER_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)\b")
HIGHER_TIERS = ("fable", "mythos")   # above the three funnel tiers; reported, never auto-proposed


def _tier(model_id):
    m = TIER_RE.match(model_id or "")
    return m.group(1) if m else None


def _vkey(model_id):
    """(major, minor) parsed from an id, ignoring any 8-digit date snapshot, as a
    fallback recency signal when the API omits created_at. claude-sonnet-4-6 -> (4, 6);
    claude-sonnet-5 -> (5, 0); claude-haiku-4-5-20251001 -> (4, 5)."""
    nums = [int(n) for n in re.findall(r"\d+", model_id or "") if len(n) <= 3]  # <=3 digits drops the date
    return (nums[0] if nums else 0, nums[1] if len(nums) > 1 else 0)


_DATE_SNAPSHOT = re.compile(r"-\d{8}$")


def _canon(model_id):
    """The model id with any trailing ``-YYYYMMDD`` snapshot stripped, so an undated alias and its
    dated snapshot compare equal: both ``claude-haiku-4-5`` and ``claude-haiku-4-5-20251001`` ->
    ``claude-haiku-4-5``. The funnel pins the undated alias on purpose (no snapshot-retirement
    expiry), but the Models API may list only the dated snapshot; matching on the canonical form
    keeps that from reading as a deprecation, or the snapshot from reading as an upgrade."""
    return _DATE_SNAPSHOT.sub("", model_id or "")


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _api_get(url):
    req = urllib.request.Request(url, headers={
        "x-api-key": update.KEY, "anthropic-version": update.VERSION,
        "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def list_models():
    """Every available model from the Models API, paginated. Each entry is
    {id, display_name, dt}, dt the parsed created_at (or None)."""
    out, after = [], None
    for _ in range(20):  # generous page cap; the catalog is well under this
        url = API + "?limit=100" + (("&after_id=" + after) if after else "")
        data = _api_get(url)
        for m in data.get("data", []):
            out.append({"id": m.get("id"), "display_name": m.get("display_name") or "",
                        "dt": _parse_dt(m.get("created_at"))})
        if not data.get("has_more"):
            break
        after = data.get("last_id")
        if not after:
            break
    return out


def _newest_in_tier(models, tier):
    fam = [m for m in models if _tier(m["id"]) == tier]
    if not fam:
        return None
    if all(m["dt"] for m in fam):
        return max(fam, key=lambda m: m["dt"])
    return max(fam, key=lambda m: _vkey(m["id"]))


def _is_newer(cand, pinned_entry, pinned_id):
    """Strictly newer than the pin? created_at when both have it, else parsed version.
    A None pinned_entry means the pinned id is no longer offered, so the newest in its
    tier is its replacement."""
    if pinned_entry is None:
        return True
    if cand["dt"] and pinned_entry["dt"]:
        return cand["dt"] > pinned_entry["dt"]
    return _vkey(cand["id"]) > _vkey(pinned_id)


def detect(models, pins=None):
    """Return (upgrades, notes). upgrades is a per-tier dict for any tier with a newer
    model; notes are human-readable lines for deprecations and the higher tier."""
    pins = pins or TIER_PINS
    by_id = {m["id"]: m for m in models}
    upgrades, notes = [], []
    for tier, pinned_id in pins.items():
        pin_canon = _canon(pinned_id)
        # The pin is "still offered" if the API lists the same model under ANY snapshot form. The
        # funnel pins the undated alias (e.g. claude-haiku-4-5), but the Models API may list only its
        # dated snapshot (claude-haiku-4-5-20251001); match on the date-stripped id so an undated pin
        # is not misread as deprecated. Use the exact entry for its created_at when present, else any
        # same-canon listing (so the recency compare below still has a date to work with).
        same = [m for m in models if _canon(m["id"]) == pin_canon]
        pinned_entry = by_id.get(pinned_id) or (same[0] if same else None)
        if not same:
            notes.append("DEPRECATION: pinned %s model %r is no longer listed by the API; "
                         "the funnel will fail on it. Migrate." % (tier, pinned_id))
        newest = _newest_in_tier(models, tier)
        # Compare canonical ids: a dated snapshot of the SAME model the pin already names is not an
        # upgrade (bumping to it would re-introduce the snapshot expiry the undated pin exists to
        # avoid); only a genuinely different version is.
        if newest and _canon(newest["id"]) != pin_canon and _is_newer(newest, pinned_entry, pinned_id):
            upgrades.append({"tier": tier, "old": pinned_id, "new": newest["id"],
                             "old_dt": pinned_entry["dt"] if pinned_entry else None,
                             "new_dt": newest["dt"], "display": newest["display_name"]})
    higher = sorted({m["id"] for m in models if _tier(m["id"]) in HIGHER_TIERS})
    if higher:
        notes.append("A tier above Opus is available (%s). Adopting it is a deliberate choice "
                     "(different price, request routing, and data-retention terms), so it is "
                     "reported here, never auto-proposed." % ", ".join(higher))
    return upgrades, notes


def _bump_text(text, upgrades):
    """Replace each old id with its new id in text. Returns (new_text, occurrences).
    Pure, so it is unit-tested directly; apply_bumps wraps it with file I/O."""
    n = 0
    for up in upgrades:
        c = text.count(up["old"])
        if c:
            text = text.replace(up["old"], up["new"])
            n += c
    return text, n


def apply_bumps(upgrades):
    """Rewrite each old id to its new id across PIN_FILES. Returns [(relpath, count)]
    for the run summary. Exact-string replace scoped to the allowlist, so only the pins
    move; nothing else in those files is touched."""
    changed = []
    for rel in PIN_FILES:
        path = os.path.join(update.REPO, rel)
        if not os.path.exists(path):
            continue
        new_text, n = _bump_text(open(path, encoding="utf-8").read(), upgrades)
        if n:
            safeio.atomic_write_text(path, new_text)
            changed.append((rel, n))
    return changed


def _fmt_dt(dt):
    return dt.date().isoformat() if dt else "unknown date"


def write_report(upgrades, notes, changed, path):
    """The PR body. The workflow's eval step appends its golden-set result below this."""
    lines = ["## Model update", ""]
    if upgrades:
        lines.append("A newer model is available in %s:"
                     % ("one tier" if len(upgrades) == 1 else "%d tiers" % len(upgrades)))
        lines.append("")
        for up in upgrades:
            lines.append("- **%s**: `%s` -> `%s` (%s, released %s; current pin released %s)"
                         % (up["tier"], up["old"], up["new"], up["display"] or up["new"],
                            _fmt_dt(up["new_dt"]), _fmt_dt(up["old_dt"])))
        lines += ["", "Pins rewritten in:"]
        lines += ["- `%s` (%d occurrence%s)" % (r, n, "" if n == 1 else "s") for r, n in changed]
    else:
        lines.append("No tier has a newer model. All pins are current.")
    if notes:
        lines += ["", "### Notes", ""] + ["- " + n for n in notes]
    lines += ["",
              "Do not merge on the strength of \"a newer model exists.\" Read the golden-set "
              "result below: whether the new model still keeps and drops the right cases (and, "
              "for the summarizer, still covers the right areas) on our own opinions. If a repo "
              "Variable overrides a pin, update that Variable to match.", ""]
    safeio.atomic_write_text(path, "\n".join(lines))


def _emit(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (key, value))


def main(argv):
    apply = "--apply" in argv
    if not update.KEY:
        print("model_watch: ANTHROPIC_API_KEY is not set; cannot query the Models API.")
        return 2
    try:
        models = list_models()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print("model_watch: Models API request failed: %s" % e)
        return 1
    print("model_watch: %d models listed by the API" % len(models))

    upgrades, notes = detect(models)
    for up in upgrades:
        print("  UPGRADE %-7s %s -> %s (released %s)"
              % (up["tier"], up["old"], up["new"], _fmt_dt(up["new_dt"])))
    for n in notes:
        print("  note: " + n)
    if not upgrades:
        print("  all tiers current")

    tiers = {up["tier"] for up in upgrades}
    body_path = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "model_watch_pr.md")
    if apply and upgrades:
        changed = apply_bumps(upgrades)
        write_report(upgrades, notes, changed, body_path)
        print("  applied: " + ", ".join("%s(%d)" % (r, n) for r, n in changed))

    _emit("upgrade", "true" if upgrades else "false")
    _emit("run_check", "true" if tiers & {"haiku", "sonnet"} else "false")
    _emit("run_summarize", "true" if "opus" in tiers else "false")
    _emit("body_path", body_path)

    # Run record: upgrades, plus any deprecation (urgent even on a no-upgrade day).
    deprecations = [n for n in notes if n.startswith("DEPRECATION")]
    if upgrades:
        head = "%d model upgrade%s available" % (len(upgrades), "" if len(upgrades) == 1 else "s")
        rows = "\n".join("- %s: `%s` -> `%s`" % (u["tier"], u["old"], u["new"]) for u in upgrades)
    else:
        head, rows = "All model pins current", ""
    extra = ("\n\n" + "\n".join("- " + d for d in deprecations)) if deprecations else ""
    safeio.step_summary("### Model watch\n\n%s\n\n%s%s" % (head, rows, extra))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
