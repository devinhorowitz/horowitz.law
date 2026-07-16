#!/usr/bin/env python3
"""Adversarial-input stress harness for update.py's INGESTION seams -- the boundary where the daily
funnel consumes bytes it does not control: CourtListener court feeds (Atom XML), opinion PDFs, and
model responses. render/treatment were hardened against our own opinions.json; this covers the data
that arrives from OUTSIDE, which is the likelier source of a surprise.

No network. Each seam is driven directly with fuzzed / hostile input and stubbed transport:

  - _parse_feed: valid-but-hostile Atom (missing links, look-alike hosts, HTML/entities in summary,
    missing dates, unicode, huge fields) must never crash and must yield only well-formed candidate
    dicts (int cluster_id, str fields); malformed XML may raise (feed_court catches it) but must never
    return corrupt data. SECURITY: a look-alike host (courtlistener.com.evil.com, evilcourtlistener.com)
    must NOT be treated as CourtListener -- its href is kept verbatim, never presented as a site-
    relative CL path.
  - parse_json: fenced / embedded / truncated / garbage model output returns a JSON value or raises a
    JSONDecodeError -- never any other exception, never a hang.
  - pdf_text: url-gates non-http, swallows every download error AND every pypdf failure (a fake pypdf
    is injected to exercise the parse branch, since a real one is optional), always returning a str.
  - shape/dedup helpers (cluster_id_of, opinion_ids_of, _docket_set, _dup_sig, party_tokens,
    _normalize_for_match, clip, _pdf_ok, snippet_of): a battery of hostile `r` dicts / strings must
    never crash the candidate-shaping and duplicate guard.

Run directly: `python scripts/stress_ingest.py [fuzz-iterations]`. Exits nonzero on any failure.
"""
import json
import os
import random
import sys
import types
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


A = "{http://www.w3.org/2005/Atom}"


def feed_bytes(entries):
    """Build a well-formed Atom feed (bytes) from a list of entry-XML fragments."""
    body = "".join(entries)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<feed xmlns="http://www.w3.org/2005/Atom">%s</feed>' % body).encode("utf-8")


def entry(href=None, pdf=None, title="Doe v. Roe", published="2026-06-01T00:00:00Z",
          summary="A holding about the case. Docket A26A0123.", enclosure_type="application/pdf"):
    parts = ["<entry>"]
    if title is not None:
        parts.append("<title>%s</title>" % title)
    if href is not None:
        parts.append('<link rel="alternate" href="%s"/>' % href)
    if pdf is not None:
        parts.append('<link rel="enclosure" type="%s" href="%s"/>' % (enclosure_type, pdf))
    if published is not None:
        parts.append("<published>%s</published>" % published)
    if summary is not None:
        parts.append("<summary>%s</summary>" % summary)
    parts.append("</entry>")
    return "".join(parts)


def well_formed(cands, court):
    """Every candidate _parse_feed emits must be safe for the downstream pipeline to consume."""
    for c in cands:
        if not isinstance(c.get("cluster_id"), int):
            return "cluster_id not int: %r" % c.get("cluster_id")
        for k in ("caseName", "court_id", "absolute_url", "dateFiled", "docketNumber", "snippet", "pdf_url"):
            if not isinstance(c.get(k), str):
                return "%s not str: %r" % (k, c.get(k))
        if c["court_id"] != court:
            return "court_id mismatch"
    return ""


