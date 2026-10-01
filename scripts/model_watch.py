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
     repo (the script defaults listed in PIN_FILES; the workflows no longer restate a
     pin as a ``|| 'id'`` fallback) so the eval below tests the candidate and a merged
     PR actually takes effect, and write a PR body.
  5. Flag any pinned id the API no longer lists (a retired model the funnel would fail
     on), so a deprecation is caught before a run breaks rather than after.

The workflow runs this, then runs golden_check against the bumped pin (update.py reads
the model from its now-edited default, so the eval exercises the candidate), appends
the result to the PR body, and opens the PR. The golden set is the real gate: whether
the new model still keeps and drops the right cases on our own opinions, not merely
that a newer version exists.

  python scripts/model_watch.py            # detect and report only (no edits)
  python scripts/model_watch.py --apply    # detect, rewrite the pins, write the PR body
  python scripts/model_watch.py --report-issue check,summarize   # file the verdict (workflow)
  python scripts/model_watch.py --close-issue                    # all pins current (workflow)

MATCHED EFFORT. A candidate is judged through update.py's own request builders, which send the
effort siteconfig.MODEL_EFFORT names for each tier, so the candidate and the incumbent run at the
same level rather than at their own API defaults (which differ across generations). Before
anything is bumped, every pin and candidate is checked against the Models API's own
capabilities.effort: if the documented rule (update.EFFORT_DOCUMENTED) covers a model and disagrees
with what the API says it accepts, the run stops as a broken run (exit 3) instead of evaluating an
unfair or 400-bound comparison. A model the docs do not cover (a newer generation) is sent effort
only when the API's capabilities.effort confirms it; when the API reports nothing either, it is
sent none and the run logs a warning that its comparison may be unmatched.

REMEMBERED VERDICTS AND THE TRACKING ISSUE. The golden check runs with --memo, so an unchanged
candidate is judged once, not daily (golden_check.py, THE MEMO). The verdict -- pass or
regression, with the failing cases -- goes to ONE tracking issue (ISSUE_TITLE), once per
verdict: a remembered verdict that has already been reported is not posted again.

Needs ANTHROPIC_API_KEY (read via update.py). Pure standard library otherwise.

Outputs (written to $GITHUB_OUTPUT when present, for the workflow):
  upgrade        true if at least one tier has a newer model
  run_check      true if a screen/pretriage/triage tier (Haiku/Sonnet) changed
  run_summarize  true if the summarizer tier (Opus) changed
  body_path      path to the written PR body markdown

Exit codes: 0 ok, 1 Models API error, 2 no key, 3 the effort rule disagrees with the Models API.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # the funnel's pins (MODEL/TRIAGE_MODEL/SCREEN_MODEL), repo root, and API auth (KEY, VERSION)
import safeio  # crash-safe writes for the files this rewrites and the PR body
import siteconfig  # MODEL_EFFORT, the per-tier effort the cross-check verifies

API = "https://api.anthropic.com/v1/models"

# The three tier representatives the funnel pins, read from update.py's resolved
# defaults. Evaluated at import; detect() compares the API's newest-in-tier against
# these, so a bump is staged only after this snapshot of the current pins is taken.
TIER_PINS = {
    "opus":   update.MODEL,         # tier 3 summarizer (and treatment audit)
    "sonnet": update.TRIAGE_MODEL,  # tier 2 triage (and crosscheck/completeness, treatment classifier)
    "haiku":  update.SCREEN_MODEL,  # tier 1 screen (and pretriage)
}

