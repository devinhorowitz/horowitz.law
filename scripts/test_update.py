#!/usr/bin/env python3
"""Self-tests for scripts/update.py. Standard library only; no network and no API key.

Currently covers crosscheck()'s two false-flag guardrails, added after the 2026-06-28
maintenance run failed on a cross-check flag whose premise the model invented (the
Barnor-Cooper card, cluster 10862301): a "temporary substitute car because of mechanical
issues" rationale the card never stated. The fixture below mirrors that card's shape so the
regression is pinned without depending on the live opinions.json.

It stubs update.anthropic_json, so it makes no Anthropic and no CourtListener calls. CI runs
it in the smoke job; run it directly to check a change to crosscheck:

  python scripts/test_update.py        # prints each case; exits nonzero on any failure
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update  # after sys.path, mirroring the other scripts' import-by-sibling-name pattern


# A synthetic card mirroring the 2026-06-28 Barnor-Cooper false positive. Built here on purpose,
# not loaded from opinions.json, so the regression stays pinned even if that card is edited or pruned.
CARD = {
    "name": "Insurer v. Claimant (fixture mirroring the 2026-06-28 Barnor-Cooper false positive)",
    "areas": ["coverage", "auto"],
    "disposition": "reversed",
    "synopsis": (
        "The Court of Appeals reversed the denial of summary judgment to the insurers in a UM claim, "
        "holding the claimant was not entitled to UM benefits: the borrowed vehicle was not a "
        "\"temporary substitute car\" because the scheduled vehicle it would have replaced had been "
        "sold, and the named-insured LLC is a separate legal entity from its members and managers."
    ),
    "why": (
        "Reads the temporary-substitute and named-insured provisions strictly: a borrowed vehicle is "
        "not a temporary substitute once the scheduled car it would replace has been sold."
    ),
}
OPINION = "FULL OPINION TEXT (stubbed; the test never sends it to a model)."

# A span copied verbatim from the fixture synopsis: a flag quoting this is substantiated.
REAL_QUOTE = "the named-insured LLC is a separate legal entity from its members and managers"
# The invented rationale from the real failure: absent from the summary, so a flag quoting it is dismissed.
FAB_QUOTE = "the borrowed vehicle was a temporary substitute car because of mechanical issues with the Dodge Caravan"


class Stub:
    """Scripted stand-in for update.anthropic_json. Each call returns the next item; an Exception
    item is raised; a call past the end fails the test, which catches an unexpected re-ask."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self, body, label=None):
        i = self.calls
        self.calls += 1
        if i >= len(self.seq):
            raise AssertionError("crosscheck over-called: attempt %d, only %d scripted" % (i + 1, len(self.seq)))
        item = self.seq[i]
        if isinstance(item, Exception):
            raise item
        return item


def match():
    return {"verdict": "match", "quote": "", "reason": "ok"}


def flag(quote, reason="misstates the holding"):
    return {"verdict": "flag", "quote": quote, "reason": reason}


# (label, scripted responses, OPINIONS_CROSSCHECK_TRIES, expected verdict, expected calls,
#  expected flag_count, substring the reason must contain). None means "do not assert".
CASES = [
    # Clean card: every roll matches. Verdict match; early-exit once a majority has cleared.
    ("clean_match", [match()] * 5, 3, "match", 2, 0, None),
    # The 2026-06-28 regression: a flag whose quoted premise is not in the summary is dismissed.
    ("fabricated_premise_dismissed", [flag(FAB_QUOTE)] * 3, 3, "match", 2, 0, None),
    # A genuine, substantiated flag confirmed by consensus: verdict flag, quote folded into the reason.
    ("substantiated_unanimous", [flag(REAL_QUOTE)] * 3, 3, "flag", 2, 2, REAL_QUOTE),
    # A substantiated flag on only a minority of rolls is noise: cleared.
    ("substantiated_minority", [flag(REAL_QUOTE), match(), match()], 3, "match", 3, 1, None),
    # A substantiated flag on a majority of rolls stands.
    ("substantiated_majority", [flag(REAL_QUOTE), match(), flag(REAL_QUOTE)], 3, "flag", 3, 2, None),
    # Every attempt errors: fail-open to unavailable so the card still surfaces for a manual look.
    ("all_errors_unavailable", [RuntimeError("boom")] * 3, 3, "unavailable", 3, None, None),
    # Consensus off (tries=1) but grounding on: a substantiated flag stands.
    ("tries1_substantiated_flag", [flag(REAL_QUOTE)], 1, "flag", 1, 1, REAL_QUOTE),
    # Consensus off, fabricated quote: still dismissed by grounding.
    ("tries1_fabricated_dismissed", [flag(FAB_QUOTE)], 1, "match", 1, 0, None),
    # A too-short quote does not substantiate a flag.
    ("short_quote_dismissed", [flag("is")] * 3, 3, "match", 2, 0, None),
    # An error then two substantiated flags: the two attempts actually made carry the majority.
    ("error_then_two_flags", [RuntimeError("x"), flag(REAL_QUOTE), flag(REAL_QUOTE)], 3, "flag", 3, 2, None),
]


