#!/usr/bin/env python3
"""Hermetic tests for the per-role reasoning effort every Anthropic request sends (no network, no
API key).

From 2026-09-23 model-watch judged an Opus candidate whose API default effort is `medium`
against an incumbent whose default is `high`, because no request named an effort at all, and
reported the gap as a regression. The fix is to send siteconfig.MODEL_EFFORT explicitly, in the
documented shape (`output_config: {"effort": ...}`, top-level, no beta header), from the request
builders production and the golden check share -- and only to a model documented to accept it,
since an effort sent to Haiku 4.5 is a 400.

The funnel tiers came first (PR #352). Every other call -- the fidelity and completeness guards,
the smell and treatment/authority audits, the Fable reviews, the treatment classifier, the four
watches, diagnose and dep_review -- still rode its model's API default, which is `high` for the
pins of today but not for the next Opus. test_every_role_default pins each role to the documented
default of the model it uses today (so production is unchanged), test_every_builder drives every
builder, and test_source_enumeration fails if a request body is written anywhere in scripts/
without going through update.with_effort.

  python scripts/test_effort.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import siteconfig  # noqa: E402
import update  # noqa: E402
import batch  # noqa: E402
import courtrules  # noqa: E402
import dep_review  # noqa: E402
import diagnose  # noqa: E402
import ethics  # noqa: E402
import fable_review  # noqa: E402
import legislation  # noqa: E402
import model_watch  # noqa: E402
import regulations  # noqa: E402
import treatment  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Pins:
    """Point the four tier pins at the given ids for one test, restoring afterwards."""
    def __init__(self, **pins):
        self.pins = pins

    def __enter__(self):
        self.saved = {k: getattr(update, k) for k in self.pins}
        for k, v in self.pins.items():
            setattr(update, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(update, k, v)
        return False


def test_support_rule():
    print("which models take an effort (the documented rule)")
    for mid in ("claude-opus-5", "claude-opus-5-5", "claude-opus-4-5", "claude-opus-4-8",
                "claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-4-5-20251101",
                "claude-fable-5", "claude-fable-5-1"):
        check("%s accepts effort high" % mid, update.effort_supported(mid, "high"))
    for mid in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-5", "claude-opus-4-1",
                "claude-3-5-sonnet-20241022", "", None, "claude-opus-5-preview", "claude-mythos-5"):
        check("%r gets no effort (documented no, or unconfirmed)" % (mid,), not update.effort_supported(mid, "high"))
    check("xhigh needs Opus 4.7+", update.effort_supported("claude-opus-4-7", "xhigh")
          and not update.effort_supported("claude-opus-4-6", "xhigh"))
    check("xhigh needs Sonnet 5+", update.effort_supported("claude-sonnet-5", "xhigh")
          and not update.effort_supported("claude-sonnet-4-6", "xhigh"))
    check("Opus 4.5 is documented for low/medium/high only",
          update.effort_supported("claude-opus-4-5", "high")
          and not update.effort_supported("claude-opus-4-5", "max")
          and not update.effort_supported("claude-opus-4-5", "xhigh"))
    check("an unknown level is never supported", not update.effort_supported("claude-opus-5", "turbo"))
    check("documented models answer True/False; older ones are a documented no",
          update.effort_documented("claude-opus-5") is True
          and update.effort_documented("claude-haiku-4-5") is False
          and update.effort_documented("claude-sonnet-4-5") is False)
    check("Fable 5 and 5.1 are documented through max",
          update.effort_supported("claude-fable-5", "max") and update.effort_supported("claude-fable-5-1", "xhigh"))
    for mid in ("claude-sonnet-5-5", "claude-opus-6", "claude-haiku-5", "claude-fable-6", "claude-mystery-1"):
        check("%s is not covered by the docs (None), never extrapolated" % mid,
              update.effort_documented(mid) is None)


class Caps:
    """Stub the Models API capabilities lookup (hermetic: no network even with a key set)."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __enter__(self):
        self.saved = (update.fetch_capabilities, dict(update._MODEL_CAPS), set(update._EFFORT_WARNED))
        update._MODEL_CAPS.clear()
        update._EFFORT_WARNED.clear()

        def fake(model):
            self.calls.append(model)
            return self.answers.get(model)
        update.fetch_capabilities = fake
        return self

    def __exit__(self, *exc):
        update.fetch_capabilities = self.saved[0]
        update._MODEL_CAPS.clear()
        update._MODEL_CAPS.update(self.saved[1])
        update._EFFORT_WARNED.clear()
        update._EFFORT_WARNED.update(self.saved[2])
        return False


