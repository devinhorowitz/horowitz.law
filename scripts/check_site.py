#!/usr/bin/env python3
"""Site-invariant checks for horowitz.law. Pure standard library; run by CI on
every push and locally any time:

  python scripts/check_site.py          # verify; exit nonzero on any failure
  python scripts/check_site.py --fix    # restamp the ?v= asset tokens, then verify
  python scripts/check_site.py --links  # network: external links on the hand pages (scheduled, not per-push)

What it guards, and why each exists:

  1. CSP hash <-> inline script. Every page carries one identical inline
     pre-paint <script>, allow-listed in _headers by its sha256. An edit to the
     script without the matching hash bump silently blocks it in browsers (theme
     flash returns, the .js marker never lands). This check recomputes the hash
     from each page and requires _headers to carry exactly that hash.

  2. ?v= asset tokens. base.css / app.js / opinions.js / subscribe.js are cached
     immutable for a year; correctness rests on the ?v= token changing whenever
     the content does. The token IS the content hash (first 10 hex of sha256),
     so the check is deterministic: an edited asset with a stale token fails CI
     until `--fix` restamps every reference. No human memory in the loop.

  3. No stray filenames in scripts/. A hand upload once landed a duplicate with
     a space in its name; py_compile happily compiles such a file, so the smoke
     test alone cannot see the class. Any whitespace-named file here fails.

  4. opinions.xml and sitemap.xml are well-formed XML.

  5. External link rot (--links only, off by default). The hand-authored pages
     link out to the firm, the bar, the courts, the schools, and LinkedIn, and
     those URLs drift as institutions reorganize. With --links the external
     links on index.html and resume.html are fetched; any returning 404 or 410
     is rot (exit 3, details in scripts/link_rot.md for the issue body), a host
     only unreachable is transient (exit 4), and a bot-block or other status is
     treated as alive. Scoped to those two pages, so the generated permalinks
     and their CourtListener links are never hit and the API budget is untouched.

Checks are independent: all of them run and every failure is reported before the
nonzero exit, so one push surfaces the full list.
"""
import base64
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safeio  # crash-safe atomic writes for --fix

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGES = ["index.html", "resume.html", "colophon.html", "opinions.html",
         "archive.html", "subscribe.html", "404.html",
         "changes.html", "stats.html", "digests.html"]
ASSETS = ["base.css", "app.js", "opinions.js", "subscribe.js"]
HEADERS_PATH = os.path.join(REPO, "_headers")
TOKEN_LEN = 10  # hex chars of sha256 in the ?v= token; plenty against collision here

_INLINE_RE = re.compile(r"<script>(.*?)</script>", re.S | re.I)
_CSP_HASH_RE = re.compile(r"'sha256-([A-Za-z0-9+/=]+)'")


def _all_pages():
    """The hand-maintained pages plus every generated permalink, so the CSP and
    token guards cover the /o/ pages too."""
    import glob as _g
    return PAGES + sorted(_g.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "o", "*.html")))

def _read(path):
    return open(os.path.join(REPO, path), encoding="utf-8").read()


def asset_token(name):
    data = open(os.path.join(REPO, name), "rb").read()
    return hashlib.sha256(data).hexdigest()[:TOKEN_LEN]