def run_case(label, seq, tries, verdict, calls, flag_count, reason_has):
    update.anthropic_json = Stub(seq)
    update.CROSSCHECK_TRIES = tries
    r = update.crosscheck(CARD["name"], OPINION, CARD)
    made = update.anthropic_json.calls
    assert isinstance(r, dict), "%s: result is not a dict" % label
    assert r["verdict"] in ("match", "flag", "unavailable"), "%s: bad verdict %r" % (label, r["verdict"])
    assert isinstance(r.get("reason", ""), str), "%s: reason is not a string" % label
    assert r["verdict"] == verdict, "%s: verdict %r != %r (%r)" % (label, r["verdict"], verdict, r)
    if calls is not None:
        assert made == calls, "%s: made %d calls, expected %d" % (label, made, calls)
    if flag_count is not None:
        assert r.get("flag_count") == flag_count, "%s: flag_count %r != %r" % (label, r.get("flag_count"), flag_count)
    if reason_has is not None:
        assert reason_has in r.get("reason", ""), "%s: reason lacks %r (%r)" % (label, reason_has, r.get("reason"))
    print("  ok  %-32s verdict=%-11s calls=%s flag_count=%s" % (label, r["verdict"], made, r.get("flag_count")))


def test_substantiation_helper():
    drafted = "Holding 1 (areas: coverage, auto)\nSynopsis: %s\nWhy it matters: %s" % (CARD["synopsis"], CARD["why"])
    assert update._quote_substantiated(REAL_QUOTE, drafted), "verbatim span should substantiate"
    assert update._quote_substantiated(
        '  "The Named-Insured LLC   is a SEPARATE legal entity from its members and managers"  ', drafted
    ), "wrapping quotes, case, and extra whitespace should still substantiate"
    assert not update._quote_substantiated(FAB_QUOTE, drafted), "an invented premise should not substantiate"
    assert not update._quote_substantiated("", drafted), "empty quote should not substantiate"
    assert not update._quote_substantiated("   ", drafted), "whitespace quote should not substantiate"
    print("  ok  substantiation helper (presence, normalization, empties)")


# --- completeness_check: the same guardrails, with the grounding source flipped to the opinion ---
# The opinion fixture states a separate material holding verbatim; a completeness flag must quote it.
COMP_OPINION = (
    "The trial court granted summary judgment to the defendant and we affirm. The record does not "
    "show the premises were in a hazardous condition. The court further held that the plaintiff's "
    "claim was independently barred because the statute of limitations had run before suit was filed."
)
COMP_REAL = "the plaintiff's claim was independently barred because the statute of limitations had run before suit was filed"
COMP_FAB = "the court awarded treble damages and attorney fees under the civil RICO count"


def complete():
    return {"verdict": "complete", "quote": "", "reason": "ok"}


def cflag(quote, reason="omits a material holding"):
    return {"verdict": "flag", "quote": quote, "reason": reason}


CASES_COMP = [
    # Clean card: every roll says complete. Verdict complete; early-exit once a majority has cleared.
    ("comp_clean_complete", [complete()] * 5, 3, "complete", 2, 0, None),
    # An invented omission whose quoted holding is not in the opinion is dismissed.
    ("comp_fabricated_omission_dismissed", [cflag(COMP_FAB)] * 3, 3, "complete", 2, 0, None),
    # A real omission quoting the opinion, confirmed by consensus: verdict flag, quote folded into reason.
    ("comp_substantiated_unanimous", [cflag(COMP_REAL)] * 3, 3, "flag", 2, 2, COMP_REAL),
    # A substantiated flag on only a minority of rolls is noise: cleared.
    ("comp_substantiated_minority", [cflag(COMP_REAL), complete(), complete()], 3, "complete", 3, 1, None),
    # A substantiated flag on a majority of rolls stands.
    ("comp_substantiated_majority", [cflag(COMP_REAL), complete(), cflag(COMP_REAL)], 3, "flag", 3, 2, None),
    # Every attempt errors: fail-open to unavailable so the card still surfaces.
    ("comp_all_errors_unavailable", [RuntimeError("boom")] * 3, 3, "unavailable", 3, None, None),
    # Consensus off (tries=1) but grounding on: a substantiated flag stands.
    ("comp_tries1_substantiated_flag", [cflag(COMP_REAL)], 1, "flag", 1, 1, COMP_REAL),
    # Consensus off, invented omission: still dismissed by grounding.
    ("comp_tries1_fabricated_dismissed", [cflag(COMP_FAB)], 1, "complete", 1, 0, None),
]