# Every place a model id is pinned. A bump rewrites the old id to the new one in all of
# them so a merged PR is complete.
#
# This list used to carry six workflow files too, because each one restated its tier's pin
# as a ``${{ vars.X || 'claude-...' }}`` fallback. Those 25 restatements were removed on
# 2026-08-18: a UI Variable could silently outrank the repo, and the duplicated literal
# severed inheritances the scripts had built (SMELL_MODEL from AUDIT_MODEL from MODEL).
# The pins now live only in the two source files below, so this list matches them --
# and, since nothing here edits .github/workflows any more, MODEL_WATCH_TOKEN no longer
# needs `workflow` scope.
#
# The four watch scripts below were added on 2026-08-19. They are not funnel stages, but each
# defaults to a tier id this file already manages -- courtrules/ethics pin claude-opus-5, and
# legislation/regulations pin claude-opus-5 for the card writer and claude-haiku-4-5 for the
# screen. They were written after this list was, and nobody added them to it, so an Opus bump
# would have moved update.py and treatment.py and left four watches running the old weights
# with nothing erroring: the repo half-bumped, silently, until someone happened to grep. A
# partial bump is worse than none, so they ride along. The bump PR shows the whole diff for a
# human to veto, and any watch that wants a different model still has its env override.
#
# Not listed on purpose: diagnose.py, dep_review.py and fable_review.py pin claude-fable-5, a
# tier above Opus that this script reports and never auto-proposes (HIGHER_TIERS), so a funnel
# bump has no business touching them.
#
# test_model_watch checks both directions -- every entry holds a pin, and no unlisted file holds
# a bare literal of a currently pinned id -- so this cannot quietly drift again in either sense.
PIN_FILES = [
    "scripts/update.py",
    "scripts/treatment.py",
    "scripts/courtrules.py",
    "scripts/ethics.py",
    "scripts/legislation.py",
    "scripts/regulations.py",
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
    {id, display_name, dt, caps}, dt the parsed created_at (or None) and caps the API's
    `capabilities` object (or None when the API omits it)."""
    out, after = [], None
    for _ in range(20):  # generous page cap; the catalog is well under this
        url = API + "?limit=100" + (("&after_id=" + after) if after else "")
        data = _api_get(url)
        for m in data.get("data", []):
            out.append({"id": m.get("id"), "display_name": m.get("display_name") or "",
                        "dt": _parse_dt(m.get("created_at")),
                        "caps": m.get("capabilities") if isinstance(m.get("capabilities"), dict) else None})
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


# What may follow an old id for it to be that whole id: not a word character, and not a '-' or '.'
# leading into one, either of which would make it the head of a LONGER id (a newer point release,
# a dated snapshot). A sentence-ending period is still a boundary.
_ID_END = r"(?![\w]|[-.]\w)"


def _bump_text(text, upgrades):
    """Replace each old id with its new id in text. Returns (new_text, occurrences).
    Pure, so it is unit-tested directly; apply_bumps wraps it with file I/O.

    Whole ids only, not a substring replace. A new id usually extends the old one
    (claude-opus-5 -> claude-opus-5-5), so a plain replace also hit the old id inside any longer
    id already in the text and turned claude-opus-5-5 into claude-opus-5-5-5."""
    n = 0
    for up in upgrades:
        text, c = re.subn(re.escape(up["old"]) + _ID_END, lambda _m, new=up["new"]: new, text)
        n += c
    return text, n


def apply_bumps(upgrades):
    """Rewrite each old id to its new id across PIN_FILES. Returns [(relpath, count)]
    for the run summary. Whole-id replace scoped to the allowlist, so only the pins
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


# Which request roles each watched tier serves, for the effort cross-check below.
TIER_ROLES = {"opus": ("summarize",), "sonnet": ("triage",), "haiku": ("screen", "pretriage")}


def _api_effort(caps, level):
    """What the Models API says about `level` effort (update.api_effort, kept here by name)."""
    return update.api_effort(caps, level)


def _effort_review(models, upgrades, pins=None):
    """(conflicts, unconfirmed) for every pin and candidate, at the level siteconfig.MODEL_EFFORT
    asks of its tier. Also hands each model's listed capabilities to update, so this process's
    effort decisions use what the API just reported rather than fetching it again.

    conflicts: the documented rule (update.effort_documented) covers the model and the Models API
      disagrees with it. Either direction: the rule says yes and the API no (production would 400),
      or the rule says no and the API yes (the model would run at its own default against one at a
      set level -- the unfair comparison this mechanism exists to remove).
    unconfirmed: the docs do not cover the model and the API reports no capabilities.effort, so no
      effort is sent to it and its comparison may be unmatched. Logged, not fatal: nothing confirms
      the parameter, and sending it unconfirmed risks a 400.
    Where the docs do not cover a model but the API does report, the API decides; nothing to flag."""
    pins = pins or TIER_PINS
    by_id = {m["id"]: m for m in models}
    ids = {(t, i) for t, i in pins.items()} | {(u["tier"], u["new"]) for u in upgrades}
    conflicts, unconfirmed = [], []
    for tier, mid in sorted(ids):
        entry = by_id.get(mid) or next((m for m in models if _canon(m["id"]) == _canon(mid)), None)
        if entry is not None:
            update.remember_capabilities(mid, entry.get("caps"))
        for role in TIER_ROLES.get(tier, ()):
            level = siteconfig.MODEL_EFFORT.get(role, "")
            if not level or entry is None:
                continue
            api = _api_effort(entry.get("caps"), level)
            doc = update.effort_documented(mid, level)
            if doc is None:
                if api is None:
                    unconfirmed.append("%s on %s: the documented rule does not cover this model and the "
                                       "Models API reports no capabilities.effort, so no effort is sent; "
                                       "it runs at its own default and the comparison at effort %r may be "
                                       "unmatched." % (role, mid, level))
                continue
            if api is not None and api != doc:
                conflicts.append("%s on %s: the documented rule (update.EFFORT_DOCUMENTED) says %s for "
                                 "effort %r but the Models API says %s. Fix the rule in scripts/update.py "
                                 "before this model is judged."
                                 % (role, mid, "yes" if doc else "no", level, "yes" if api else "no"))
    return conflicts, unconfirmed


def effort_conflicts(models, upgrades, pins=None):
    """The conflict lines of _effort_review: documented rule and Models API disagree."""
    return _effort_review(models, upgrades, pins)[0]


def effort_unconfirmed(models, upgrades, pins=None):
    """The unconfirmed lines of _effort_review: neither the docs nor the API confirm effort."""
    return _effort_review(models, upgrades, pins)[1]


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


# ---- The tracking issue -----------------------------------------------------------------------

ISSUE_TITLE = "Model watch: candidate model evaluation"
GH_RETRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh_retry.sh")


def _gh(args):
    """Run `gh` through gh_retry.sh against this repository; returns stdout, raises on failure."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    cmd = ["bash", GH_RETRY] + list(args) + (["--repo", repo] if repo else [])
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _verdicts_now(modes, memo_path=None):
    """[(mode, key, entry)] for the verdicts named by `modes`, from the memo. Each item is either
    "mode=key" (the key the eval step's golden_check reported, which the workflow passes so the
    lookup cannot depend on the working tree still holding the bump) or a bare mode, whose key is
    computed here from the pins this process reads."""
    import golden_check  # imported here: it pulls the golden set, which detection never needs
    data = golden_check.load_memo(memo_path)
    out = []
    for item in modes:
        mode, _, key = item.partition("=")
        if mode not in golden_check.MEMO_MODES:
            continue
        if not key:
            key, _meta = golden_check.memo_key(mode)
        entry = data["verdicts"].get(key)
        if entry:
            out.append((mode, key, entry))
    return data, out


def issue_text(verdicts, pr_url=""):
    """The tracking-issue post for one set of verdicts: which models were judged, at what effort,
    the result, and every failing case. Ends with a marker naming the memo keys."""
    regressed = any(e.get("verdict") == "regression" for _m, _k, e in verdicts)
    lines = ["## %s" % ("Candidate REGRESSED on the golden set" if regressed
                        else "Candidate passed the golden set"), ""]
    for mode, _key, e in verdicts:
        what = "screen/pretriage/triage" if mode == "check" else "summarizer"
        models = ", ".join("%s `%s`%s" % (r, m, (" at effort `%s`" % e["effort"][r])
                                          if (e.get("effort") or {}).get(r) else " (no effort parameter)")
                           for r, m in sorted((e.get("models") or {}).items()))
        lines.append("### `%s` (%s): %s" % (mode, what, "**REGRESSION**" if e.get("verdict") == "regression"
                                             else "pass"))
        lines.append("")
        lines.append("- Judged: %s" % models)
        lines.append("- %d case(s) ok; evaluated %s%s" % (e.get("ok", 0), e.get("evaluated", "?"),
                                                         (" ([run](%s))" % e["run"]) if e.get("run") else ""))
        for f in e.get("failures") or []:
            lines.append("- FAIL %s" % f)
        if e.get("uncached"):
            lines.append("- uncached (run `golden_check.py build`): %s" % ", ".join(e["uncached"]))
        lines.append("")
    if pr_url:
        lines.append("The bump PR: %s" % pr_url)
    elif regressed:
        lines.append("Do not apply this bump as-is. The verdict is remembered, so model-watch will "
                     "not spend on this candidate again until a model, prompt, effort setting or "
                     "the golden set changes; it stays quiet (and green) until then.")
    else:
        lines.append("No PR was opened (MODEL_WATCH_TOKEN is not set). Apply the bump by hand: "
                     "rewrite the old id to the new one in model_watch.PIN_FILES.")
    lines += ["", "<!-- model-watch keys: %s -->" % ",".join(k for _m, k, _e in verdicts)]
    return "\n".join(lines) + "\n"


def _open_issue(gh):
    """The open tracking issue's number, matched on the exact title, or ""."""
    raw = gh(["issue", "list", "--state", "open", "--search", 'in:title "%s"' % ISSUE_TITLE,
              "--json", "number,title"])
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        rows = []
    nums = [str(r.get("number")) for r in rows if isinstance(r, dict) and r.get("title") == ISSUE_TITLE]
    return min(nums, key=int) if nums else ""


def report_issue(modes, gh=_gh, memo_path=None, pr_url=""):
    """Post the current verdicts to the tracking issue unless they have been posted already.

    Dedupe is by the memo: a verdict carries `reported` once posted, and the workflow commits the
    memo, so the same candidate on the same prompts is posted once, not daily -- even if someone
    closes the issue meanwhile. A new verdict comments on the open issue, or opens one."""
    import golden_check
    data, verdicts = _verdicts_now(modes, memo_path)
    if not verdicts:
        print("model_watch: no remembered verdict for %s; nothing to report" % ",".join(modes))
        return 0
    if all(e.get("reported") for _m, _k, e in verdicts):
        print("model_watch: verdict already reported (%s); not posting again"
              % ", ".join(e["reported"] for _m, _k, e in verdicts))
        return 0
    body = issue_text(verdicts, pr_url)
    num = _open_issue(gh)
    if num:
        gh(["issue", "comment", num, "--body", body])
        where = "#%s" % num
    else:
        out = gh(["issue", "create", "--title", ISSUE_TITLE, "--body", body]).strip()
        m = re.search(r"/issues/(\d+)", out)
        where = ("#" + m.group(1)) if m else (out or "issue")
    for _m, _k, e in verdicts:
        e["reported"] = where
    golden_check.save_memo(data, memo_path)
    print("model_watch: verdict reported on %s" % where)
    return 0


def close_issue(gh=_gh):
    """No tier has a newer model (the bump was applied, or the candidate went away): close the
    tracking issue if one is open."""
    num = _open_issue(gh)
    if num:
        gh(["issue", "comment", num, "--body", "All model pins are current; closing automatically."])
        gh(["issue", "close", num])
        print("model_watch: closed #%s" % num)
    return 0


def _emit(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (key, value))


def main(argv):
    if "--report-issue" in argv:
        i = argv.index("--report-issue")
        modes = [m for m in (argv[i + 1] if i + 1 < len(argv) else "").split(",") if m]
        return report_issue(modes, pr_url=os.environ.get("PR_URL", ""))
    if "--close-issue" in argv:
        return close_issue()
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

    conflicts, unconfirmed = _effort_review(models, upgrades)
    for u in unconfirmed:
        print("::warning::effort unconfirmed: " + u)
    if unconfirmed:
        safeio.step_summary("### Model watch: effort unconfirmed\n\n"
                            + "\n".join("- " + u for u in unconfirmed))
    if conflicts:
        for c in conflicts:
            print("::error::effort rule out of date: " + c)
        safeio.step_summary("### Model watch\n\nNot evaluated: the effort rule disagrees with the "
                            "Models API.\n\n" + "\n".join("- " + c for c in conflicts))
        return 3

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
