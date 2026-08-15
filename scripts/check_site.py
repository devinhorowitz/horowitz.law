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

  4. opinions.xml, sitemap.xml, and changes.xml are well-formed XML.

  4b. The two PWA manifests (manifest.json for the personal site, manifest.webmanifest
     for Georgia Appellate Watch -- intentionally separate installable apps) are valid
     JSON and every icon they reference exists on disk, so a renamed icon fails here
     rather than silently breaking install in production.

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
WEB = os.path.join(REPO, "public")   # deployed web root; served files live here, scripts/ stays at REPO

PAGES = ["index.html", "resume.html", "colophon.html", "opinions.html",
         "archive.html", "subscribe.html", "404.html",
         "changes.html", "stats.html", "digests.html", "legislation.html"]
ASSETS = ["base.css", "app.js", "opinions.js", "subscribe.js"]
HEADERS_PATH = os.path.join(WEB, "_headers")
TOKEN_LEN = 10  # hex chars of sha256 in the ?v= token; plenty against collision here

_INLINE_RE = re.compile(r"<script>(.*?)</script>", re.S | re.I)
_CSP_HASH_RE = re.compile(r"'sha256-([A-Za-z0-9+/=]+)'")


def _all_pages():
    """The hand-maintained pages plus every generated permalink, so the CSP and
    token guards cover the /o/ pages too."""
    import glob as _g
    return PAGES + sorted(_g.glob(os.path.join(WEB, "o", "*.html")))

def _read(path):
    return open(os.path.join(WEB, path), encoding="utf-8").read()


def asset_token(name):
    data = open(os.path.join(WEB, name), "rb").read()
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
                        lambda mm, _a=a: mm.group(1) + "?v=" + expected[_a] + mm.group(4), new)
                elif tok is None:
                    errors.append("%s: /%s is referenced without a ?v= token "
                                  "(run scripts/check_site.py --fix)" % (p, a))
                else:
                    errors.append("%s: /%s?v=%s is stale; the file's content hash is %s "
                                  "(run scripts/check_site.py --fix)" % (p, a, tok, expected[a]))
        if fix and new != doc:
            safeio.atomic_write_text(os.path.join(WEB, p), new)
            print("stamped current asset tokens into %s" % p)


def check_scripts_dir(errors):
    for name in os.listdir(os.path.join(REPO, "scripts")):
        if re.search(r"\s", name):
            errors.append("scripts/%s: whitespace in a filename; almost certainly a stray "
                          "duplicate from a hand upload. Delete it." % name)


def check_xml(errors):
    import xml.etree.ElementTree as ET
    for f in ("opinions.xml", "sitemap.xml", "changes.xml"):
        path = os.path.join(WEB, f)
        if not os.path.exists(path):
            errors.append("%s: missing" % f)
            continue
        try:
            ET.parse(path)
        except ET.ParseError as e:
            errors.append("%s: not well-formed XML (%s)" % (f, e))


def check_manifests(errors):
    """The site ships two intentional PWA manifests -- manifest.json (the personal
    site, id "/") and manifest.webmanifest (Georgia Appellate Watch, id "/opinions")
    -- each installable with its own identity. This guards them against silent rot:
    both must be valid JSON, and every icon they reference must be a real file, so a
    renamed or deleted icon fails here at push time instead of breaking install and
    the home-screen icon in production (where nothing else would catch it)."""
    import json as _json
    for name in ("manifest.json", "manifest.webmanifest"):
        path = os.path.join(WEB, name)
        if not os.path.exists(path):
            errors.append("%s: missing" % name)
            continue
        try:
            data = _json.loads(_read(name))
        except ValueError as e:
            errors.append("%s: not valid JSON (%s)" % (name, e))
            continue
        for icon in data.get("icons", []):
            src = (icon.get("src") or "").split("?", 1)[0]
            if not src.startswith("/"):
                errors.append("%s: icon src %r is not a root-relative path" % (name, src))
                continue
            if not os.path.exists(os.path.join(WEB, src.lstrip("/"))):
                errors.append("%s: icon %s does not exist on disk" % (name, src))


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