def run_comp_case(label, seq, tries, verdict, calls, flag_count, reason_has):
    update.anthropic_json = Stub(seq)
    update.COMPLETENESS_TRIES = tries
    r = update.completeness_check(CARD["name"], COMP_OPINION, CARD)
    made = update.anthropic_json.calls
    assert isinstance(r, dict), "%s: result is not a dict" % label
    assert r["verdict"] in ("complete", "flag", "unavailable"), "%s: bad verdict %r" % (label, r["verdict"])
    assert isinstance(r.get("reason", ""), str), "%s: reason is not a string" % label
    assert r["verdict"] == verdict, "%s: verdict %r != %r (%r)" % (label, r["verdict"], verdict, r)
    if calls is not None:
        assert made == calls, "%s: made %d calls, expected %d" % (label, made, calls)
    if flag_count is not None:
        assert r.get("flag_count") == flag_count, "%s: flag_count %r != %r" % (label, r.get("flag_count"), flag_count)
    if reason_has is not None:
        assert reason_has in r.get("reason", ""), "%s: reason lacks %r (%r)" % (label, reason_has, r.get("reason"))
    print("  ok  %-32s verdict=%-11s calls=%s flag_count=%s" % (label, r["verdict"], made, r.get("flag_count")))


# --- docket-aware duplicate guard (pure functions; no model or network calls) ---
CASES_DEDUP = [
    # (label, courtA, dateA, docketsA, nameA, courtB, dateB, docketsB, nameB, same_case?)
    # A corrected opinion republished under a new cluster: same court, same docket, later date.
    ("revision_shared_docket_diff_date", "ctapp", "2026-03-01", ["A26A0526"], "Smith v. Jones",
     "ctapp", "2026-04-15", "A26A0526", "Smith v. Jones", True),
    # A split-docket twin of one consolidated appeal: same court and day, dockets differ, parties match.
    ("split_docket_twin_same_day", "ctapp", "2026-03-01", ["A26A0526"], "Barnor-Cooper v. Acme Roofing",
     "ctapp", "2026-03-01", "A26A0550", "Acme Roofing v. Barnor-Cooper", True),
    # A genuine repeat appearance at a higher court is NOT a duplicate: the court differs.
    ("higher_court_repeat_diff_court", "ctapp", "2026-03-01", ["A26A0526"], "Smith v. Jones",
     "scotga", "2026-06-01", "S26G0010", "Smith v. Jones", False),
    # Same court and day but only one shared distinctive token: not enough to merge.
    ("one_shared_token_same_day", "ctapp", "2026-03-01", ["X100"], "Washington v. Lincoln Apartments",
     "ctapp", "2026-03-01", ["X200"], "Washington v. Jefferson Holdings", False),
    # Same parties, no docket signal, different day: too little to merge, kept separate.
    ("same_parties_diff_day_no_docket", "ctapp", "2026-03-01", [], "Barnor-Cooper v. Acme Roofing",
     "ctapp", "2026-05-01", [], "Barnor-Cooper v. Acme Roofing", False),
    # Unrelated cases decided the same day: no shared docket or parties.
    ("unrelated_same_day", "ctapp", "2026-03-01", ["A1234"], "Alpha v. Beta",
     "ctapp", "2026-03-01", ["C5678"], "Gamma v. Delta", False),
]


def run_dedup_case(label, ca, da, ka, na, cb, db, kb, nb, expected):
    a = update._dup_sig(ca, da, ka, na)
    b = update._dup_sig(cb, db, kb, nb)
    got = update._same_case(a, b)
    assert got == expected, "%s: _same_case=%r expected %r" % (label, got, expected)
    assert update._same_case(b, a) == expected, "%s: relation is not symmetric" % label
    print("  ok  %-34s same_case=%s" % (label, got))


def test_docket_set():
    assert update._docket_set("A26A0526, A26A0550") == {"A26A0526", "A26A0550"}, "comma split"
    assert update._docket_set(["No. 21-1234"]) == {"21-1234"}, "list and No. prefix dropped"
    assert update._docket_set("") == set(), "empty string"
    assert update._docket_set(["A1", "and", "S24G0123"]) == {"S24G0123"}, "short token and noise dropped"
    print("  ok  docket-set normalization (split, noise, empties)")


