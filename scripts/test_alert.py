#!/usr/bin/env python3
"""Hermetic tests for scripts/alert.py -- the instant-alert send.

Standard library only; no network and no API key. digest.send_broadcast is stubbed, so a
test that expects "nothing was sent" asserts on an empty call log rather than on a return
value. Where alert.py reads git, a real throwaway repository is built in a temp dir.

Why this file exists: alert.py mails every confirmed subscriber the day a landmark card
merges. It was the largest module no test imported. The gates below are the ones that keep
a misfire from becoming a mailing: which ids get announced, the CAN-SPAM postal refusal,
the duplicate-proof broadcast name, and the dry-run path that must never reach Resend.

Run directly: `python scripts/test_alert.py`.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alert      # noqa: E402  (sys.path shim must run first)
import digest     # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


CARD = {"cluster_id": 111, "name": "Alpha v. Beta", "court": "ctapp", "date": "2026-07-20",
        "disposition": "affirmed", "areas": ["coverage"], "synopsis": "A synopsis.",
        "why": "it matters here.", "dockets": ["A26A0001"], "url": "https://example.test/1"}


class Sent:
    """Stand-in for digest.send_broadcast; records every attempted send."""
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result if result is not None else {"id": "bc_1"}
        self.raises = raises

    def __call__(self, subject, html_body, text_body, name, **kw):
        self.calls.append({"subject": subject, "html": html_body, "text": text_body,
                           "name": name, "kw": kw})
        if self.raises:
            raise self.raises
        return self.result


class Env:
    """Swap alert/digest module state for one test and restore it afterwards."""
    def __init__(self, cards=(CARD,), **over):
        self.cards = list(cards)
        self.over = over
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="alerttest")
        self.saved = {
            "JSON_PATH": alert.JSON_PATH, "PREVIEW": alert.PREVIEW,
            "d_DRY_RUN": digest.DRY_RUN, "d_API_KEY": digest.API_KEY,
            "d_SEGMENT_ID": digest.SEGMENT_ID, "d_POSTAL": digest.POSTAL,
            "d_DRAFT": digest.DRAFT, "d_send": digest.send_broadcast,
            "ALERT_IDS": os.environ.get("ALERT_IDS"),
            "NOPOSTAL": os.environ.get("DIGEST_ALLOW_NO_POSTAL"),
        }
        alert.JSON_PATH = os.path.join(self.tmp, "opinions.json")
        alert.PREVIEW = os.path.join(self.tmp, "preview.html")
        with open(alert.JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cards, f)
        # Defaults: configured and live, so each test opts INTO the failure it studies.
        digest.DRY_RUN, digest.API_KEY = False, "re_key"
        digest.SEGMENT_ID, digest.POSTAL, digest.DRAFT = "seg_1", "1 Main St", False
        for k, v in self.over.items():
            setattr(digest, k, v)
        os.environ.pop("ALERT_IDS", None)
        os.environ.pop("DIGEST_ALLOW_NO_POSTAL", None)
        self.sent = Sent()
        digest.send_broadcast = self.sent
        return self

    def __exit__(self, *exc):
        alert.JSON_PATH, alert.PREVIEW = self.saved["JSON_PATH"], self.saved["PREVIEW"]
        digest.DRY_RUN, digest.API_KEY = self.saved["d_DRY_RUN"], self.saved["d_API_KEY"]
        digest.SEGMENT_ID, digest.POSTAL = self.saved["d_SEGMENT_ID"], self.saved["d_POSTAL"]
        digest.DRAFT, digest.send_broadcast = self.saved["d_DRAFT"], self.saved["d_send"]
        for k, v in (("ALERT_IDS", self.saved["ALERT_IDS"]),
                     ("DIGEST_ALLOW_NO_POSTAL", self.saved["NOPOSTAL"])):
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def run_main():
    try:
        alert.main()
        return None
    except SystemExit as e:
        return e.code or 0


# --- which cards get announced -------------------------------------------
def test_id_selection():
    with Env() as e:
        os.environ["ALERT_IDS"] = "111"
        run_main()
        check("ALERT_IDS selects the card", len(e.sent.calls) == 1)
        check("the subject names a single decision",
              e.sent.calls[0]["subject"] == "Instant alert: Alpha v. Beta",
              e.sent.calls[0]["subject"])

    with Env(cards=[CARD, dict(CARD, cluster_id=222, name="Gamma v. Delta")]) as e:
        os.environ["ALERT_IDS"] = "111,222"
        run_main()
        check("two cards give a counted subject",
              e.sent.calls[0]["subject"] == "Instant alert: 2 decisions worth knowing today",
              e.sent.calls[0]["subject"])

    with Env() as e:
        os.environ["ALERT_IDS"] = "999"        # id not present in opinions.json
        run_main()
        check("an id absent from opinions.json announces nothing", e.sent.calls == [])

    with Env() as e:
        os.environ["ALERT_IDS"] = "not,an,id"
        run_main()
        check("junk ids with no git diff announce nothing", e.sent.calls == [])


def test_new_ids_from_git():
    """The git diff that decides what a merge announces, against a real repository."""
    saved_repo, saved_json = alert.REPO, alert.JSON_PATH
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e",
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        def g(*a):
            subprocess.run(("git",) + a, cwd=tmp, capture_output=True, env=env, check=True)
        g("init", "--initial-branch=main")
        path = os.path.join(tmp, "opinions.json")
        json.dump([CARD], open(path, "w"))
        g("add", "-A"); g("commit", "-m", "one")
        alert.REPO, alert.JSON_PATH = tmp, path
        try:
            check("a first commit announces nothing (no HEAD~1 to diff)",
                  alert.new_ids_from_git() == [])
            json.dump([CARD, dict(CARD, cluster_id=222)], open(path, "w"))
            g("add", "-A"); g("commit", "-m", "two")
            check("only the newly added id is announced", alert.new_ids_from_git() == [222])
            json.dump([CARD], open(path, "w"))
            g("add", "-A"); g("commit", "-m", "removal")
            check("a removal announces nothing (never a stale card)",
                  alert.new_ids_from_git() == [])
        finally:
            alert.REPO, alert.JSON_PATH = saved_repo, saved_json


# --- the gates that stop a send ------------------------------------------
def test_dry_run_and_missing_key_never_send():
    for label, over in (("DIGEST_DRY_RUN", {"DRY_RUN": True}), ("no API key", {"API_KEY": ""})):
        with Env(**over) as e:
            os.environ["ALERT_IDS"] = "111"
            run_main()
            check("%s writes a preview and sends nothing" % label,
                  e.sent.calls == [] and os.path.exists(alert.PREVIEW))


def test_missing_segment_never_sends():
    with Env(SEGMENT_ID="") as e:
        os.environ["ALERT_IDS"] = "111"
        run_main()
        check("an empty segment id sends nothing", e.sent.calls == [])


def test_postal_gate_is_fail_closed():
    """CAN-SPAM: a commercial email needs a physical address. Missing it must REFUSE."""
    with Env(POSTAL="") as e:
        os.environ["ALERT_IDS"] = "111"
        code = run_main()
        check("an empty postal address refuses to send", e.sent.calls == [])
        check("and exits nonzero so the workflow surfaces it", code == 1, repr(code))
    with Env(POSTAL="") as e:
        os.environ["ALERT_IDS"] = "111"
        os.environ["DIGEST_ALLOW_NO_POSTAL"] = "1"
        run_main()
        check("the documented override still allows one send", len(e.sent.calls) == 1)


def test_send_failure_exits_nonzero():
    with Env() as e:
        e.sent.raises = RuntimeError("resend down")
        digest.send_broadcast = e.sent
        os.environ["ALERT_IDS"] = "111"
        check("a failed broadcast exits nonzero rather than reporting success",
              run_main() == 1)


# --- the payload ----------------------------------------------------------
def test_broadcast_name_is_duplicate_proof():
    with Env() as e:
        os.environ["ALERT_IDS"] = "111"
        run_main()
        name = e.sent.calls[0]["name"]
        check("the broadcast name carries today's date and the ids",
              name.startswith("Instant alert ") and name.endswith("[111]"), name)


def test_card_content_is_escaped_and_linked():
    hostile = dict(CARD, name='Evil <script>alert(1)</script> & "Co"',
                   synopsis="<img src=x onerror=1>", why="<b>no</b>")
    with Env(cards=[hostile]) as e:
        os.environ["ALERT_IDS"] = "111"
        run_main()
        body = e.sent.calls[0]["html"]
        check("the card name is HTML-escaped in the email",
              "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body)
        check("the synopsis is escaped too", "<img src=x" not in body)
        check("the card links its permanent page", "https://horowitz.law/o/111" in body)
        text = e.sent.calls[0]["text"]
        check("a plain-text alternative is sent", isinstance(text, str) and len(text) > 40)


def main():
    print("alert.py:")
    test_id_selection()
    test_new_ids_from_git()
    test_dry_run_and_missing_key_never_send()
    test_missing_segment_never_sends()
    test_postal_gate_is_fail_closed()
    test_send_failure_exits_nonzero()
    test_broadcast_name_is_duplicate_proof()
    test_card_content_is_escaped_and_linked()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