# Every workflow that runs render.py and commits the result through a hard-coded add-paths list.
# render-sync stages ONLY render outputs, so its list must EQUAL render.OUTPUT_PATHS; the others
# stage their own data files too, so their lists must be a SUPERSET of OUTPUT_PATHS. A list that
# omits a render output ships a PR whose committed pages disagree with a fresh render -- main then
# fails CI's idempotency diff until a render-sync heals it, which is exactly how queue.yml's
# hand-picked list broke main on 2026-07-25 (six recovered cards changed stats.html and
# public/areas, neither of which the queue PR carried). opinions.yml is not listed: its publish
# step derives the set from render.OUTPUT_PATHS via scripts/publish.py, so it cannot drift.
RENDER_COMMIT_WORKFLOWS = [("render-sync.yml", True),   # (filename, must_be_exact)
                           ("queue.yml", False),
                           ("backfill.yml", False),
                           ("treatment.yml", False),
                           ("legislation.yml", False)]


def _addpaths_block(text):
    """The entries of the first `add-paths: |` block scalar in a workflow file. Line-based:
    collect the deeper-indented lines under it until a dedent ends the block -- more robust
    than a regex against YAML indentation."""
    listed, in_block, base_indent = [], False, None
    for ln in text.splitlines():
        if not in_block:
            if re.match(r"\s*add-paths:\s*\|\s*$", ln):
                in_block, base_indent = True, len(ln) - len(ln.lstrip())
            continue
        if not ln.strip():
            continue
        if (len(ln) - len(ln.lstrip())) <= base_indent:
            break
        s = ln.strip()
        if not s.startswith("#"):
            listed.append(s)
    return listed


def check_render_outputs(errors):
    """Each render-committing workflow's add-paths must cover the set render owns
    (render.OUTPUT_PATHS) -- exactly for render-sync, at least for the workflows that also
    stage their own data files. See RENDER_COMMIT_WORKFLOWS for the roster and the rationale."""
    import render  # OUTPUT_PATHS: the authoritative render-owned set
    owned = set(render.OUTPUT_PATHS)
    for fname, exact in RENDER_COMMIT_WORKFLOWS:
        wf = os.path.join(REPO, ".github", "workflows", fname)
        try:
            text = open(wf, encoding="utf-8").read()
        except OSError as e:
            errors.append("%s unreadable, cannot check add-paths (%s)" % (fname, e))
            continue
        listed = _addpaths_block(text)
        if not listed:
            errors.append("%s: could not read the add-paths block" % fname)
            continue
        missing = sorted(owned - set(listed))
        if missing:
            errors.append("%s add-paths is MISSING render output(s) (drift risk): %s"
                          % (fname, ", ".join(missing)))
        if exact:
            extra = sorted(set(listed) - owned)
            if extra:
                errors.append("%s add-paths lists path(s) render does not own: %s"
                              % (fname, ", ".join(extra)))


# wrangler.toml [vars] key -> the siteconfig attribute it must equal. These values are read at
# TWO runtimes that cannot import each other: the Cloudflare Functions (JS, at request time, from
# wrangler.toml) and the Python pipeline (digest.py, via siteconfig). Neither can be generated from
# the other, so the duplication is structural -- but drift is silent and expensive:
# functions/api/subscribe/confirm.js WRITES a confirmed subscriber into RESEND_SEGMENT_ID and opts
# them into RESEND_TOPIC_ID, while digest.py SENDS to those same ids. If the two copies disagree,
# subscribers confirm successfully, see the success page, and never receive an email -- with no
# error raised anywhere. Assert instead of remembering, the same discipline as the render-output
# and .python-version checks above.
WRANGLER_MIRRORED = {
    "DIGEST_FROM": "DIGEST_FROM",
    "RESEND_LEGISLATION_SEGMENT_ID": "RESEND_LEGISLATION_SEGMENT_ID",
    "RESEND_LEGISLATION_TOPIC_ID": "RESEND_LEGISLATION_TOPIC_ID",
    "RESEND_SEGMENT_ID": "RESEND_SEGMENT_ID",
    "RESEND_TOPIC_ID": "RESEND_TOPIC_ID",
    "SITE_URL": "SITE_URL",
}
# Vars that legitimately live only in wrangler.toml (no Python twin). Empty today; an entry here is
# a deliberate statement that the Python side does not need the value.
WRANGLER_LOCAL_ONLY = set()