def test_draft_pending():
    """The tier-3 summarize-batch orchestration (OPINIONS_BATCH, update._draft_pending): result
    mapping by custom_id, the ok / errored-line / unparseable-body / whole-batch-defer branches, and
    the drafted (evaluated) set it returns. Stubs batch.run, so it exercises the real summarize_request
    + batch.from_body request building with no network."""
    print("tier-3 summarize batch (_draft_pending):")
    pend = [{"cid": c, "r": {}, "name": nm, "court_id": "ga", "docket": "A%d" % c,
             "date_filed": "2026-07-0%d" % i, "text": "t%d" % c, "note": "", "cl_status": "published"}
            for i, (c, nm) in enumerate([(111, "A v. B"), (222, "C v. D"), (333, "E v. F")], 1)]
    real_run = update.batch.run
    finished = []

    def finish_fn(v, p):
        finished.append((p["cid"], v))

    def mixed_run(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
        assert sorted(rq["custom_id"] for rq in reqs) == ["111", "222", "333"], [rq["custom_id"] for rq in reqs]
        return {"111": {"ok": True, "text": '{"relevant": true, "significance": "high"}', "stop_reason": "end_turn"},
                "222": {"ok": False, "type": "errored", "error": "x"},
                "333": {"ok": True, "text": "not json {{{", "stop_reason": "end_turn"}}
    update.batch.run = mixed_run
    try:
        drafted = update._draft_pending(pend, deadline=123.0, finish_fn=finish_fn)
    finally:
        update.batch.run = real_run
    assert drafted == {111}, drafted
    assert finished == [(111, {"relevant": True, "significance": "high"})], finished
    print("  ok  ok line drafts+finishes; errored and unparseable lines skip (retry next run)")

    for name, exc in (("timeout", update.batch.BatchTimeout("bid", "still running")),
                      ("transport error", update.batch.BatchError("submit failed"))):
        finished.clear()

        def raiser(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None, _e=exc):
            raise _e
        update.batch.run = raiser
        try:
            drafted = update._draft_pending(pend, deadline=123.0, finish_fn=finish_fn)
        finally:
            update.batch.run = real_run
        assert drafted == set() and finished == [], (name, drafted, finished)
        print("  ok  batch %s defers the whole draft set (nothing evaluated)" % name)


def test_guard_cards_batch():
    """The post-draft fidelity-guard batch (OPINIONS_GUARD_BATCH, update.guard_cards_batch): one request
    per (card, guard kind), results mapped by custom_id, grounding applied via guard_verdict, and
    crosschecks/completeness populated in place. A per-line failure -> 'unavailable' for that guard; a
    whole-batch failure -> returns False WITHOUT populating (the caller falls back to the sync guards).
    Stubs batch.run, so it exercises the real guard_request + batch.from_body building with no network."""
    print("post-draft guard batch (guard_cards_batch):")
    items = [{"cid": 111, "name": CARD["name"], "text": OPINION, "entry": CARD},
             {"cid": 222, "name": CARD["name"], "text": OPINION, "entry": CARD}]
    real_run = update.batch.run

    def guard_run(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
        ids = sorted(rq["custom_id"] for rq in reqs)
        assert ids == ["111-completeness", "111-fidelity", "222-completeness", "222-fidelity"], ids
        return {
            "111-fidelity": {"ok": True, "text": '{"verdict": "match"}'},
            "111-completeness": {"ok": True, "text": '{"verdict": "complete"}'},
            # 222 fidelity flags with a quote copied verbatim from the drafted summary (grounded).
            "222-fidelity": {"ok": True, "text": '{"verdict": "flag", "reason": "misstates", "quote": "%s"}' % REAL_QUOTE},
            "222-completeness": {"ok": False, "type": "errored"},   # a per-line failure -> unavailable
        }
    cc, cp = {}, {}
    update.batch.run = guard_run
    try:
        ok = update.guard_cards_batch(items, cc, cp, deadline=1.0)
    finally:
        update.batch.run = real_run
    assert ok is True, ok
    assert cc[111]["verdict"] == "match", cc[111]
    assert cp[111]["verdict"] == "complete", cp[111]
    assert cc[222]["verdict"] == "flag" and REAL_QUOTE in cc[222].get("quote", ""), cc[222]
    assert cp[222]["verdict"] == "unavailable", cp[222]     # errored line -> unavailable; card still surfaces
    print("  ok  batched guards map by custom_id, ground a flag, and mark an errored line unavailable")

    cc2, cp2 = {}, {}

    def raiser(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
        raise update.batch.BatchTimeout("bid", "still running")
    update.batch.run = raiser
    try:
        ok2 = update.guard_cards_batch(items, cc2, cp2, deadline=1.0)
    finally:
        update.batch.run = real_run
    assert ok2 is False and cc2 == {} and cp2 == {}, (ok2, cc2, cp2)
    print("  ok  a whole-batch failure returns False without populating (caller falls back to sync)")


def test_triage_batch():
    """The tier-2 triage-batch orchestration (OPINIONS_TRIAGE_BATCH, update._triage_batch): result
    mapping by custom_id, and -- unlike the summarize batch -- a SYNCHRONOUS FALLBACK for any line the
    batch does not usably return, so the returned {cid: verdict} space is always complete and the gate
    is never silently changed. Exercises the ok line, the per-line errored + unparseable-body fallbacks,
    and a whole-batch timeout/error fallback. Stubs batch.run and update.triage (the sync path), so it
    runs the real triage_request + batch.from_body building with no network."""
    print("tier-2 triage batch (_triage_batch):")
    items = [{"cid": 111, "name": "A v. B", "docket": "A111", "text": "t111"},
             {"cid": 222, "name": "C v. D", "docket": "A222", "text": "t222"},
             {"cid": 333, "name": "E v. F", "docket": "A333", "text": "t333"}]
    real_run, real_triage = update.batch.run, update.triage
    sync_calls = []

    def fake_triage(name, docket, text, feed_index=""):
        sync_calls.append(name)
        return {"relevant": True, "significance": "high", "note": "sync:%s" % name}

    # 111 ok from the batch; 222 errored line -> sync fallback; 333 unparseable body -> sync fallback.
    def mixed_run(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
        assert sorted(rq["custom_id"] for rq in reqs) == ["111", "222", "333"], [rq["custom_id"] for rq in reqs]
        return {"111": {"ok": True, "text": '{"relevant": true, "significance": "high", "note": "batch"}'},
                "222": {"ok": False, "type": "errored", "error": "x"},
                "333": {"ok": True, "text": "not json {{{"}}
    update.batch.run, update.triage = mixed_run, fake_triage
    try:
        verdicts = update._triage_batch(items, "", deadline=123.0)
    finally:
        update.batch.run, update.triage = real_run, real_triage
    assert set(verdicts) == {111, 222, 333}, verdicts                 # every candidate has a verdict
    assert verdicts[111]["note"] == "batch", verdicts[111]            # ok line came from the batch
    assert verdicts[222]["note"] == "sync:C v. D", verdicts[222]      # errored line fell back to sync
    assert verdicts[333]["note"] == "sync:E v. F", verdicts[333]      # unparseable body fell back to sync
    assert sorted(sync_calls) == ["C v. D", "E v. F"], sync_calls     # only the two missing lines
    print("  ok  ok line from batch; errored and unparseable lines fall back to synchronous triage")

    for label, exc in (("timeout", update.batch.BatchTimeout("bid", "still running")),
                       ("transport error", update.batch.BatchError("submit failed"))):
        sync_calls.clear()

        def raiser(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None, _e=exc):
            raise _e
        update.batch.run, update.triage = raiser, fake_triage
        try:
            verdicts = update._triage_batch(items, "", deadline=123.0)
        finally:
            update.batch.run, update.triage = real_run, real_triage
        assert set(verdicts) == {111, 222, 333}, (label, verdicts)          # whole set still triaged
        assert sorted(sync_calls) == ["A v. B", "C v. D", "E v. F"], (label, sync_calls)
        print("  ok  batch %s: every candidate falls back to synchronous triage (gate unchanged)" % label)


def test_treatment_citer_seen():
    """Claim-1 regression (the vetoed-treatment loop): route_and_publish marks a treatment citer SEEN,
    not held-out like a card. Held out, a vetoed treatment finding was redraft-logged, re-discovered,
    and re-escalated every run forever; marked seen, the loop cannot form. Held CARDS still stay out of
    seen (their veto is meant to redraft)."""
    import json as _json
    import os as _os
    import shutil as _sh
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="route-test-")
    saved = {}

    def sv(obj, name, val):
        saved[(id(obj), name)] = (obj, name, getattr(obj, name))
        setattr(obj, name, val)
    try:
        sv(update, "STATE_PATH", _os.path.join(tmp, "state.json"))
        sv(update, "REVIEW_PR_PATH", _os.path.join(tmp, "review_pr.md"))
        sv(update.review_store, "REVIEW_DIR", _os.path.join(tmp, "review"))
        sv(update.review_store, "CARDS_DIR", _os.path.join(tmp, "review", "cards"))
        sv(update.review_store, "TREAT_DIR", _os.path.join(tmp, "review", "treatments"))
        sv(update.review_store, "PENDING_PATH", _os.path.join(tmp, "pending.json"))
        citer = {"cluster_id": 5555, "name": "Later v. State", "court": "ctapp",
                 "date": "2026-06-01", "kind": "overruled"}
        state = {"seen_clusters": []}
        counts = update.route_and_publish(
            added=[], treat_events=[{"card_cid": 111, "citer": citer}], clean_entries=[],
            flagged=[], crosschecks={}, completeness={}, overruling_cids=set(), pending_review=set(),
            state=state, seen=set(), evaluated={5555}, have=set(), now_iso="2026-07-16T00:00:00Z",
            treat_flags=[("Old Card", "Later v. State", "overruled")])
        written = _json.load(open(update.STATE_PATH))
        assert 5555 in written.get("seen_clusters", []), "treatment citer must be marked seen (no veto loop)"
        assert counts["treatments"] == 1, counts
    finally:
        for obj, name, val in saved.values():
            setattr(obj, name, val)
        _sh.rmtree(tmp, ignore_errors=True)
    print("  ok  a treatment citer is marked seen (breaks the vetoed-treatment loop)")


def test_quote_substantiated():
    """A flag's verbatim quote must be a non-trivial span really present in the source: a lone short
    word ("that", "held") no longer rubber-stamps a hallucinated flag, but a real phrase or a
    distinctive long single term still substantiates. The guard fails closed (substantiated -> held)."""
    src = "the court held that the temporary substitute provision did not apply to the borrowed car"
    assert update._quote_substantiated("that", src) is False, "lone short word rejected"
    assert update._quote_substantiated("held", src) is False, "lone short word rejected"
    assert update._quote_substantiated("", src) is False, "empty rejected"
    assert update._quote_substantiated("temporary substitute", src) is True, "real phrase substantiates"
    assert update._quote_substantiated("provision", src) is True, "distinctive long single term substantiates"
    assert update._quote_substantiated("mechanical breakdown clause", src) is False, "absent phrase is not substantiated"
    print("  ok  quote substantiation rejects lone short words, keeps real spans")


def test_party_tokens():
    """The three-char floor (lowered from four): common short surnames survive so an individual-vs-
    individual case's higher-court reappearance still clears the two-token dedup/escalation bar, while
    institution words and sub-three-char noise stay out."""
    assert update.party_tokens("Lee v. Cox") == {"lee", "cox"}, "short surnames retained at floor 3"
    assert update.party_tokens("Smith v. Jones") == {"smith", "jones"}, "normal surnames unchanged"
    assert update.party_tokens("In re Wu") == set(), "2-char tokens (in, re, wu) all out"
    assert update.party_tokens("State Farm Mutual v. Kim") == {"kim"}, "institution stopped -> one token"
    print("  ok  party tokens keep short surnames, drop institutions and <3-char noise")


def test_parse_json_extraction():
    """parse_json's fallback extracts the FIRST brace-balanced object even amid prose -- including
    trailing prose that itself contains braces (a greedy regex over-grabbed to invalid JSON) and a
    nested object (a lazy regex stopped at the first })."""
    assert update.parse_json('{"a": 1}') == {"a": 1}
    assert update.parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert update.parse_json('{"a": {"b": 1}} then prose {oops}') == {"a": {"b": 1}}, "nested + trailing-brace prose"
    assert update.parse_json('Sure! {"ok": true} hope that helps') == {"ok": True}, "leading + trailing prose"
    assert update._first_json_object('{"s": "a } b"}') == '{"s": "a } b"}', "brace inside a string is not counted"
    raised = False
    try:
        update.parse_json("no json here")
    except Exception:
        raised = True
    assert raised, "parse_json should raise when there is no object"
    print("  ok  json extraction: nested, prose-braces, string-braces, no-object")


def test_today_eastern():
    """_today_eastern is today's UTC date or the day before (Eastern is UTC-4/-5), never ahead -- so a
    late-Eastern-evening discovery is not stamped a day forward on the UTC runner."""
    import datetime as _dt
    te = update._today_eastern()
    utc = _dt.datetime.now(_dt.timezone.utc).date()
    assert te in (utc.isoformat(), (utc - _dt.timedelta(days=1)).isoformat()), te
    print("  ok  _today_eastern is UTC-today or the day before, never ahead")


def test_funnel_pr_body():
    """The run's PR/dry-run markdown assembly (update._funnel_pr_body), extracted verbatim from
    main(). Pure: given this run's accumulated results it must fold in each section -- the added card
    with its cross-check flag, the treatment flag on an existing card, and the dropped tail -- and, on
    an empty run, say so."""
    print("funnel PR body:")
    card = {"cluster_id": 4321, "name": "Alpha v. Beta", "court": "ctapp", "date": "2026-06-01",
            "dockets": ["A26A0001"], "areas": ["auto"], "disposition": "affirmed",
            "synopsis": "Held a thing.", "why": "It matters.", "url": "https://cl/4321/",
            "precedential": "published", "additional_holdings": []}
    body = update._funnel_pr_body(
        added=[card], flagged=[("Alpha v. Beta", ["low confidence"])],
        crosschecks={4321: {"verdict": "flag", "reason": "the synopsis overstates the holding"}},
        completeness={4321: {"verdict": "ok", "reason": ""}},
        treat_flags=[("Old Card v. State", "Alpha v. Beta", "overruled")],
        audit_notes=[], sa_events=[], skipped=[("Noise v. State", "screen: out of scope")])
    for needle in ("## Georgia Appellate Watch: 1 new opinion(s)", "Alpha v. Beta",
                   "cross-check FLAG: the synopsis overstates the holding", "review: low confidence",
                   "Treatment flags this run", "Old Card v. State", "Screened or dropped this run",
                   "Noise v. State"):
        assert needle in body, "missing %r in PR body" % needle
    assert body.endswith("\n"), "trailing newline preserved"
    print("  ok  full run folds in card, checks, treatment flag, and dropped tail")

    empty = update._funnel_pr_body([], [], {}, {}, [], [], [], [])
    assert "No new relevant opinions this run." in empty, empty
    assert empty.startswith("## Georgia Appellate Watch: 0 new opinion(s)"), empty
    print("  ok  empty run says 'no new relevant opinions'")


def test_guard_token_budget():
    """The guard output budget was a hardcoded 400 -- the smallest in the funnel, given to the
    two tiers that must quote source text VERBATIM. It fails SOFT: a truncated response becomes
    verdict "unavailable", not an error, so the guard silently stops guarding. On 2026-08-05
    the fidelity cross-check for Universal Property & Casualty burned all three attempts on
    "hit max_tokens (400); response truncated" and that card went un-cross-checked.

    Pin it here because nothing else would notice it shrinking back: no test asserts on a
    verdict that only appears when the budget is too small.
    """
    assert update.GUARD_TOKENS >= 1024, update.GUARD_TOKENS
    assert update.GUARD_TOKENS > 400, "400 is the value that truncated in production"
    # Both guard kinds build through one function, so both must get the budget.
    entry = {"areas": ["procedure"], "synopsis": "s", "why": "w", "disposition": "affirmed"}
    for kind in ("fidelity", "completeness"):
        body, ground = update.guard_request(kind, "Case v. Case", "opinion text", entry)
        assert body["max_tokens"] == update.GUARD_TOKENS, (kind, body["max_tokens"])
        assert "max_tokens" in body and body["max_tokens"] != 400, kind
    # It must be env-overridable like every other tier's budget, not a literal.
    src = open(os.path.join(HERE, "update.py"), encoding="utf-8").read()
    assert 'OPINIONS_GUARD_MAX_TOKENS' in src, "the budget should be settable without an edit"
    assert '"max_tokens": 400' not in src, "a hardcoded 400 is back somewhere"
    print("  ok  guard output budget is raised, shared by both kinds, and env-overridable")


def test_batch_carry_over():
    """A Message Batch is billed when Anthropic accepts it, not when we read it. Before this,
    every deferral abandoned a batch that had already run -- at opus-5 rates for summarize.
    The id must be recorded at SUBMIT time (not after the wait) and collected next run.

    Honest scope, asserted in the comments so it is not forgotten: this recovers DEFERRALS.
    A reclaimed run never reaches the commit step, so nothing reaches the state file.
    """
    real_run = update.batch.run
    seen = {}

    def submitting(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
        seen["resume_id"] = resume_id
        if on_submit:
            on_submit("bid_new")
        return {"a": {"ok": True, "text": "{}"}}

    def timing_out(reqs, deadline=None, interval=20.0, label="batch", resume_id=None, on_submit=None):
        if on_submit:
            on_submit("bid_deferred")
        raise update.batch.BatchTimeout("bid_deferred", "still running")

    try:
        # A completed batch leaves nothing behind.
        update._PENDING_BATCHES.clear(); update._RESUME_BATCHES.clear()
        update.batch.run = submitting
        out = update.batch_run(["r"], 0, "funnel-summarize", now=1000.0)
        assert out == {"a": {"ok": True, "text": "{}"}}, out
        assert update._PENDING_BATCHES == {}, update._PENDING_BATCHES
        assert seen["resume_id"] is None, seen
        print("  ok  a collected batch returns results and leaves nothing to carry")

        # A deferral still raises (callers degrade unchanged) but keeps the paid id.
        update.batch.run = timing_out
        update._PENDING_BATCHES.clear()
        raised = False
        try:
            update.batch_run(["r"], 0, "funnel-summarize", now=2000.0)
        except update.batch.BatchTimeout:
            raised = True
        assert raised, "BatchTimeout must still propagate to the caller"
        rec = update._PENDING_BATCHES.get("funnel-summarize") or {}
        assert rec.get("id") == "bid_deferred", rec
        assert rec.get("at") == 2000.0, rec
        print("  ok  a deferral still raises, and retains the billed batch id")

        # A deferral whose id arrives ONLY on the exception (on_submit never fired -- e.g. the
        # carried batch was still running, so nothing new was submitted) must still be held.
        # Mutation testing caught this path being unreachable from the happy-path test.
        def timing_out_silently(reqs, deadline=None, interval=20.0, label="batch",
                                resume_id=None, on_submit=None):
            raise update.batch.BatchTimeout("bid_from_exception", "still running")
        update.batch.run = timing_out_silently
        update._PENDING_BATCHES.clear()
        try:
            update.batch_run(["r"], 0, "funnel-triage", now=2500.0)
        except update.batch.BatchTimeout:
            pass
        assert (update._PENDING_BATCHES.get("funnel-triage") or {}).get("id") == "bid_from_exception", \
            update._PENDING_BATCHES
        print("  ok  an id carried only on the exception is still retained")

        # Restore the state the round-trip assertions below expect.
        update.batch.run = timing_out
        update._PENDING_BATCHES.clear()
        try:
            update.batch_run(["r"], 0, "funnel-summarize", now=2000.0)
        except update.batch.BatchTimeout:
            pass

        # The id round-trips through the committed state file.
        st = update.stamp_pending_batches({"seen_clusters": []})
        assert st["pending_batches"]["funnel-summarize"]["id"] == "bid_deferred", st
        back = update.load_pending_batches(st, now=2060.0)
        assert back == {"funnel-summarize": "bid_deferred"}, back
        print("  ok  the id round-trips through opinions_state.json")

        # Next run offers it as resume_id instead of paying for the same work twice.
        update._RESUME_BATCHES.clear(); update._RESUME_BATCHES.update(back)
        update._PENDING_BATCHES.clear()
        update.batch.run = submitting
        update.batch_run(["r"], 0, "funnel-summarize", now=3000.0)
        assert seen["resume_id"] == "bid_deferred", seen
        assert update._RESUME_BATCHES == {}, update._RESUME_BATCHES
        print("  ok  the carried id is offered as resume_id, and consumed once")

        # A healthy run leaves no trace, so the committed diff stays quiet.
        st2 = update.stamp_pending_batches({"pending_batches": {"stale": {"id": "x"}}})
        assert "pending_batches" not in st2, st2
        print("  ok  a clean run removes the key rather than committing an empty dict")

        # Too old to still be collectable -> dropped, not polled with the phase's whole budget.
        old = {"pending_batches": {"funnel-triage": {"id": "bid_old", "at": 1000.0}}}
        assert update.load_pending_batches(old, now=1000.0 + update.BATCH_CARRY_MAX_AGE_SEC + 1) == {}
        assert update.load_pending_batches(old, now=1060.0) == {"funnel-triage": "bid_old"}
        print("  ok  a stale id is dropped; one inside the window is carried")

        # Junk in a committed file must never crash the run that reads it.
        for junk in ({"pending_batches": {"a": "not-a-dict"}},
                     {"pending_batches": {"a": {"id": "x", "at": "nonsense"}}},
                     {"pending_batches": {"a": {"at": 1.0}}},
                     {"pending_batches": None},
                     {}):
            assert update.load_pending_batches(junk, now=1000.0) == {}, junk
        print("  ok  malformed carry-over state reads as empty instead of crashing")
    finally:
        update.batch.run = real_run
        update._PENDING_BATCHES.clear(); update._RESUME_BATCHES.clear()


def test_screen_caption_rule():
    """The screen judges from a caption and an opening excerpt, so a caption CONVENTION it
    misreads costs the feed whole cases silently. Nothing catches it either: update.py's tier 2.5
    smells TRIAGE-drop reasons only, and of 1,502 logged rejections the 1,163 screen drops are
    100% unaudited.

    Twelve adversarial 'In re: A v. B' captions were discarded this way, including two Supreme
    Court of Alabama insurance decisions: SC-2025-0918 (Ex parte State Farm Fire & Cas. Co.,
    'sharing' provisions in a protective order in a first-party bad-faith case), dropped as
    "dependency or juvenile proceeding ('In re' with minor names)" though no minor is named and
    State Farm is a party; and SC-2025-1015 (Ex parte Assn. of County Commissions of Ala.
    Liability Self-Insurance Fund, duty to defend), dropped as "dependency or receivership
    proceeding". In Alabama that prefix heads a mandamus petition and says nothing about subject.

    This pins the INSTRUCTION, not the model's behavior -- a prompt fix can only be guarded by
    asserting the prompt still carries it. It fails if the rule is edited back out.
    """
    sys_prompt = update.SCREEN_SYSTEM
    for phrase in ("A CAPTION IS NOT A CATEGORY", "Ex parte", "In the Matter of", "mandamus",
                   "PARTIES", "self-insurance fund"):
        assert phrase in sys_prompt, "screen prompt lost the caption-wrapper rule: %r" % phrase
    for hedge in ("likely", "appears to be", "suggests"):
        assert hedge in sys_prompt, "screen prompt no longer forbids the hedge %r" % hedge
    # It must stay DISCRIMINATING. A blanket "always pass In re" would readmit the probate and
    # rules-amendment captions the screen currently discards correctly.
    for marker in ("In the Interest of", "In re Estate of", "Rules", "initials"):
        assert marker in sys_prompt, "screen prompt lost the true subject marker %r" % marker
    # The exclusion list must still be closed; that is what makes "if you cannot, PASS" bite.
    assert "That list is CLOSED" in sys_prompt
    print("  ok  screen prompt separates caption wrappers from subject markers, and bans hedged reasons")


def main():
    print("crosscheck guardrails:")
    for c in CASES:
        run_case(*c)
    print("completeness guardrails:")
    for c in CASES_COMP:
        run_comp_case(*c)
    print("duplicate guard:")
    for c in CASES_DEDUP:
        run_dedup_case(*c)
    print("helpers:")
    test_substantiation_helper()
    test_docket_set()
    test_treatment_citer_seen()
    test_quote_substantiated()
    test_party_tokens()
    test_parse_json_extraction()
    test_today_eastern()
    test_draft_pending()
    test_guard_cards_batch()
    test_triage_batch()
    test_funnel_pr_body()
    test_guard_token_budget()
    test_screen_caption_rule()
    test_batch_carry_over()
    print("\nALL TESTS PASSED (%d cases)" % (len(CASES) + len(CASES_COMP) + len(CASES_DEDUP) + 14))
    return 0


if __name__ == "__main__":
    sys.exit(main())
