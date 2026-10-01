#!/usr/bin/env python3
"""Hermetic test of the REAL pypdf against a real (tiny) PDF -- the one runtime dependency.

Every other pdf_text test (stress_ingest.py) injects a fake pypdf, so none of them notice when the
pinned one changes under us. And the funnel cannot notice either: pdf_text swallows every pypdf
failure and returns "", which sends each opinion to the REST fallback. A Dependabot bump that broke
PdfReader(BytesIO), .pages or .extract_text() would merge on green CI and quietly move every read
onto the rate-limited API. This builds a two-page PDF in memory (standard-14 font, one text object
per page, xref offsets computed so the file is well-formed), then asserts the text round-trips
through pypdf directly and through update.pdf_text itself, with only the download stubbed.

No network and no fixture file. If pypdf will not import, this FAILS under CI (the CI env var,
which GitHub Actions sets; ci.yml installs requirements-ci.txt, so pypdf must be there) and prints
SKIP and exits 0 elsewhere -- a local box whose pypdf is broken (e.g. a cffi/cryptography mismatch
that panics on import) should not block the rest of the suite.

Run directly: `python scripts/test_pypdf_real.py`.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # noqa: E402

FAILS = []
PAGES = ["Smith v. Jones, Court of Appeals of Georgia",
         "Judgment affirmed. All the Judges concur."]


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def tiny_pdf(pages):
    """A minimal valid PDF: one page per string, each drawn by a single BT/Tj/ET text object."""
    n = len(pages)
    kids = " ".join("%d 0 R" % (4 + 2 * i) for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + kids.encode() + b"] /Count %d >>" % n,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        stream = b"BT /F1 12 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i))
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


class FakeResp:
    """A urlopen response serving the in-memory PDF, honoring read(n) like the real one."""
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data if n is None or n < 0 else self.data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main():
    try:
        import pypdf
    except BaseException as e:  # a broken native dep can raise pyo3's PanicException, not an Exception
        if isinstance(e, KeyboardInterrupt):
            raise
        why = "%s: %s" % (type(e).__name__, e)
        if os.environ.get("CI"):
            print("FAIL: pypdf does not import under CI (%s)" % why)
            return 1
        print("SKIP: pypdf does not import here (%s); CI runs this for real" % why)
        return 0

    print("real pypdf %s:" % getattr(pypdf, "__version__", "?"))
    raw = tiny_pdf(PAGES)

    reader = pypdf.PdfReader(io.BytesIO(raw))
    check("PdfReader reads a BytesIO and sees both pages", len(reader.pages) == 2, "%d pages" % len(reader.pages))
    first = (reader.pages[0].extract_text() or "").strip()
    check("pages[0].extract_text() round-trips the text object", first == PAGES[0], repr(first))

    # pdf_text end to end, only the download stubbed: the funnel's exact PdfReader/.pages/.extract_text
    # chain, which would return "" (the REST fallback) rather than raise if the API had moved.
    saved_open = update.urllib.request.urlopen
    update.urllib.request.urlopen = lambda *a, **k: FakeResp(raw)
    try:
        got = update.pdf_text("https://storage.courtlistener.com/pdf/test.pdf")
    finally:
        update.urllib.request.urlopen = saved_open
    check("update.pdf_text returns both pages' text, in order",
          got == "\n".join(PAGES), repr(got))

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS))
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
