#!/usr/bin/env python3
"""Hermetic unit tests for the drop-reason smell test (update.py tier 2.5 + smell_check.py).

Standard library only; no network and no API key. Stubs update.anthropic_json and batch.run the
same way test_update.py does. The smell test is the recall audit of triage-drop reasons: a wrong
parse here silently swallows the audit (fail-open), so these tests pin the request shape, the
verdict parsing, the fail-open defaults, the empty-reason rule, the escalation cap, and the retro
script's record selection.

Run directly: `python scripts/test_smell.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update       # noqa: E402  (sys.path shim must run first)
import batch        # noqa: E402
import smell_check  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


ITEMS = [
    {"name": "A v. B", "court": "ctapp", "date": "2026-07-01", "reason": "criminal appeal, out of scope"},
    {"name": "C v. D", "court": "ca11", "date": "2026-07-02", "reason": "Georgia duty-to-defend analysis"},
    {"name": "E v. F", "court": "scotga", "date": "2026-07-03", "reason": ""},
]


def test_prompt_shape():
    body = update.smell_request(ITEMS)
    check("request uses the smell model", body["model"] == update.SMELL_MODEL)
    check("request uses the tunable output budget", body["max_tokens"] == update.SMELL_TOKENS)
    check("the budget clears ~200 tokens per chunk item",
          update.SMELL_TOKENS >= update.SMELL_CHUNK * 200)
    user = body["messages"][0]["content"]
    check("items are numbered 1-based", "1. [ctapp 2026-07-01]" in user and "2. [ca11 2026-07-02]" in user)
    check("reason text reaches the prompt", "criminal appeal, out of scope" in user)
    check("an empty reason renders as (none given)", "REASON FOR THE DROP: (none given)" in user)
    check("smell system carries the triage criteria verbatim (no drift)",
          update.TRIAGE_CRITERIA in update.SMELL_SYSTEM)
    check("smell system demands the verdicts object", '"verdicts"' in update.SMELL_SYSTEM)


def _sync_stub(payload):
    """Stub anthropic_json to return `payload` and force the synchronous path."""
    update.SMELL_BATCH = False
    update.anthropic_json = lambda body, label=None: payload


def test_verdict_parsing():
    real_json, real_batch_run, real_smell_batch = update.anthropic_json, batch.run, update.SMELL_BATCH
    try:
        _sync_stub({"verdicts": [
            {"i": 1, "verdict": "ok", "note": ""},
            {"i": 2, "verdict": "suspect", "note": "keep-shaped label"},
            {"i": 99, "verdict": "suspect", "note": "out of range"},
            {"i": "x", "verdict": "suspect", "note": "bad index"},
            {"i": 3, "verdict": "banana", "note": "unknown verdict"},
        ]})
        out = update.smell_reasons(ITEMS)
        check("verdict 1 parses ok", out[0] == {"verdict": "ok", "note": ""})
        check("verdict 2 parses suspect with note", out[1] == {"verdict": "suspect", "note": "keep-shaped label"})
        check("out-of-range item number is ignored", all(k in (0, 1, 2) for k in out))
        check("an unknown verdict coerces to ok (fail-open)", out[2]["verdict"] == "ok")

        _sync_stub({"verdicts": []})
        check("empty verdicts list -> empty map", update.smell_reasons(ITEMS) == {})
        _sync_stub({"nonsense": True})
        check("missing verdicts key -> empty map", update.smell_reasons(ITEMS) == {})

        # First verdict for an item wins; a duplicate row cannot flip it.
        _sync_stub({"verdicts": [{"i": 1, "verdict": "ok"}, {"i": 1, "verdict": "suspect"}]})
        check("duplicate item number keeps the first verdict",
              update.smell_reasons(ITEMS)[0]["verdict"] == "ok")
    finally:
        update.anthropic_json, batch.run, update.SMELL_BATCH = real_json, real_batch_run, real_smell_batch


def test_batch_path_and_fallback():
    real_json, real_batch_run, real_smell_batch = update.anthropic_json, batch.run, update.SMELL_BATCH
    try:
        update.SMELL_BATCH = True
        payload = json.dumps({"verdicts": [{"i": 2, "verdict": "suspect", "note": "topic label"}]})
        calls = {"batch": 0, "sync": 0}

        def fake_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            calls["batch"] += 1
            check("batch gets exactly one request", len(reqs) == 1)
            check("batch request is keyed 'smell-0'", reqs[0]["custom_id"] == "smell-0")
            return {"smell-0": {"ok": True, "text": payload}}
        batch.run = fake_run
        update.anthropic_json = lambda body, label=None: calls.__setitem__("sync", calls["sync"] + 1) or {}
        out = update.smell_reasons(ITEMS)
        check("batch path parses the verdict", out.get(1, {}).get("verdict") == "suspect")
        check("batch path never calls the sync API", calls["sync"] == 0)

        def broken_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            raise batch.BatchError("boom")
        batch.run = broken_run
        update.anthropic_json = lambda body, label=None: {"verdicts": [{"i": 1, "verdict": "suspect", "note": "n"}]}
        out = update.smell_reasons(ITEMS)
        check("batch error falls back to the synchronous call", out.get(0, {}).get("verdict") == "suspect")

        def failed_line_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            return {"smell-0": {"ok": False, "type": "errored", "error": "x"}}
        batch.run = failed_line_run
        out = update.smell_reasons(ITEMS)
        check("a failed batch line falls back to the synchronous call",
              out.get(0, {}).get("verdict") == "suspect")
    finally:
        update.anthropic_json, batch.run, update.SMELL_BATCH = real_json, real_batch_run, real_smell_batch


def test_select():
    drops = [{"reason": "criminal, out of scope"}, {"reason": "keep-shaped"}, {"reason": "  "},
             {"reason": "another suspect"}, {"reason": "yet another"}]
    verdicts = {0: {"verdict": "ok", "note": ""}, 1: {"verdict": "suspect", "note": "label"},
                3: {"verdict": "suspect", "note": "s2"}, 4: {"verdict": "suspect", "note": "s3"}}
    esc, annot = update.smell_select(drops, verdicts, cap=10)
    check("ok verdict stays ok", annot[0]["verdict"] == "ok")
    check("empty reason is suspect without a model verdict",
          annot[2] == {"verdict": "suspect", "note": "no reason recorded"})
    check("suspects escalate in order", esc == [1, 2, 3, 4])
    esc_capped, _ = update.smell_select(drops, verdicts, cap=2)
    check("cap bounds the escalations", esc_capped == [1, 2])
    esc_zero, annot_zero = update.smell_select(drops, verdicts, cap=0)
    check("cap 0 escalates nothing but still annotates", esc_zero == [] and len(annot_zero) == 5)
    _, annot_missing = update.smell_select([{"reason": "something"}], {})
    check("an un-judged drop gets NO annotation (never a fabricated 'ok')", 0 not in annot_missing)
    esc_none, annot_none = update.smell_select(drops, {})
    check("with no verdicts at all, only the empty-reason drop is annotated",
          list(annot_none) == [2] and esc_none == [2])


def test_chunking():
    real_json, real_batch_run, real_smell_batch = update.anthropic_json, batch.run, update.SMELL_BATCH
    try:
        many = [{"name": "N%d" % i, "court": "ctapp", "date": "2026-07-01", "reason": "r%d" % i}
                for i in range(update.SMELL_CHUNK * 2 + 5)]
        update.SMELL_BATCH = True

        def fake_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            check("chunked batch: one request per SMELL_CHUNK slice", len(reqs) == 3)
            check("chunk custom_ids are smell-<k>",
                  [r["custom_id"] for r in reqs] == ["smell-0", "smell-1", "smell-2"])
            # chunk 1 judges its second item (global index SMELL_CHUNK+1); chunk 2's line fails
            return {"smell-0": {"ok": True, "text": json.dumps({"verdicts": []})},
                    "smell-1": {"ok": True, "text": json.dumps(
                        {"verdicts": [{"i": 2, "verdict": "suspect", "note": "x"}]})},
                    "smell-2": {"ok": False, "type": "errored", "error": "boom"}}
        sync_calls = []

        def fake_sync(body, label=None):
            sync_calls.append(body)
            return {"verdicts": [{"i": 1, "verdict": "suspect", "note": "fallback"}]}
        batch.run, update.anthropic_json = fake_run, fake_sync
        out = update.smell_reasons(many)
        check("chunk-local item numbers map to global indices",
              out.get(update.SMELL_CHUNK + 1, {}).get("verdict") == "suspect")
        check("only the failed chunk falls back to a synchronous call", len(sync_calls) == 1)
        check("the fallback chunk's verdict lands at its global index",
              out.get(update.SMELL_CHUNK * 2, {}).get("note") == "fallback")

        def broken_sync(body, label=None):
            raise RuntimeError("api down")
        def dead_run(reqs, deadline=None, interval=20.0, label="batch", **_kw):
            raise batch.BatchError("dead")
        batch.run, update.anthropic_json = dead_run, broken_sync
        check("total failure returns an EMPTY map (nothing judged, nothing invented)",
              update.smell_reasons(ITEMS) == {})

        def cfg_sync(body, label=None):
            raise update.ConfigError("bad model")
        update.anthropic_json = cfg_sync
        try:
            update.smell_reasons(ITEMS)
            check("ConfigError propagates out of smell_reasons", False)
        except update.ConfigError:
            check("ConfigError propagates out of smell_reasons", True)
    finally:
        update.anthropic_json, batch.run, update.SMELL_BATCH = real_json, real_batch_run, real_smell_batch


def test_retro_selection():
    recs = [
        {"stage": "screen", "reason": "criminal"},
        {"stage": "triage", "reason": "topic label"},
        {"stage": "triage", "reason": "already audited", "smell": "ok"},
        {"stage": "pretriage", "reason": "immigration"},
        {"stage": "triage", "reason": "newest"},
    ]
    lines = [json.dumps(r) for r in recs] + ["", "not json {{{"]
    picked, parsed = smell_check.select_records(lines, stages=["triage"], all_flag=False, limit=10)
    check("retro picks only un-audited triage records",
          [r["reason"] for r in picked] == ["topic label", "newest"])
    check("corrupt and blank lines are dropped, valid ones parsed", len(parsed) == 5)
    check("selected records alias the parsed list (annotation reaches the rewrite)",
          picked[0] is parsed[1])
    picked_all, _ = smell_check.select_records(lines, stages=["triage"], all_flag=True, limit=10)
    check("SMELL_ALL re-audits annotated records", len(picked_all) == 3)
    picked_lim, _ = smell_check.select_records(lines, stages=["triage"], all_flag=True, limit=2)
    check("limit keeps the most recent records",
          [r["reason"] for r in picked_lim] == ["already audited", "newest"])
    ql = smell_check.queue_line({"cluster_id": 123, "name": "A v. B", "court": "ca11",
                                 "date": "2026-07-01"}, "keep-shaped")
    check("queue line is a forced bare cluster id", ql.startswith("123 !  # smell: keep-shaped"))
    deferred = [json.dumps({"stage": "triage", "reason": "x", "smell": "suspect",
                            "smell_outcome": "deferred"})]
    picked_def, _ = smell_check.select_records(deferred, stages=["triage"], all_flag=False, limit=10)
    check("a deferred in-run escalation is re-audited by the retro pass", len(picked_def) == 1)


def test_retro_persistence():
    import tempfile
    real = (update.REJECT_PATH, update.KEY, update.SMELL_MODEL, smell_check.CHUNK,
            smell_check.OUT, smell_check.DRY_RUN, update.smell_reasons)
    tmp = tempfile.mkdtemp(prefix="smelltest")
    try:
        update.REJECT_PATH = os.path.join(tmp, "rej.jsonl")
        smell_check.OUT = os.path.join(tmp, "suspects.md")
        update.KEY, update.SMELL_MODEL = "test-key", "test-model"
        smell_check.CHUNK, smell_check.DRY_RUN = 2, False
        recs = [{"ts": "t", "stage": "triage", "cluster_id": 100 + i, "name": "Case %d" % i,
                 "court": "ctapp", "docket": "", "date": "2026-07-01", "url": "",
                 "reason": "reason %d" % i} for i in range(4)]
        with open(update.REJECT_PATH, "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")

        calls = [0]
        def scripted(items, deadline=None):
            calls[0] += 1
            if calls[0] == 1:   # chunk 1: one suspect, one ok
                return {0: {"verdict": "suspect", "note": "keep-shaped"},
                        1: {"verdict": "ok", "note": ""}}
            raise RuntimeError("api died mid-run")   # chunk 2: transport failure
        update.smell_reasons = scripted
        rc = smell_check.main()
        check("retro run survives a mid-run failure (exit 0)", rc == 0)
        lines = [json.loads(l) for l in open(update.REJECT_PATH) if l.strip()]
        check("chunk 1's verdicts persisted despite the later failure",
              lines[0].get("smell") == "suspect" and lines[1].get("smell") == "ok")
        check("failed chunk's records stay un-audited",
              "smell" not in lines[2] and "smell" not in lines[3])
        check("suspect carries the review outcome", lines[0].get("smell_outcome") == "review")
        check("suspects report exists with the queue line",
              os.path.exists(smell_check.OUT) and "100 !" in open(smell_check.OUT).read())
    finally:
        (update.REJECT_PATH, update.KEY, update.SMELL_MODEL, smell_check.CHUNK,
         smell_check.OUT, smell_check.DRY_RUN, update.smell_reasons) = real


def test_stage_config():
    """Screen drops were the blindest and the only unwatched gate: of 1,502 logged rejections the
    1,163 screen drops had never had a reason checked, because this audit read "triage" alone on
    the premise that screen reasons are category labels and so safe by construction. Twelve
    "In re: A v. B" captions dropped on the prefix alone disproved it.

    Two things are pinned. The stage list must come from the CONFIG FILE, not a hardcoded default
    or a GitHub Variable, with the env var demoted to a one-run override; and the audit must treat
    a reason the case NAME contradicts as suspect, which is what actually catches these -- the
    reasons name a recognized disqualifier ("dependency or juvenile proceeding") and are only
    detectable as wrong against a caption naming State Farm as a party."""
    import importlib, siteconfig
    check("screen is audited by default", "screen" in siteconfig.SMELL_STAGES)
    check("triage is still audited", "triage" in siteconfig.SMELL_STAGES)
    check("the default comes from the config file, not a literal in the script",
          'os.environ.get("SMELL_STAGES", "triage")' not in open(
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "smell_check.py"), encoding="utf-8").read())

    saved = os.environ.get("SMELL_STAGES")
    try:
        os.environ["SMELL_STAGES"] = "pretriage"
        mod = importlib.reload(importlib.import_module("smell_check"))
        check("the env var still overrides for one run", mod.STAGES == ["pretriage"],
              "got %r" % (mod.STAGES,))
        os.environ.pop("SMELL_STAGES")
        mod = importlib.reload(importlib.import_module("smell_check"))
        check("and falls back to the config file when unset",
              mod.STAGES == list(siteconfig.SMELL_STAGES), "got %r" % (mod.STAGES,))
    finally:
        if saved is None:
            os.environ.pop("SMELL_STAGES", None)
        else:
            os.environ["SMELL_STAGES"] = saved
        importlib.reload(importlib.import_module("smell_check"))

    sysp = update.SMELL_SYSTEM
    check("a reason contradicted by the caption is suspect", "contradicted by the case" in sysp)
    # ...but NOT a criminal one. The caption-contradiction rule names juvenile/dependency/probate;
    # the model generalized it to "criminal" and flagged Blount v. Columbia County (Ga. Ct. App.,
    # 2026-07-23), whose screen reason "Criminal case - DUI conviction review" was exactly right.
    # Georgia traffic and DUI appeals from probate court reach the Court of Appeals captioned
    # against the county, so a government-entity caption does not refute a criminal reason. That was
    # the first FALSE SUSPECT in 26 verified drops -- the audit guessing from the caption, which is
    # the very failure it exists to catch.
    check("a criminal reason is exempt from the caption-contradiction rule",
          "A CRIMINAL reason is the" in sysp)
    check("and the carve-out names the county-caption shape that produced the false positive",
          "v. <County>" in sysp)
    check("the wrapper prefixes are named as carrying no subject",
          "'In re'" in sysp and "Ex parte" in sysp)
    check("the request header no longer claims every drop came from triage",
          "CASES DROPPED AT TRIAGE" not in update.smell_request(ITEMS)["messages"][0]["content"])


def test_model_resolution():
    """The audit model had three homes: a GitHub Variable, a literal in two workflows, and the
    chain in update.py. The workflows always set OPINIONS_SMELL_MODEL, so the chain's fallback
    never ran -- repinning the tier-3 summarizer would have left this audit on the old model
    silently and forever, because the inheritance it was designed around (smell <- audit <-
    summarizer) was severed by a YAML literal.

    Pinned here: the value lives in the config file, "" still means inherit so a summarizer
    repin carries, an explicit id pins it independently, "off" disables it, and the env var is
    demoted to a one-run override."""
    import importlib, siteconfig
    check("the config file is where the value lives", hasattr(siteconfig, "SMELL_MODEL"))
    check('"" means inherit rather than disable', siteconfig.SMELL_MODEL == ""
          and update.SMELL_MODEL == update.AUDIT_MODEL)
    check("and the audit model inherits the summarizer pin",
          update.AUDIT_MODEL == update.MODEL)

    for wf in ("smell.yml", "opinions.yml"):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".github", "workflows", wf)
        check("%s no longer pins the audit model from a repo Variable" % wf,
              "OPINIONS_SMELL_MODEL" not in open(path, encoding="utf-8").read())

    saved = {k: os.environ.get(k) for k in ("OPINIONS_MODEL", "OPINIONS_SMELL_MODEL")}
    try:
        os.environ.pop("OPINIONS_SMELL_MODEL", None)
        os.environ["OPINIONS_MODEL"] = "test-summarizer-pin"
        mod = importlib.reload(importlib.import_module("update"))
        check("a summarizer repin now carries to the audit",
              mod.SMELL_MODEL == "test-summarizer-pin", "got %r" % mod.SMELL_MODEL)
        os.environ["OPINIONS_SMELL_MODEL"] = "test-one-run-override"
        mod = importlib.reload(importlib.import_module("update"))
        check("the env var still overrides for one run",
              mod.SMELL_MODEL == "test-one-run-override", "got %r" % mod.SMELL_MODEL)
        os.environ["OPINIONS_SMELL_MODEL"] = "off"
        mod = importlib.reload(importlib.import_module("update"))
        check("'off' is still the kill switch", mod.SMELL_MODEL == "")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(importlib.import_module("update"))


def test_reclamation_exposure():
    """The audit is the repo's most reclamation-exposed job, and both ways it could lose finished
    work are pinned here.

    On 2026-08-24 run 32738308741 was killed at 18.4 minutes -- inside both the 20-minute soft
    budget and the 30-minute job timeout -- by hosted-runner reclamation. The old budget comment
    claimed being under the watchdog meant "progress persists, never a kill", which is false:
    reclamation is not a timeout and respects neither limit. It cost that run's model spend and a
    week until the next Monday cron, though no data (select_records re-picks anything un-stamped).

    1. BUDGET bounds the exposure. The probe reads reclamation as a hazard that scales with
       exposure rather than a duration cliff, so a shorter run is hit less often AND forfeits less
       when it is hit. Pinned at most 900 so a revert to 1200 fails here.
    2. The workflow must commit and file suspects on always(). smell_check rewrites the log after
       every chunk so partial progress survives a crash -- but the default if: success() threw
       that away, and a bare `if` expression carries an implicit success() too, which is why the
       suspects step needed always() explicitly and not just its hashFiles guard. Neither rescues
       a reclaimed runner (every remaining step is stamped `skipped`); that is what BUDGET is for.
    """
    import importlib
    mod = importlib.import_module("smell_check")
    check("run budget is bounded against reclamation exposure", mod.BUDGET <= 900,
          "got %r" % mod.BUDGET)
    check("budget is not back at the value that was killed", mod.BUDGET != 1200)
    check("budget is still env-overridable for a watched drain",
          'os.environ.get("SMELL_BUDGET_SEC"' in open(
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "smell_check.py"),
              encoding="utf-8").read())

    wf = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".github", "workflows", "smell.yml"), encoding="utf-8").read()
    commit = wf.split("- name: Commit annotated log")[1].split("- name:")[0]
    check("the commit step runs on always(), so a failed audit keeps finished chunks",
          "if: always()" in commit)
    suspects = wf.split("- name: Surface suspect drops")[1].split("- name:")[0]
    check("the suspects step defeats the implicit success() on its bare if",
          "always()" in suspects)



# The fifteen drop reasons issue #293 reported, verbatim, paired with whether the screen prompt's
# own hedge rule condemns them. Every one was read through the full opinion on 2026-08-29 and every
# one was a CORRECT drop -- which is exactly why the lint must stay an audit signal and never gate
# the funnel. Kept as a corpus so a widened HEDGE_RE that starts flagging committed reasons (or a
# narrowed one that stops flagging these six) fails here instead of in a weekly report.
HEDGE_CORPUS = [
    # Commits to a category, then names its marker -- the good shape, even though the marker it
    # cites is not actually in this caption (the parties are "A. P. v. Department of Children and
    # Families"). Fabricated evidence is a real defect and this lint cannot see it; that is the
    # audit's job, not the regex's.
    ("Dependency/child welfare case - 'In the Interest of' caption indicates juvenile dependency proceeding", False),
    ("Domestic/family matter (name-based parties, likely divorce or personal dispute)", True),
    ("Captioned 'In re:' - likely dependency/family/domestic matter", True),
    ("Likely landlord-tenant or property dispute involving residential parties; minimal detail suggests dispossessory or eviction context", True),
    ("Likely family/domestic case (individual names suggest personal dispute, 'In re' caption format)", True),
    ("Family/domestic case (LT case number format indicates domestic relation; names suggest spousal dispute)", True),
    ("Probate/estate matter ('In re' caption with individual name)", False),
    ("Prisoner civil rights case involving DOC defendant", False),
    ("Criminal case - DUI conviction review", False),
    ("Dependency/child welfare case ('In the Interest of' minor); juvenile matter", False),
    ("Family/domestic case - private dispute between named individuals", False),
    ("Probate/estate administration matter", False),
    ("Civil forfeiture case from Florida supplementary court; outside Georgia focus", False),
    ("Election/political challenge to Governor; out-of-area Florida case", False),
    ("Family/domestic matter involving natural guardianship of minor; supplementary Florida state court", False),
]


def test_hedge_lint():
    for reason, want in HEDGE_CORPUS:
        got = bool(update.hedged_reason(reason))
        check("hedge %s: %s" % ("flags" if want else "clears", reason[:44]), got == want,
              "got %r" % (update.hedged_reason(reason),))
    check("five of the fifteen #293 reasons hedge",
          sum(1 for r, _ in HEDGE_CORPUS if update.hedged_reason(r)) == 5)
    # The 137 explanatory indicat* reasons in the live log are the reason indicat* is not linted.
    # If it ever comes back, these fail and the report goes back to being 75% noise.
    for good in ("Family/domestic case (FC designation indicates family court)",
                 "Criminal case - State v. defendant format indicates prosecution",
                 "Landlord-tenant (LT case number indicates dispossessory)"):
        check("explanatory 'indicates' is not a hedge: %s" % good[:40], not update.hedged_reason(good))
    # A disjunction of categories is the shape the prompt now calls out by name.
    check("category disjunction still caught",
          bool(update.hedged_reason("Matter involving individual (likely probate, domestic, or bar discipline)")))
    # "probable cause" is a holding, not a hedge: the lint must not fire on legal vocabulary that
    # merely shares a stem with one, or every forfeiture and Fourth Amendment drop reads as guessing.
    check("probable cause is not a hedge",
          not update.hedged_reason("Forfeiture reversed: no probable cause for the seizure"))
    check("possible-cause prose is not a hedge",
          not update.hedged_reason("Sanctions affirmed; no possible prejudice shown"))
    check("empty reason is not a hedge", update.hedged_reason("") == [])
    check("None reason is not a hedge", update.hedged_reason(None) == [])
    check("hedges are deduped and sorted",
          update.hedged_reason("Likely X; likely Y; suggests Z") == ["likely", "suggests"])


def test_report_headlines_firm_not_provisional():
    """A provisional finding is a record whose excerpt was discarded, so the lint checks the quote
    against a haystack NARROWER than the model saw. Three were checked against the opinions and all
    three were TRUE markers -- 'DR' in "LT Case No. 27-2020-DR-2233" where the record keeps only the
    appellate number, 'FC' in "Lower Tribunal No. 24-16088-FC-04" where the stored docket truncates
    it, and 'Executor' in a caption CourtListener's case_name shortens.

    So the headline must count FIRM findings only. Summing firm+provisional announced 47 defects
    where one was demonstrable, which is how a lint teaches its reader to discount it."""
    firm = {"cluster_id": 1, "name": "A v. B", "court": "ctapp", "date": "2026-08-31", "docket": "",
            "reason": "juvenile ('In the Interest of')", "evidence": "nothing of the sort",
            "unsupported_quote": ["In the Interest of"]}
    prov = [{"cluster_id": 100 + i, "name": "C v. D", "court": "dcafl", "date": "2026-07-01",
             "docket": "5D2025-2554", "reason": "family/domestic ('DR' case number)",
             "unsupported_quote": ["DR"]} for i in range(5)]
    import tempfile
    saved = smell_check.OUT
    try:
        with tempfile.TemporaryDirectory() as d:
            smell_check.OUT = os.path.join(d, "r.md")
            smell_check.write_report([], 0, [], [firm] + prov)
            out = open(smell_check.OUT, encoding="utf-8").read()
    finally:
        smell_check.OUT = saved
    check("headline counts firm only", "## Unsupported quoted markers: 1 firm" in out,
          out.split("## Unsupported quoted markers")[1][:60] if "## Unsupported" in out else out[:80])
    check("the combined total is not the headline",
          "Unsupported quoted markers: 6" not in out)
    check("provisional are filed as unverifiable, not as findings",
          "5 unverifiable (not findings)" in out)
    check("the report says why they cannot be checked", "narrower haystack" in out)
    check("the three verifications are recorded for the reader",
          "27-2020-DR-2233" in out and "24-16088-FC-04" in out and "EXECUTOR" in out)
    check("the actionable signal is named", "rising FIRM count" in out)
    # The firm one is still listed in full, and still marked as an echo.
    check("the firm finding is listed", "`1`" in out)
    check("the firm echo is still marked", "**ECHO**" in out)


def test_echoed_quote_prompt_drift():
    """The defect: the model reciting SCREEN_SYSTEM's own quoted markers as though it had observed
    them. 62% of every unsupported quote in the log, and one phrase is 27 of those.

    Two halves have to stay together. PROMPT_MARKERS is DERIVED from the prompt, so adding a quoted
    exemplar teaches the lint automatically -- these checks pin that derivation, and pin that the
    prompt still carries the rule that makes the finding actionable. Break either and the funnel
    goes back to inventing evidence in the one place an auditor looks first."""
    sys_text = update.SCREEN_SYSTEM
    check("prompt carries the quoting rule", "QUOTE ONLY WHAT YOU WERE SHOWN" in sys_text)
    check("rule says instructions are vocabulary, not observation",
          "not a record of what you saw" in sys_text)
    check("rule names the real instance", "05-2024-DP-001765" in sys_text)
    check("rule leaves 'name the marker' intact", "shows its work" in sys_text)

    # Derived, not listed: the two phrases the prompt supplies for dependency cases must be known
    # to the lint without anyone maintaining a second copy.
    check("'In the Interest of' is derived from the prompt",
          update._quote_norm("In the Interest of") in update.PROMPT_MARKERS)
    check("'In re Estate of' is derived too",
          update._quote_norm("In re Estate of") in update.PROMPT_MARKERS)
    # Short fragments the prompt also quotes ('or', the JSON scaffolding) would match real reasons
    # by accident, so they are excluded by length.
    check("2-char prompt fragments are not markers", "or" not in update.PROMPT_MARKERS)

    # The live record that prompted this fix, verbatim.
    name = "Z.H., Mother of S.R., a Child v. Department of Children and Families"
    reason = ("juvenile/dependency matter (caption shows 'In the Interest of' procedural context "
              "with child identified only by initials, docket prefix 'DP' indicates dependency)")
    ev = ("Case No. 5D2026-0764 LT Case No. 05-2024-DP-001765 Z.H., Mother of S.R., a Child, "
          "Appellant, v. DEPARTMENT OF CHILDREN AND FAMILIES,")
    check("the live echo is flagged unsupported",
          update.unsupported_quotes(reason, name, "5D2026-0764", ev) == ["In the Interest of"])
    check("the live echo is identified AS an echo",
          update.echoed_quotes(reason, name, "5D2026-0764", ev) == ["In the Interest of"])
    # 'DP' is in the docket line of the evidence and must NOT be flagged: the model got that one
    # right, and flagging a true marker is how a lint gets ignored.
    check("the supported marker in the same reason is untouched",
          "DP" not in update.unsupported_quotes(reason, name, "5D2026-0764", ev))

    # A docket-code guess is unsupported but NOT an echo: different failure, different fix, and
    # merging them would hide that the prompt repair does not address it.
    check("a docket-code guess is unsupported",
          update.unsupported_quotes("family/domestic ('FC' docket prefix)", "Smith v. Jones",
                                    "", "no codes here") == ["FC"])
    check("a docket-code guess is not an echo",
          update.echoed_quotes("family/domestic ('FC' docket prefix)", "Smith v. Jones",
                               "", "no codes here") == [])
    # A quoted marker actually present is neither.
    check("an observed marker is neither unsupported nor echoed",
          update.unsupported_quotes("juvenile ('In the Interest of')",
                                    "In the Interest of J.S., a Child", "", "") == []
          and update.echoed_quotes("juvenile ('In the Interest of')",
                                   "In the Interest of J.S., a Child", "", "") == [])


def test_hedge_prompt_drift():
    """The lint enforces a rule written in SCREEN_SYSTEM. If someone edits the words the prompt
    bans without editing HEDGE_RE, the funnel starts telling the model one thing and grading it by
    another -- silently, and in the direction that under-reports. Pin them together."""
    sys_text = update.SCREEN_SYSTEM
    for word in ("likely", "appears to be", "suggests"):
        check("prompt still bans %r" % word, word in sys_text)
        check("HEDGE_RE catches the prompt's %r" % word,
              bool(update.hedged_reason("dropped because it %s a family matter" % word)))
    # The prompt and the lint agree in MEANING, which is what matters, and the prompt has to carry
    # the distinction explicitly or the omission of indicat* reads as an oversight.
    check("prompt defines a hedge as qualifying the category", "qualifies your CATEGORY" in sys_text)
    check("prompt blesses naming the settling marker", "shows its work" in sys_text)
    check("prompt names the disjunction tell", "joined by 'or' is always a hedge" in sys_text)
    check("prompt no longer bans explanatory 'indicates'", "'indicates'" not in sys_text)


def test_screen_names_a_no_merits_ground():
    """A bare per curiam affirmance has no discoverable subject, so a screen forced to name a
    subject-matter category invents one -- the confabulation issue #293 measured (four of its
    eleven were PCAs). The prompt must offer the truthful ground instead."""
    t = update.SCREEN_SYSTEM
    check("screen has a no-merits ground", "DECIDES NOTHING ON" in t)
    check("screen names the per curiam shape", "per curiam" in t.lower())
    check("screen forbids reaching past it for a subject",
          "Do NOT reach past it" in t and "invented" in t)
    # pretriage reads full text and already had this ground; it must keep it, since the screen now
    # passes nothing extra to it -- both gates independently refuse a no-merits opinion.
    check("pretriage keeps its no-merits ground",
          "decides nothing on the merits" in update.PRETRIAGE_SYSTEM.lower())


def test_hedge_annotation():
    """_log_rejections is the one choke point every stage funnels through, so the stamp lands there
    and a stage added later is covered without anyone remembering to wire it up."""
    import tempfile
    recs = [{"stage": "screen", "cluster_id": 1, "name": "A v. B", "reason": "likely a family matter"},
            {"stage": "pretriage", "cluster_id": 2, "name": "C v. D", "reason": "workers' compensation"},
            {"stage": "triage", "cluster_id": 3, "name": "E v. F", "reason": "names suggest a divorce"}]
    d = tempfile.mkdtemp()
    old_path, old_env = update.REJECT_PATH, os.environ.pop("GITHUB_STEP_SUMMARY", None)
    try:
        update.REJECT_PATH = os.path.join(d, "rej.jsonl")
        update._log_rejections(recs)
        written = [json.loads(l) for l in open(update.REJECT_PATH, encoding="utf-8").read().splitlines() if l.strip()]
    finally:
        update.REJECT_PATH = old_path
        if old_env is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = old_env
    check("hedged screen reason stamped", written[0].get("hedge") == ["likely"])
    check("committed reason carries no hedge key", "hedge" not in written[1])
    check("lint covers non-screen stages too", written[2].get("hedge") == ["suggest"])


def test_hedge_report():
    """The two sections are independent claims and the file must reflect that: a hedge-only run
    still reports, and a run with neither writes nothing at all (the workflow surfaces the file
    whenever it exists, so an empty one would open an issue that says nothing)."""
    import tempfile
    d = tempfile.mkdtemp()
    old = smell_check.OUT
    hedged = [{"cluster_id": 7, "name": "G v. H", "court": "dcafl", "date": "2026-07-24",
               "reason": "likely a family matter", "hedge": ["likely"]}]
    susp = [({"cluster_id": 9, "name": "I v. J", "court": "ctapp", "date": "2026-07-25",
              "reason": "probate"}, "probate guessed")]
    try:
        smell_check.OUT = os.path.join(d, "a.md")
        smell_check.write_report([], 0, [])
        check("neither section -> no file written", not os.path.exists(smell_check.OUT))

        smell_check.OUT = os.path.join(d, "b.md")
        smell_check.write_report([], 0, hedged)
        body = open(smell_check.OUT, encoding="utf-8").read()
        check("hedge-only run still reports", "Hedged drop reasons: 1" in body)
        check("hedge-only run has no suspect header", "suspect drop(s)" not in body)
        check("hedge section carries no queue line", "!  #" not in body)
        check("hedge section says not a recall claim", "not recall claims" in body)

        smell_check.OUT = os.path.join(d, "c.md")
        smell_check.write_report(susp, 5, hedged)
        body = open(smell_check.OUT, encoding="utf-8").read()
        check("both sections present",
              "suspect drop(s) of 5 audited" in body and "Hedged drop reasons: 1" in body)
        check("suspect section still carries its queue line", "9 !  #" in body)

        smell_check.OUT = os.path.join(d, "d.md")
        smell_check.write_report(susp, 5, [])
        body = open(smell_check.OUT, encoding="utf-8").read()
        check("suspects-only run unchanged", "Hedged drop reasons" not in body and "9 !  #" in body)
    finally:
        smell_check.OUT = old



# Calibrated on all 1,939 logged reasons: 451 quote something, 388 of those quotes really are in the
# caption. The two dominant misses are opposite cases and the lint must split them.
QUOTE_CORPUS = [
    # The caption reads "v. State"; the model quotes the canonical form SCREEN_SYSTEM itself uses.
    # The claim is TRUE and the caption supports it -- flagging 22 of these would bury the real
    # class below, the same trap indicat* set for the hedge lint.
    ("Criminal case - 'v. The State' indicates prosecution appeal", "Chaz M. Dobbs v. State", "A26A2203", "", False),
    ("Criminal (v. The State)", "Ismael Gomez v. State", "A26A2119", "", False),
    # The caption contains no such phrase. Correct drop, invented evidence -- the class an audit
    # cannot see from the outcome and the reason alone.
    ("Dependency - 'In the Interest of' caption", "J.S., a Child v. State of Florida", "1D2025-0696", "", True),
    ("Juvenile: 'In the Interest of' minor", "C.M. v. Mobile County Department of Human Resources", "", "", True),
    # Real markers that live in the DOCKET, not the caption. Checking the caption alone flags these
    # wrongly, so the haystack has to include the docket number.
    ("Family/domestic ('DR' case number)", "Smith v. Smith", "2024-DR-1188", "", False),
    ("Landlord-tenant ('LT Case No.' shown)", "Dodge v. Almeida", "LT Case No. 22-114", "", False),
    # A marker genuinely present in the stored excerpt is supported.
    ("Probate ('In re Estate of' in the opening)", "Kudler v. Bethesda", "4D2025-1", "IN RE ESTATE OF HOWARD KUDLER", False),
    # ... and the same quote with the excerpt kept but silent is not.
    ("Probate ('In re Estate of' in the opening)", "Kudler v. Bethesda", "4D2025-1", "PER CURIAM. AFFIRMED.", True),
    # A possessive must never be read as an opening single quote.
    ("Dismissed on the defendant's own motion; no merits", "A v. B", "1", "", False),
]


def test_quote_lint():
    for reason, name, docket, ev, want in QUOTE_CORPUS:
        got = bool(update.unsupported_quotes(reason, name, docket, ev))
        check("quote %s: %s" % ("flags" if want else "clears", reason[:42]), got == want,
              "got %r" % (update.unsupported_quotes(reason, name, docket, ev),))
    check("no quotes -> no finding", update.unsupported_quotes("family/domestic matter", "A v. B") == [])
    check("both quote styles extracted",
          sorted(update.quoted_markers("saw 'In re' and \"v. State\"")) == ["In re", "v. State"])
    check("curly quotes extracted",
          update.quoted_markers("caption \u201cIn the Interest of\u201d") == ["In the Interest of"])
    # A single character is an initial or a stray apostrophe far more often than a marker, so the
    # extractor requires two: 'A' in "A. P. v. Department" must not read as a quoted claim.
    check("one-character quote is ignored", update.quoted_markers("parties given as 'A' only") == [])
    check("possessive is not a quote", update.quoted_markers("the defendant's motion") == [])
    check("a supported quote repeated once is not double-reported",
          update.unsupported_quotes("'In the Interest of' and again 'In the Interest of'",
                                    "A v. B", "", "") == ["In the Interest of"])


def test_audit_block():
    r = {}
    update.record_audit(r, "confirmed", "full_opinion", by="tool/x", note="bare PCA")
    check("audit stamped", r["audit"]["verdict"] == "confirmed" and r["audit"]["depth"] == "full_opinion")
    check("audit records who", r["audit"]["by"] == "tool/x")
    check("audit records when", r["audit"]["ts"].endswith("Z"))
    check("audit keeps the note", r["audit"]["note"] == "bare PCA")
    # A cheap reason-only pass must never erase an expensive full-opinion read, or the log would
    # silently lose the strongest thing anyone established about the drop.
    update.record_audit(r, "recovered", "reason_only", by="smell")
    check("weaker depth cannot overwrite stronger",
          r["audit"]["verdict"] == "confirmed" and r["audit"]["by"] == "tool/x")
    # A later full read may legitimately change the verdict.
    update.record_audit(r, "recovered", "full_opinion", by="tool/y")
    check("equal depth may revise", r["audit"]["verdict"] == "recovered")
    check("audited_to_depth true at depth", update.audited_to_depth(r, "full_opinion"))
    check("audited_to_depth true below depth", update.audited_to_depth(r, "reason_only"))
    check("unaudited record is not audited", not update.audited_to_depth({}))
    shallow = {}
    update.record_audit(shallow, "confirmed", "reason_only", by="smell")
    check("reason_only does not satisfy full_opinion",
          not update.audited_to_depth(shallow, "full_opinion"))
    for bad in ("maybe", "", None):
        try:
            update.record_audit({}, bad)
            check("bad verdict %r rejected" % (bad,), False)
        except ValueError:
            check("bad verdict %r rejected" % (bad,), True)
    try:
        update.record_audit({}, "confirmed", depth="skimmed")
        check("bad depth rejected", False)
    except ValueError:
        check("bad depth rejected", True)


def test_audit_retires_from_queue():
    """The whole durability claim: a drop read to the bottom stops being re-audited by every tool
    that comes along. If this regresses, the 37 opinions already read get re-read forever."""
    settled = {"stage": "screen", "cluster_id": 1, "name": "A v. B", "reason": "probate",
               "audit": {"verdict": "confirmed", "depth": "full_opinion", "by": "x", "ts": "t"}}
    shallow = {"stage": "screen", "cluster_id": 2, "name": "C v. D", "reason": "probate",
               "audit": {"verdict": "confirmed", "depth": "reason_only", "by": "x", "ts": "t"}}
    fresh = {"stage": "screen", "cluster_id": 3, "name": "E v. F", "reason": "probate"}
    lines = [json.dumps(x) for x in (settled, shallow, fresh)]
    picked, _ = smell_check.select_records(lines, stages=["screen"])
    ids = [r["cluster_id"] for r in picked]
    check("full_opinion audit retires the drop", 1 not in ids)
    check("reason_only audit does not retire it", 2 in ids)
    check("unaudited drop is still queued", 3 in ids)
    picked_all, _ = smell_check.select_records(lines, stages=["screen"], all_flag=True)
    check("SMELL_ALL still forces a re-read", len(picked_all) == 3)


def test_quote_report_section():
    """Firm and provisional findings are different claims and the report must not blur them: a
    record logged before evidence capture cannot rule out that the marker was in the excerpt."""
    import tempfile
    d = tempfile.mkdtemp()
    old = smell_check.OUT
    firm = {"cluster_id": 5, "name": "J.S., a Child v. State of Florida", "court": "dcafl",
            "date": "2026-07-01", "reason": "Dependency - 'In the Interest of' caption",
            "evidence": "PER CURIAM. AFFIRMED.", "unsupported_quote": ["In the Interest of"]}
    prov = {"cluster_id": 6, "name": "C.M. v. Mobile County DHR", "court": "civappal",
            "date": "2026-07-02", "reason": "Juvenile: 'In the Interest of'"}
    try:
        smell_check.OUT = os.path.join(d, "q.md")
        smell_check.write_report([], 0, [], [firm, prov])
        body = open(smell_check.OUT, encoding="utf-8").read()
        # Headline is the FIRM count, not firm+provisional: see
        # test_report_headlines_firm_not_provisional for why summing them misreports a coverage
        # gap as a defect rate.
        check("quote section headlines the firm count", "Unsupported quoted markers: 1 firm" in body)
        check("firm finding is listed", "J.S., a Child" in body)
        check("provisional finding is counted, not listed",
              "1 unverifiable (not findings)" in body and "Mobile County DHR" not in body)
        check("quote section says evidence does not exist", "which does not exist" in body)
        check("quote section carries no queue line", "!  #" not in body)
        smell_check.OUT = os.path.join(d, "n.md")
        smell_check.write_report([], 0, [], [])
        check("no findings at all -> still no file", not os.path.exists(smell_check.OUT))
    finally:
        smell_check.OUT = old


def test_evidence_capture():
    """The screen's excerpt is ephemeral -- not reconstructible from the cluster id later -- so if
    this stops being written, every future screen reason becomes unfalsifiable again."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.py"),
               encoding="utf-8").read()
    check("screen drop records the excerpt it was shown", '"evidence": (snip or "")[:EVIDENCE_CAP]' in src)
    check("the excerpt passed to screen is the one recorded", "s = screen(name, docket, snip)" in src)
    check("evidence is bounded", update.EVIDENCE_CAP > 0 and update.EVIDENCE_CAP <= 1000)
    # An empty excerpt beside a confident subject reason is itself the finding, so the key must be
    # written even when blank -- an absent key means "logged before capture existed", not "empty".
    check("empty evidence is still a written key", '(snip or "")[:EVIDENCE_CAP]' in src)