def inline_hash(doc):
    blocks = _INLINE_RE.findall(doc)
    if len(blocks) != 1:
        return None, len(blocks)
    digest = hashlib.sha256(blocks[0].encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode(), 1


def check_csp(errors):
    headers = _read("_headers")
    csp_line = next((ln for ln in headers.splitlines()
                     if ln.strip().startswith("Content-Security-Policy:")), "")
    declared = set(_CSP_HASH_RE.findall(csp_line))
    page_hashes = set()
    for p in _all_pages():
        h, n = inline_hash(_read(p))
        if h is None:
            errors.append("%s: expected exactly one inline <script> block, found %d" % (p, n))
            continue
        page_hashes.add(h.split("sha256-", 1)[1])
    if len(page_hashes) > 1:
        errors.append("inline pre-paint script differs across pages; it must be byte-identical "
                      "(hashes: %s)" % ", ".join(sorted(page_hashes)))
        return
    if not page_hashes:
        return
    actual = page_hashes.pop()
    if actual not in declared:
        errors.append("_headers CSP does not allow-list the pages' inline script. "
                      "Set the script-src hash to 'sha256-%s' in the same commit "
                      "as the script change." % actual)
    stale = declared - {actual}
    if stale:
        errors.append("_headers CSP carries stale script hash(es) no page uses: %s. "
                      "Remove them so the allow-list stays exactly one script wide."
                      % ", ".join("sha256-" + s for s in sorted(stale)))


def check_tokens(errors, fix=False):
    expected = {a: asset_token(a) for a in ASSETS}
    ref_re = {a: re.compile(r'((?:href|src)="/%s)(\?v=([^"]*))?(")' % re.escape(a))
              for a in ASSETS}
    for p in _all_pages():
        doc = _read(p)
        new = doc
        for a in ASSETS:
            for m in ref_re[a].finditer(doc):
                tok = m.group(3)
                if tok == expected[a]:
                    continue
                if fix:
                    new = ref_re[a].sub(
                        lambda mm: mm.group(1) + "?v=" + expected[a] + mm.group(4), new)
                elif tok is None:
                    errors.append("%s: /%s is referenced without a ?v= token "
                                  "(run scripts/check_site.py --fix)" % (p, a))
                else:
                    errors.append("%s: /%s?v=%s is stale; the file's content hash is %s "
                                  "(run scripts/check_site.py --fix)" % (p, a, tok, expected[a]))
        if fix and new != doc:
            safeio.atomic_write_text(os.path.join(REPO, p), new)
            print("stamped current asset tokens into %s" % p)


def check_scripts_dir(errors):
    for name in os.listdir(os.path.join(REPO, "scripts")):
        if re.search(r"\s", name):
            errors.append("scripts/%s: whitespace in a filename; almost certainly a stray "
                          "duplicate from a hand upload. Delete it." % name)


def check_xml(errors):
    import xml.etree.ElementTree as ET
    for f in ("opinions.xml", "sitemap.xml", "changes.xml"):
        path = os.path.join(REPO, f)
        if not os.path.exists(path):
            errors.append("%s: missing" % f)
            continue
        try:
            ET.parse(path)
        except ET.ParseError as e:
            errors.append("%s: not well-formed XML (%s)" % (f, e))


# ---- External link rot (opt-in, --links) --------------------------------
LINK_PAGES = ("index.html", "resume.html")   # hand-authored pages only
_LINK_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) horowitz.law-linkcheck/1.0")
_LINK_SKIP = ("www.w3.org/2000/svg", "schema.org")  # XML / JSON-LD namespaces, not pages


def _external_links():
    seen = set()
    for name in LINK_PAGES:
        for u in re.findall(r'"(https?://[^"\s]+)"', _read(name)):
            if "horowitz.law" in u or any(k in u for k in _LINK_SKIP):
                continue
            seen.add(u)
    return sorted(seen)


def _link_status(url):
    """('dead'|'alive'|'unreachable', detail). 404/410 -> dead. Any other HTTP
    response, including 403/429/5xx bot-blocks, -> alive: the URL resolves, it is
    just guarded or hiccuping, which is not link rot. No response after a retry ->
    unreachable (transient or DNS)."""
    import urllib.request, urllib.error
    for _ in range(2):                        # one retry on a transport failure
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": _LINK_UA})
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    return ("alive", r.status)
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return ("dead", e.code)
                if method == "HEAD" and e.code in (403, 405, 501):
                    continue                  # server refuses HEAD; try GET
                return ("alive", e.code)
            except Exception:
                break                         # transport error; fall through to retry
    return ("unreachable", "no response")


def check_links():
    """Verify external links on the hand-authored pages still resolve. Returns 0
    (all resolve), 3 (a link is dead, 404/410), or 4 (only unreachable, no
    confirmed rot). Writes scripts/link_rot.md on a dead link for the maintenance
    workflow to file as an issue. Scoped to index.html and resume.html, so the
    generated permalinks and their CourtListener links are never touched."""
    urls = _external_links()
    dead, unreachable, ok = [], [], 0
    for u in urls:
        state, detail = _link_status(u)
        if state == "dead":
            dead.append((u, detail)); print("  ! dead (%s): %s" % (detail, u))
        elif state == "unreachable":
            unreachable.append(u);    print("  ? unreachable: %s" % u)
        else:
            ok += 1;                  print("  ok (%s): %s" % (detail, u))
    print("check_site --links: %d ok, %d unreachable, %d dead, of %d checked"
          % (ok, len(unreachable), len(dead), len(urls)))
    if dead:
        with open(os.path.join(REPO, "scripts", "link_rot.md"), "w", encoding="utf-8") as f:
            f.write("Dead external link(s) found on the hand-authored pages:\n\n")
            for u, d in dead:
                f.write("- `%s` returned %s\n" % (u, d))
            f.write("\nLinked from index.html or resume.html. Update or remove them.\n")
        return 3
    return 4 if unreachable else 0


def main(argv):
    if "--links" in argv:
        return check_links()
    fix = "--fix" in argv
    errors = []
    if fix:
        check_tokens(errors, fix=True)
        errors = []  # fix pass rewrote tokens; verify everything fresh below
    check_scripts_dir(errors)
    check_csp(errors)
    check_tokens(errors, fix=False)
    check_xml(errors)
    if errors:
        print("check_site: %d problem(s)" % len(errors))
        for e in errors:
            print("  ! " + e)
        return 1
    print("check_site: CSP hash, asset tokens, scripts/ names, and XML all check out")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