def test_parse_feed():
    court = update.COURTS_ALL[0]

    # A genuine CL alternate link is relativized to a site path; a look-alike host is kept verbatim.
    cl = "https://www.courtlistener.com/opinion/123/doe-v-roe/"
    cands = update._parse_feed(feed_bytes([entry(href=cl, pdf="https://storage.courtlistener.com/x.pdf")]), court)
    check("parse_feed: one clean CL entry -> one candidate", len(cands) == 1 and cands[0]["cluster_id"] == 123)
    check("parse_feed: genuine CL host is relativized to a site path",
          cands and cands[0]["absolute_url"] == "/opinion/123/doe-v-roe/", "%r" % (cands and cands[0]["absolute_url"]))

    for eviction, evil in (("subdomain-suffix", "https://courtlistener.com.evil.com/opinion/123/x/"),
                           ("prefix-lookalike", "https://evilcourtlistener.com/opinion/123/x/"),
                           ("userinfo-trick", "https://courtlistener.com@evil.com/opinion/123/x/"),
                           ("plain-other-host", "http://example.com/opinion/123/x/")):
        c = update._parse_feed(feed_bytes([entry(href=evil)]), court)
        # It still parses (the id is in the path), but the absolute_url must remain the FULL hostile
        # url -- never a bare "/opinion/..." path that a template could render as an on-site CL link.
        ok = c and c[0]["absolute_url"] == evil and not c[0]["absolute_url"].startswith("/")
        check("parse_feed: look-alike host (%s) is NOT treated as CourtListener" % eviction, bool(ok),
              "%r" % (c and c[0]["absolute_url"]))

    # A grab-bag of valid-but-hostile entries: all must parse without crashing and yield clean dicts.
    hostile = [
        entry(href=None),                                              # no alternate link -> skipped
        entry(href="https://www.courtlistener.com/docket/9/x/"),       # no /opinion/ -> skipped
        entry(href=cl, title=None, summary=None, published=None),      # missing title/summary/date
        entry(href=cl, title="", summary=""),                          # empty title/summary
        entry(href=cl, summary="<b>HTML</b> &amp; &lt;script&gt; " + "long " * 4000),  # html + entities + huge
        entry(href=cl, summary="Ünïcode 案件 ☃  control", published="not-a-date"),
        entry(href=cl, pdf="not-a-url", enclosure_type="text/html"),   # bad pdf enclosure
        entry(href="https://www.courtlistener.com/opinion/007/x/"),    # leading-zero id
        entry(href=cl) + entry(href=cl),                               # duplicate ids in one feed
    ]
    cands = update._parse_feed(feed_bytes(hostile), court)
    check("parse_feed: hostile-but-valid feed parses without crashing", isinstance(cands, list))
    check("parse_feed: every candidate is well-formed", not well_formed(cands, court), well_formed(cands, court))

    # Malformed XML: raising is acceptable (feed_court wraps this in try/except); returning corrupt
    # data is not. The invariant is "list or raise, never anything else".
    for label, raw in (("garbage bytes", b"\x00\x01not xml at all"),
                       ("truncated feed", b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>x")):
        raised = False
        val = None
        try:
            val = update._parse_feed(raw, court)
        except Exception:
            raised = True
        check("parse_feed: malformed XML (%s) raises or returns a list, never corrupt" % label,
              raised or isinstance(val, list))

    # A DTD / internal-entity declaration (the billion-laughs vector) is refused BEFORE parsing, so
    # expat never expands it. A genuine CL feed carries none, so this only ever fires on an attack.
    bomb = (b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY lol "lol">]>'
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&lol;</title></entry></feed>')
    raised = False
    try:
        update._parse_feed(bomb, court)
    except Exception:
        raised = True
    check("parse_feed: a DTD/entity feed is refused (no entity expansion)", raised)


def test_parse_json():
    good = [('{"a": 1}', dict), ('```json\n{"b": 2}\n```', dict), ('prose {"c": 3} trailer', dict),
            ('[1, 2, 3]', list), ('```\n[4]\n```', list)]
    for s, typ in good:
        try:
            v = update.parse_json(s)
        except Exception as e:
            check("parse_json: %r -> value" % s, False, "raised %r" % e); continue
        check("parse_json: %r parses to %s" % (s, typ.__name__), isinstance(v, typ))

    rng = random.Random(20260716)
    frag = ['{', '}', '[', ']', '"k"', ':', ',', '1', 'true', 'null', 'nan', '```', '```json',
            'prose', '\n', ' ', '{"a":', '}}}', '"unterminated', '\\', '…', '案']
    for _ in range(1500):
        s = "".join(rng.choice(frag) for _ in range(rng.randint(0, 16)))
        try:
            update.parse_json(s)                       # a JSON value is fine
        except json.JSONDecodeError:
            pass                                       # the documented failure mode is fine
        except Exception as e:
            check("parse_json never raises a non-JSONDecodeError", False, "%r on %r" % (e, s))
            break


class FakeResp:
    def __init__(self, data):
        self._d = data

    def read(self, n=-1):
        return self._d[:n] if (n is not None and n >= 0) else self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_pypdf(mode):
    """Inject a fake `pypdf` so pdf_text's parse branch runs even though the real one is optional.
    `mode`: 'ok' (pages of text), 'reader_raises', 'extract_raises', 'extract_none', 'no_pages'."""
    mod = types.ModuleType("pypdf")

    class _Page:
        def extract_text(self):
            if mode == "extract_raises":
                raise ValueError("bad page")
            if mode == "extract_none":
                return None
            return "Opinion text extracted from the PDF. " * 20

    class PdfReader:
        def __init__(self, _stream):
            if mode == "reader_raises":
                raise Exception("not a pdf")
            self.pages = [] if mode == "no_pages" else [_Page(), _Page()]

    mod.PdfReader = PdfReader
    sys.modules["pypdf"] = mod


def test_pdf_text():
    saved_open = update.urllib.request.urlopen
    saved_sleep = update.time.sleep
    saved_pypdf = sys.modules.get("pypdf")
    update.time.sleep = lambda *a, **k: None
    try:
        # url gating: nothing that is not http(s) should ever hit the network.
        for bad in (None, "", "ftp://x/y.pdf", "file:///etc/passwd", "javascript:alert(1)", "/local"):
            called = {"n": 0}
            update.urllib.request.urlopen = lambda *a, _c=called, **k: _c.__setitem__("n", _c["n"] + 1)
            r = update.pdf_text(bad)
            check("pdf_text: non-http url %r -> '' and no fetch" % bad, r == "" and called["n"] == 0)

        good_url = "https://storage.courtlistener.com/x.pdf"

        # download errors are swallowed -> "" (so the caller falls back to REST).
        def raise_http(*a, **k):
            raise urllib.error.URLError("boom")
        update.urllib.request.urlopen = raise_http
        _install_fake_pypdf("ok")
        check("pdf_text: download error -> ''", update.pdf_text(good_url) == "")

        # every pypdf failure mode is swallowed -> "".
        update.urllib.request.urlopen = lambda *a, **k: FakeResp(b"%PDF-1.4 fake bytes")
        for mode in ("reader_raises", "extract_raises"):
            _install_fake_pypdf(mode)
            check("pdf_text: pypdf %s -> ''" % mode, update.pdf_text(good_url) == "")

        # success and the benign edge cases return a str, never raise.
        for mode in ("ok", "extract_none", "no_pages"):
            _install_fake_pypdf(mode)
            out = update.pdf_text(good_url)
            check("pdf_text: pypdf %s -> str" % mode, isinstance(out, str))
        _install_fake_pypdf("ok")
        check("pdf_text: a good extraction returns non-empty cleaned text",
              len(update.pdf_text(good_url)) > 0)

        # a past deadline returns "" without fetching.
        called = {"n": 0}
        update.urllib.request.urlopen = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or FakeResp(b"x")
        check("pdf_text: past deadline -> '' without fetching", update.pdf_text(good_url, deadline=1.0) == "" and called["n"] == 0)
    finally:
        update.urllib.request.urlopen = saved_open
        update.time.sleep = saved_sleep
        if saved_pypdf is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = saved_pypdf


class CapResp:
    """A urlopen response whose read(n) honors the byte limit, to prove the caller caps it."""
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data[:n] if (n is not None and n >= 0) else self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_read_caps():
    saved_open = update.urllib.request.urlopen
    saved_sleep = update.time.sleep
    update.time.sleep = lambda *a, **k: None
    try:
        big = b"x" * (update.FEED_MAX_BYTES + 4096)
        update.urllib.request.urlopen = lambda *a, **k: CapResp(big)
        out = update.feed_get("https://www.courtlistener.com/feed/court/x/")
        check("feed_get caps the read at FEED_MAX_BYTES", len(out) == update.FEED_MAX_BYTES, "%d" % len(out))
    finally:
        update.urllib.request.urlopen = saved_open
        update.time.sleep = saved_sleep


def test_shape_helpers():
    # opinion_ids_of's REST fallback would hit the network on a cluster-only r; stub cl_get so the
    # fuzz can throw anything at it without a live call, and so a fallback is detected, not executed.
    saved_cl = update.cl_get
    update.cl_get = lambda *a, **k: {"sub_opinions": ["/opinions/55/", 66, None, "junk"]}
    rng = random.Random(2718281)
    BAD = [None, 0, 5, -1, [], {}, "", "x" * 5000, ["a", 1, None], {"k": None}, True, 3.14,
           "A26A0123", "not a docket", "案件", "\x00\n\t", ["A26A0001", "B99B9999"], 12345678]
    try:
        for it in range(4000):
            r = {}
            for k in ("cluster_id", "absolute_url", "opinions", "sibling_ids", "caseName",
                      "snippet", "dockets", "docketNumber", "dateFiled", "court_id"):
                if rng.random() < 0.7:
                    r[k] = rng.choice(BAD)
            # opinions as a list of dicts sometimes (the fast path in opinion_ids_of).
            if rng.random() < 0.3:
                r["opinions"] = [{"id": rng.choice([None, 7, "8", -1])} for _ in range(rng.randint(0, 3))]
            try:
                cid = update.cluster_id_of(r)
                update.opinion_ids_of(r)
                update.snippet_of(r)
                update._docket_set(r.get("dockets") or r.get("docketNumber"))
                update._dup_sig(str(r.get("court_id") or ""), r.get("dateFiled"), r.get("dockets"), r.get("caseName"))
                update.party_tokens(r.get("caseName"))              # hardened: accepts a non-str name
                update._normalize_for_match(r.get("snippet"))       # hardened: accepts a non-str quote
                update.clip(r.get("snippet"), rng.choice([None, 0, 1, 100, 10**6]))   # hardened: non-str ok
                update._pdf_ok(r.get("snippet"))                    # hardened: accepts a non-str
                if cid is not None and not isinstance(cid, int):
                    check("cluster_id_of returns int or None", False, "%r -> %r" % (r, cid)); break
            except Exception as e:
                check("shape helpers never crash on a hostile r (it=%d)" % it, False, "%r on %r" % (e, r))
                break
        else:
            check("shape/dedup helpers: 4000 hostile r dicts, no crash", True)
    finally:
        update.cl_get = saved_cl


def main():
    print("update.py ingestion stress (feeds, PDFs, model output, shape helpers):")
    print("- parse_feed:")
    test_parse_feed()
    print("- parse_json:")
    test_parse_json()
    print("- pdf_text:")
    test_pdf_text()
    print("- read caps:")
    test_read_caps()
    print("- shape/dedup helpers:")
    test_shape_helpers()
    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS[:20]))
        return 1
    print("\nALL INGESTION STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