def check_wrangler_vars(errors):
    """Every plaintext var in wrangler.toml's [vars] must either match its siteconfig twin or be
    declared wrangler-only. A new var with neither is an error, so a fifth shared value cannot be
    added without deciding which it is. Secrets are never in [vars] (they live in the Pages
    dashboard), so nothing here reads a credential."""
    import tomllib  # stdlib since 3.11; the pipeline requires newer
    import siteconfig
    path = os.path.join(REPO, "wrangler.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        errors.append("wrangler.toml unreadable or not valid TOML (%s)" % e)
        return
    variables = data.get("vars")
    if variables is None:
        errors.append("wrangler.toml has no [vars] table; the Functions read their config from it")
        return
    for key, wrangler_value in sorted(variables.items()):
        if key in WRANGLER_LOCAL_ONLY:
            continue
        attr = WRANGLER_MIRRORED.get(key)
        if attr is None:
            errors.append("wrangler.toml [vars] has %s, which check_site does not know about. "
                          "Add it to WRANGLER_MIRRORED (with the siteconfig attribute it must "
                          "equal) or to WRANGLER_LOCAL_ONLY." % key)
            continue
        expected = getattr(siteconfig, attr, None)
        if expected is None:
            errors.append("siteconfig has no %s, but wrangler.toml [vars] mirrors it as %s"
                          % (attr, key))
        elif wrangler_value != expected:
            errors.append("wrangler.toml [vars] %s is %r but siteconfig.%s is %r; the Functions "
                          "and the digest would disagree at runtime"
                          % (key, wrangler_value, attr, expected))
    for key, attr in sorted(WRANGLER_MIRRORED.items()):
        if key not in variables:
            errors.append("wrangler.toml [vars] is missing %s; the Functions read it at request "
                          "time (siteconfig.%s holds the Python-side copy)" % (key, attr))


def check_python_version(errors):
    """.python-version is the one place the interpreter version is stated, and ruff.toml's
    target-version must agree with it. The workflows read the file via setup-python's
    python-version-file rather than carrying a literal, so these two are the only statements
    left -- and a stale ruff target silently lints against the wrong language version,
    accepting or rejecting syntax the runtime does not. Same discipline as
    check_render_outputs: two places that must agree, asserted rather than remembered."""
    vpath = os.path.join(REPO, ".python-version")
    try:
        version = open(vpath, encoding="utf-8").read().strip()
    except OSError as e:
        errors.append(".python-version unreadable; the workflows resolve their interpreter "
                      "from it (%s)" % e)
        return
    if not re.fullmatch(r"\d+\.\d+", version):
        errors.append(".python-version should hold a bare MAJOR.MINOR (e.g. 3.14), not %r" % version)
        return
    try:
        ruff = open(os.path.join(REPO, "ruff.toml"), encoding="utf-8").read()
    except OSError as e:
        errors.append("ruff.toml unreadable, cannot check target-version (%s)" % e)
        return
    m = re.search(r'^target-version\s*=\s*"py(\d+)"', ruff, re.M)
    if not m:
        errors.append("ruff.toml has no target-version to check against .python-version")
        return
    want = "py" + version.replace(".", "")
    if m.group(1) != want[2:]:
        errors.append("ruff.toml target-version is py%s but .python-version says %s (expected %s)"
                      % (m.group(1), version, want))


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
    check_manifests(errors)
    check_render_outputs(errors)
    check_python_version(errors)
    check_wrangler_vars(errors)
    if errors:
        print("check_site: %d problem(s)" % len(errors))
        for e in errors:
            print("  ! " + e)
        return 1
    print("check_site: CSP hash, asset tokens, scripts/ names, XML, manifests, and render-output "
          "lists all check out")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