def test_category_contract():
    """The prompts have always said "your reason must name one of the categories" and nothing ever
    checked it. These pin the token contract that makes the claim verifiable."""
    for cats, sys_text, label in ((update.SCREEN_CATEGORIES, update.SCREEN_SYSTEM, "screen"),
                                  (update.PRETRIAGE_CATEGORIES, update.PRETRIAGE_SYSTEM, "pretriage")):
        check("%s prompt asks for a category" % label, "`category`" in sys_text)
        check("%s prompt lists every token" % label, all(c in sys_text for c in cats))
        check("%s prompt ties token to reason" % label, "SAME ground your reason states" in sys_text)
        check("%s prompt says pick the token first" % label, "Pick the token FIRST" in sys_text)
        check("%s output shape names category" % label, '"category"' in sys_text)
    # The two lists differ ON PURPOSE. SCREEN_SYSTEM routes landlord-tenant and bankruptcy to the
    # full-text gate because neither can be told from an in-scope claim by a caption -- a
    # slip-and-fall at an apartment complex is a premises case. Handing the screen those tokens
    # would invite exactly the drops that reasoning forbids, so this must never be "simplified".
    for tok in ("landlord_tenant", "bankruptcy"):
        check("screen has no %r token" % tok, tok not in update.SCREEN_CATEGORIES)
        check("pretriage has the %r token" % tok, tok in update.PRETRIAGE_CATEGORIES)
        check("%r rejected at screen" % tok, not update.category_ok(tok, "screen"))
        check("%r accepted at pretriage" % tok, update.category_ok(tok, "pretriage"))
    check("screen list is a subset of pretriage's",
          set(update.SCREEN_CATEGORIES) <= set(update.PRETRIAGE_CATEGORIES))
    # workers_comp is the token whose absence let Altamira be dropped as "probate" for a month.
    check("workers_comp is on both lists",
          update.category_ok("workers_comp", "screen") and update.category_ok("workers_comp", "pretriage"))
    check("no_merits token exists for a bare disposition", update.category_ok("no_merits", "screen"))
    check("an invented token is rejected", not update.category_ok("probate_or_wills", "screen"))
    check("an empty token is rejected", not update.category_ok("", "screen"))
    # triage has no closed list, so nothing is judged there rather than guessed at.
    check("a stage with no list judges nothing", update.category_ok("anything", "triage"))