def test_undocumented_models():
    """A model the docs do not cover (the Sonnet 5.5 candidate) gets effort ONLY when the Models API
    confirms it. Docs silent and API silent: no effort, and a logged warning that the comparison
    may be unmatched -- never an extrapolation from "Sonnet 4.6+"."""
    print("models the docs do not cover")
    yes = {"effort": {"supported": True, "high": {"supported": True}}}
    with Caps({"claude-sonnet-5-5": yes}) as c:
        check("the API confirms effort: it is sent", update.effort_supported("claude-sonnet-5-5", "high"))
        update.effort_supported("claude-sonnet-5-5", "high")
        check("...and the API is asked once per process", c.calls == ["claude-sonnet-5-5"], str(c.calls))
        check("documented models never hit the API", update.effort_supported("claude-opus-5", "high")
              and c.calls == ["claude-sonnet-5-5"], str(c.calls))
    with Caps({"claude-sonnet-5-5": {"effort": {"supported": True, "high": {"supported": False}}}}):
        check("the API denies the level: none sent", not update.effort_supported("claude-sonnet-5-5", "high"))
    with Caps({"claude-sonnet-5-5": {"effort": {"supported": False}}}):
        check("the API denies effort: none sent", not update.effort_supported("claude-sonnet-5-5", "high"))
    import contextlib
    import io
    for label, answer in (("no capabilities object", None), ("capabilities without effort", {"vision": {}})):
        with Caps({"claude-sonnet-5-5": answer}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = update.effort_supported("claude-sonnet-5-5", "high")
                update.effort_supported("claude-sonnet-5-5", "high")
            check("%s: no effort is sent" % label, got is False)
            log = buf.getvalue()
            check("%s: it is logged once, naming the unmatched comparison" % label,
                  log.count("claude-sonnet-5-5") == 1 and "unmatched" in log, log)
    with Caps({}), Pins(TRIAGE_MODEL="claude-sonnet-5-5"):
        t = update.triage_request("A v. B", "S1", "text")
    check("an unconfirmed triage candidate is sent no output_config", "output_config" not in t, str(t.keys()))
    with Caps({"claude-sonnet-5-5": yes}), Pins(TRIAGE_MODEL="claude-sonnet-5-5"):
        t = update.triage_request("A v. B", "S1", "text")
    check("a confirmed one is sent the tier's effort", t.get("output_config") == {"effort": "high"},
          str(t.get("output_config")))
    with Caps({}):
        update.remember_capabilities("claude-sonnet-5-5", yes)
        check("capabilities model_watch already listed are reused", update.effort_supported("claude-sonnet-5-5"))
    saved_key = update.KEY
    try:
        update.KEY = ""
        check("with no API key the lookup is skipped (None)", update.fetch_capabilities("claude-sonnet-5-5") is None)
    finally:
        update.KEY = saved_key


def test_config():
    print("siteconfig.MODEL_EFFORT")
    check("every request role has an entry",
          set(siteconfig.MODEL_EFFORT) == set(ROLE_PINS), str(sorted(set(siteconfig.MODEL_EFFORT) ^ set(ROLE_PINS))))
    check("summarize and triage pin high (the incumbents' documented default: no change today)",
          siteconfig.MODEL_EFFORT["summarize"] == "high" and siteconfig.MODEL_EFFORT["triage"] == "high")
    check("the Haiku tiers send none", not siteconfig.MODEL_EFFORT["screen"]
          and not siteconfig.MODEL_EFFORT["pretriage"])
    check("keyed by role, never by a model id (a bump would leave an id key behind)",
          not any(k.startswith("claude-") for k in siteconfig.MODEL_EFFORT))


def test_request_shapes():
    print("the request builders")
    with Pins(MODEL="claude-opus-5-5", TRIAGE_MODEL="claude-sonnet-5",
              SCREEN_MODEL="claude-haiku-4-5", PRETRIAGE_MODEL="claude-haiku-4-5"):
        s = update.summarize_request("ga", "A v. B", "S1", "2026-01-01", "text", "note")
        t = update.triage_request("A v. B", "S1", "text")
        sc = update.screen_request("A v. B", "S1", "text")
        pt = update.pretriage_request("A v. B", "S1", "text")
    check("summarize sends output_config.effort high", s.get("output_config") == {"effort": "high"}, str(s.get("output_config")))
    check("triage sends output_config.effort high", t.get("output_config") == {"effort": "high"}, str(t.get("output_config")))
    check("no top-level `effort` key (not the documented shape)", "effort" not in s and "effort" not in t)
    check("the Haiku screen sends no output_config", "output_config" not in sc, str(sc.keys()))
    check("the Haiku pretriage sends no output_config", "output_config" not in pt, str(pt.keys()))
    check("no thinking, temperature or other sampling field is added",
          not ({"thinking", "temperature", "top_p", "top_k"} & (set(s) | set(t) | set(sc) | set(pt))))

    with Caps({}), Pins(MODEL="claude-mystery-1", TRIAGE_MODEL="claude-haiku-4-5"):
        s = update.summarize_request("ga", "A v. B", "S1", "2026-01-01", "text", "note")
        t = update.triage_request("A v. B", "S1", "text")
    check("an undocumented model gets no effort (its default, the old behaviour)",
          "output_config" not in s and "output_config" not in t)

    saved = dict(siteconfig.MODEL_EFFORT)
    try:
        siteconfig.MODEL_EFFORT["screen"] = "high"
        with Pins(SCREEN_MODEL="claude-haiku-4-5"):
            sc = update.screen_request("A v. B", "S1", "text")
        check("even if configured, Haiku 4.5 is never sent an effort (it 400s)", "output_config" not in sc)
    finally:
        siteconfig.MODEL_EFFORT.clear()
        siteconfig.MODEL_EFFORT.update(saved)


def test_batch_carries_it():
    print("the batch path")
    with Pins(MODEL="claude-opus-5", TRIAGE_MODEL="claude-sonnet-5"):
        line = batch.from_body("c1", update.summarize_request("ga", "A", "S", "d", "t", "n"))
        tline = batch.from_body("c2", update.triage_request("A", "S", "t"))
    check("a batched summarize carries the same output_config", line["params"].get("output_config") == {"effort": "high"})
    check("a batched triage carries the same output_config", tline["params"].get("output_config") == {"effort": "high"})


def test_sync_path_sends_it():
    print("the synchronous path")
    sent = []
    orig = update.anthropic_json
    update.anthropic_json = lambda body, label="call": sent.append((label, body)) or {"areas": []}
    try:
        with Pins(MODEL="claude-opus-5", TRIAGE_MODEL="claude-sonnet-5", SCREEN_MODEL="claude-haiku-4-5",
                  PRETRIAGE_MODEL="claude-haiku-4-5"):
            update.summarize("ga", "A", "S", "d", "t", "n")
            update.triage("A", "S", "t")
            update.screen("A", "S", "t")
            update.pretriage("A", "S", "t")
    finally:
        update.anthropic_json = orig
    got = {label: body.get("output_config") for label, body in sent}
    check("summarize() and triage() post the effort; screen() and pretriage() do not",
          got == {"summarize": {"effort": "high"}, "triage": {"effort": "high"},
                  "screen": None, "pretriage": None}, str(got))


# --- every request role, not just the funnel tiers -----------------------------------------------

# The model each role sends to today, read live from the module that pins it (so a model_watch bump
# or an env override is what is checked, not a copy).
ROLE_PINS = {
    "summarize":          lambda: update.MODEL,
    "triage":             lambda: update.TRIAGE_MODEL,
    "pretriage":          lambda: update.PRETRIAGE_MODEL,
    "screen":             lambda: update.SCREEN_MODEL,
    "guard_fidelity":     lambda: update.CROSSCHECK_MODEL,
    "guard_completeness": lambda: update.COMPLETENESS_MODEL,
    "smell":              lambda: update.SMELL_MODEL,
    "treatment_audit":    lambda: update.AUDIT_MODEL,
    "authority_audit":    lambda: update.AUDIT_MODEL,
    "fable_review":       lambda: update.FABLE_MODEL,
    "treatment":          lambda: treatment.MODEL,
    "courtrules_extract": lambda: courtrules.MODEL,
    "ethics_extract":     lambda: ethics.MODEL,
    "leg_screen":         lambda: legislation.SCREEN_MODEL,
    "leg_recall":         lambda: legislation.RECALL_MODEL,
    "leg_write":          lambda: legislation.WRITE_MODEL,
    "reg_screen":         lambda: regulations.SCREEN_MODEL,
    "reg_write":          lambda: regulations.WRITE_MODEL,
    "diagnose":           lambda: diagnose.MODEL,
    "dep_review":         lambda: dep_review.MODEL,
}

# The API's default effort for each model pinned today, from the bundled API docs: Opus 5 "The API
# default is high"; Sonnet 5 high; Fable 5 "Default is high" (and Fable 5.1, same API surface,
# "Start with high (the default)"); Haiku 4.5 takes no effort parameter (""). Sending a model its
# default is the same as omitting it, so pinning these is what keeps production unchanged.
API_DEFAULT_EFFORT = {"claude-opus-5": "high", "claude-sonnet-5": "high", "claude-fable-5": "high",
                      "claude-haiku-4-5": ""}


def test_every_role_default():
    print("each role pins the documented default of the model it uses today")
    for role, pin in sorted(ROLE_PINS.items()):
        model = pin()
        if model not in API_DEFAULT_EFFORT:
            print("  skip %s: pinned to %r here (an env override?), not a model this test knows" % (role, model))
            continue
        want = API_DEFAULT_EFFORT[model]
        check("%s (%s) asks for %r" % (role, model, want), siteconfig.MODEL_EFFORT.get(role) == want,
              repr(siteconfig.MODEL_EFFORT.get(role)))
        check("%s (%s) sends %r" % (role, model, want), update.effort_level(role, model) == want,
              repr(update.effort_level(role, model)))


def test_with_effort_merges():
    print("update.with_effort")
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    body = {"model": "claude-opus-5", "max_tokens": 10, "system": "s",
            "messages": [{"role": "user", "content": "u"}], "output_config": {"format": fmt}}
    out = update.with_effort("summarize", body)
    check("merges into an existing output_config (format survives)",
          out.get("output_config") == {"format": fmt, "effort": "high"}, str(out.get("output_config")))
    check("does not mutate the caller's body", body["output_config"] == {"format": fmt})
    bare = {"model": "claude-opus-5", "max_tokens": 10, "system": "s", "messages": []}
    out = update.with_effort("summarize", bare)
    check("appends output_config last on a body without one (the PR #352 key order)",
          list(out) == ["model", "max_tokens", "system", "messages", "output_config"], str(list(out)))
    haiku = dict(bare, model="claude-haiku-4-5")
    check("Haiku: the body comes back unchanged", update.with_effort("summarize", haiku) == haiku)
    off = dict(bare, model="")
    check("a disabled \"\" pin: unchanged", update.with_effort("guard_fidelity", off) == off)
    check("an unknown role sends nothing", update.with_effort("no-such-role", bare) == bare)


class _Stub:
    """A call seam that records each body and answers with a harmless dict."""
    def __init__(self, answer=None):
        self.bodies, self.answer = [], answer or {}

    def __call__(self, body, label="call"):
        self.bodies.append((label, body))
        return dict(self.answer)


def _all_bodies(model):
    """{role: request body} from every request builder in scripts/, each pointed at `model`. The
    synchronous call sites are driven through a stubbed transport, so this is what would be POSTed."""
    out = {}
    saved_upd = {k: getattr(update, k) for k in ("MODEL", "TRIAGE_MODEL", "SCREEN_MODEL", "PRETRIAGE_MODEL",
                                                 "SMELL_MODEL", "AUDIT_MODEL", "CROSSCHECK_MODEL",
                                                 "COMPLETENESS_MODEL", "anthropic_json", "CROSSCHECK_TRIES",
                                                 "COMPLETENESS_TRIES")}
    saved_tr = treatment.MODEL
    sync = _Stub()
    try:
        for k in saved_upd:
            if k.endswith("MODEL"):
                setattr(update, k, model)
        update.CROSSCHECK_TRIES = update.COMPLETENESS_TRIES = 1
        update.anthropic_json = sync
        treatment.MODEL = model
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            out["screen"] = update.screen_request("A v. B", "S1", "text")
            out["pretriage"] = update.pretriage_request("A v. B", "S1", "text")
            out["triage"] = update.triage_request("A v. B", "S1", "text")
            out["summarize"] = update.summarize_request("ga", "A v. B", "S1", "2026-01-01", "text", "note")
            out["smell"] = update.smell_request([{"name": "A v. B", "reason": "off topic"}])
            entry = {"areas": ["insurance"], "synopsis": "s", "why": "w"}
            out["guard_fidelity"] = update.guard_request("fidelity", "A v. B", "text", entry)[0]
            out["guard_completeness"] = update.guard_request("completeness", "A v. B", "text", entry)[0]
            update.crosscheck("A v. B", "text", entry)
            update.completeness_check("A v. B", "text", entry)
            update.treatment_audit("C v. D", "text", {"name": "A v. B"})
            update.authority_audit("C v. D", "text", "A v. B")
            fab = _Stub()
            fable_review.review_held(entry, ["flag"], "opinion text " * 100, fab, model=model)
            fable_review.review_published(entry, ["flag"], "opinion text " * 100, fab, model=model)
            out["treatment"] = treatment.classify_request({"name": "A v. B"}, "C v. D", "text")
            out["courtrules_extract"] = courtrules._extract_body("page", model)
            out["ethics_extract"] = ethics._extract_body("page", model)
            leg = _Stub({"relevant": False, "suspect": False})
            legislation.screen_bill({"number": "HB 1", "title": "t"}, leg, model=model)
            legislation.recall_drop({"brief": "b", "reason": "r"}, leg, model=model)
            out["leg_write"] = legislation._write_body({"number": "HB 1", "title": "t"}, model=model)
            reg = _Stub({"relevant": False})
            regulations.screen_doc({"title": "t"}, reg, model=model)
            out["reg_write"] = regulations._write_body({"title": "t"}, model=model)
            out["diagnose"] = diagnose.build_request("t", "b", "", model)
            out["dep_review"] = dep_review.build_request("dep", "1", "2", "body", "usage", model)
    finally:
        for k, v in saved_upd.items():
            setattr(update, k, v)
        treatment.MODEL = saved_tr
    by_label = {"crosscheck": "guard_fidelity_sync", "completeness": "guard_completeness_sync",
                "treatment-audit": "treatment_audit", "authority-audit": "authority_audit",
                "fable-review": "fable_review", "fable-review-published": "fable_review_published",
                "leg-screen": "leg_screen", "leg-recall": "leg_recall", "reg-screen": "reg_screen"}
    for stub in (sync, fab, leg, reg):
        for label, body in stub.bodies:
            out[by_label[label]] = body
    return out


def _role_of(key):
    return key[:-len("_sync")] if key.endswith("_sync") else ("fable_review" if key == "fable_review_published"
                                                              else key)


def test_every_builder():
    print("every request builder attaches its own role's effort")
    saved = dict(siteconfig.MODEL_EFFORT)
    try:
        bodies = _all_bodies("claude-opus-5")
        check("every role in siteconfig.MODEL_EFFORT is driven by this test",
              {_role_of(k) for k in bodies} == set(saved), str(sorted({_role_of(k) for k in bodies} ^ set(saved))))
        for key, body in sorted(bodies.items()):
            role = _role_of(key)
            want = saved.get(role)
            got = (body.get("output_config") or {}).get("effort", "")
            check("%s on Opus 5 sends %r (siteconfig)" % (key, want), got == want, repr(got))
            check("%s adds no thinking/sampling field" % key,
                  not ({"thinking", "temperature", "top_p", "top_k", "effort"} & set(body)), str(sorted(body)))
            line = batch.from_body("c", body)
            check("%s: a batch line carries the same output_config" % key,
                  line["params"].get("output_config") == body.get("output_config"))
        # Each builder must read ITS role, not a neighbour's: turn one role on at a time.
        for role in sorted(saved):
            siteconfig.MODEL_EFFORT.clear()
            siteconfig.MODEL_EFFORT.update({r: "" for r in saved})
            siteconfig.MODEL_EFFORT[role] = "medium"
            sent = sorted(k for k, b in _all_bodies("claude-opus-5").items() if b.get("output_config"))
            check("only %s's request(s) follow MODEL_EFFORT[%r]" % (role, role),
                  sent and all(_role_of(k) == role for k in sent), str(sent))
        siteconfig.MODEL_EFFORT.clear()
        siteconfig.MODEL_EFFORT.update({r: "high" for r in saved})
        leaked = sorted(k for k, b in _all_bodies("claude-haiku-4-5").items() if "output_config" in b)
        check("pointed at Haiku 4.5, no request sends an effort even when every role asks", not leaked, str(leaked))
    finally:
        siteconfig.MODEL_EFFORT.clear()
        siteconfig.MODEL_EFFORT.update(saved)


_TRANSPORT = {"batch.py"}   # builds batch lines FROM a finished body (batch.from_body); not a builder


def test_source_enumeration():
    """Static: every Messages body literal in scripts/ (a dict with both "max_tokens" and "messages")
    is written as the direct argument of with_effort / _with_effort, and every role it names is a
    MODEL_EFFORT key. A new call site that forgets the effort fails here before it ships."""
    print("every request body in scripts/ goes through with_effort")
    import ast
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    roles, bare, unknown = set(), [], []
    for path in sorted(glob.glob(os.path.join(here, "*.py"))):
        name = os.path.basename(path)
        if name.startswith(("test_", "stress_")) or name in _TRANSPORT:
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "effort_params" \
                    and name != "update.py":
                bare.append("%s:%d effort_params() outside update.with_effort" % (name, node.lineno))
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "output_config" in keys and name != "update.py":
                bare.append("%s:%d writes output_config by hand" % (name, node.lineno))
            if not {"max_tokens", "messages"} <= keys:
                continue
            call = parent.get(node)
            fname = getattr(getattr(call, "func", None), "id", None) or getattr(getattr(call, "func", None), "attr", None)
            if not (isinstance(call, ast.Call) and fname in ("with_effort", "_with_effort")
                    and len(call.args) == 2 and call.args[1] is node):
                bare.append("%s:%d" % (name, node.lineno))
                continue
            role = call.args[0]
            if isinstance(role, ast.Constant) and isinstance(role.value, str):
                roles.add(role.value)
            elif isinstance(role, ast.Subscript) and getattr(role.value, "id", "") == "GUARD_ROLES":
                roles.update(update.GUARD_ROLES.values())
            else:
                unknown.append("%s:%d role %s" % (name, node.lineno, ast.dump(role)[:60]))
    check("no request body bypasses with_effort", not bare, "; ".join(bare))
    check("every with_effort role is a literal (or GUARD_ROLES)", not unknown, "; ".join(unknown))
    check("every role named at a call site is configured", roles <= set(siteconfig.MODEL_EFFORT),
          str(sorted(roles - set(siteconfig.MODEL_EFFORT))))
    check("every configured role has a call site", set(siteconfig.MODEL_EFFORT) <= roles,
          str(sorted(set(siteconfig.MODEL_EFFORT) - roles)))


def test_model_watch_covers_roles():
    print("model_watch cross-checks every role a bump moves")
    listed = [r for roles in model_watch.TIER_ROLES.values() for r in roles]
    fable = {"fable_review", "diagnose", "dep_review"}   # Fable pins: never bumped by model_watch
    check("TIER_ROLES lists each non-Fable role exactly once",
          sorted(listed) == sorted(set(siteconfig.MODEL_EFFORT) - fable), str(sorted(listed)))
    for tier, roles in sorted(model_watch.TIER_ROLES.items()):
        for role in roles:
            pin = ROLE_PINS[role]()
            if pin not in API_DEFAULT_EFFORT:
                continue   # an env override in this shell; nothing to compare
            check("%s runs on the %s pin" % (role, tier), pin == model_watch.TIER_PINS[tier],
                  "%s vs %s" % (pin, model_watch.TIER_PINS[tier]))


def main():
    print("effort:")
    # Hermetic: no test may reach the Models API, even when ANTHROPIC_API_KEY is set.
    update.fetch_capabilities = lambda model: None
    for t in (test_support_rule, test_undocumented_models, test_config, test_request_shapes, test_batch_carries_it,
              test_sync_path_sends_it, test_every_role_default, test_with_effort_merges, test_every_builder,
              test_source_enumeration, test_model_watch_covers_roles):
        t()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