def test_category_is_recorded_not_enforced():
    """Fail-open, exactly like the reason lints: an invalid token is a finding about the record, not
    a reason to reroute the case. If this ever starts changing verdicts, a formatting slip begins
    flipping correct drops."""
    import tempfile
    recs = [{"stage": "screen", "cluster_id": 1, "name": "A v. B", "reason": "wc", "category": "workers_comp"},
            {"stage": "screen", "cluster_id": 2, "name": "C v. D", "reason": "p", "category": "landlord_tenant"},
            {"stage": "screen", "cluster_id": 3, "name": "E v. F", "reason": "p", "category": ""},
            {"stage": "pretriage", "cluster_id": 4, "name": "G v. H", "reason": "lt", "category": "landlord_tenant"},
            {"stage": "triage", "cluster_id": 5, "name": "I v. J", "reason": "no"}]
    d = tempfile.mkdtemp()
    old_path, old_env = update.REJECT_PATH, os.environ.pop("GITHUB_STEP_SUMMARY", None)
    try:
        update.REJECT_PATH = os.path.join(d, "r.jsonl")
        update._log_rejections(recs)
        w = [json.loads(l) for l in open(update.REJECT_PATH, encoding="utf-8").read().splitlines() if l.strip()]
    finally:
        update.REJECT_PATH = old_path
        if old_env is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = old_env
    check("a valid token is left unflagged", "category_invalid" not in w[0])
    check("a pretriage-only token is invalid at screen", w[1].get("category_invalid") == "landlord_tenant")
    check("a missing token at a listed stage is flagged", w[2].get("category_invalid") == "(none)")
    check("the same token is valid at pretriage", "category_invalid" not in w[3])
    check("a stage with no closed list is not judged", "category_invalid" not in w[4])
    check("every record still survives logging", len(w) == 5)
    # Hand-built records cannot prove the funnel actually emits the field, so assert the drop sites
    # themselves -- the gap that let a mutation removing the screen's category line pass silently.
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.py"),
               encoding="utf-8").read()
    check("the screen drop writes a category",
          src.count('"category": (s.get("category") or "").strip()') == 1)
    check("the pretriage drop writes a category",
          src.count('"category": (ps.get("category") or "").strip()') == 1)


def main():
    print("smell prompt + parsing:")
    test_prompt_shape()
    test_verdict_parsing()
    test_batch_path_and_fallback()
    print("smell selection:")
    test_select()
    print("chunking:")
    test_chunking()
    print("retro record selection:")
    test_retro_selection()
    print("retro persistence:")
    test_retro_persistence()
    test_stage_config()
    test_model_resolution()
    print("reclamation exposure:")
    test_reclamation_exposure()
    print("hedge lint:")
    test_hedge_lint()
    test_report_headlines_firm_not_provisional()
    test_echoed_quote_prompt_drift()
    test_hedge_prompt_drift()
    test_hedge_annotation()
    print("no-merits ground:")
    test_screen_names_a_no_merits_ground()
    print("report sections:")
    test_hedge_report()
    print("quote lint:")
    test_quote_lint()
    test_quote_report_section()
    print("evidence capture:")
    test_evidence_capture()
    print("category contract:")
    test_category_contract()
    test_category_is_recorded_not_enforced()
    print("audit provenance:")
    test_audit_block()
    test_audit_retires_from_queue()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
