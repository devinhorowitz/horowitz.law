# CI failure: diagnosis and fix set · 2026-06-10

## What actually broke

Nothing in the bundle, and nothing in the pipeline. `render.py` writes only
between the HTML comment markers (`<!-- opinions:start/end -->` and friends)
and deliberately never touches anything else in the page — so when the assets
changed and the five hand-maintained pages got new `?v=` tokens, the *shells*
of the two generated pages kept their old ones, and the every-4-hours pipeline
faithfully preserved them. `check_site.py` (guard #2) then did exactly its job:
red CI until the shells are restamped. The failure is the system working;
what was missing was a mechanism to keep generated-page tokens fresh
automatically.

## This set (5 files, upload to the same paths)

**opinions.html, archive.html** — restamped by your own tool
(`python scripts/check_site.py --fix`) against main @ fbae516. Equivalent
alternative: run that command yourself and commit. If a bot PR merges before
you upload, prefer running `--fix` locally over these copies; everything else
in this set stays valid regardless.

**scripts/render.py** — the durable fix. Every page write now passes through
`_stamp_tokens()` alongside the existing `_stamp_year()`: `_inject()` covers
the two generated pages, and the STATIC_PAGES loop means the daily render-sync
net heals the hand pages too. Tokens are computed the same way `check_site.py`
checks them (sha256[:10] of asset content), but the implementations stay
deliberately independent so a renderer bug can't blind the checker. Verified
end to end on your exact CI sequence: py_compile + the full import list, all
node --check targets, `check_site.py` green, and the render-idempotency step
clean. Self-healing proven by drill: corrupted a token to `deadbeef00`, ran
`render.py`, token came back correct and the full check passed.

**functions/_middleware.js** — the pages.dev → apex 301, done right. My
earlier `_redirects` host rule was Netlify-style syntax; it deployed but is
inert, because Pages `_redirects` sources are path-based only. This middleware
301s exactly `horowitz-law.pages.dev` (so hashed preview deployments keep
working) and passes everything else through. Stated tradeoff, in the file
header too: a root middleware turns every request into a Functions invocation
(previously only /api/*) — negligible at this traffic, and deleting the file
returns you to pure static + rel=canonical. Optional CI line while you're in
there: add `node --check functions/_middleware.js` to the JS syntax step.

**_redirects** — corrected to say what it can and cannot do, pointing at the
middleware; keeps the commented /dh.vcf alias.

## Found while diagnosing: a header source outside the repo

With the new single-rule `_headers` confirmed live, base.css and app.js emit
exactly one Cache-Control — but `/fonts/*` is still doubled, and HTML still
carries `Access-Control-Allow-Origin: *`, which the committed `_headers` does
not set. Both are therefore being added by zone-level configuration on
horowitz.law (Transform Rule → Modify Response Header, a Cache Rule, or a
Worker), not by anything in git. Dashboard checklist: Rules → remove the
redundant font Cache-Control append (the `_headers` rule already covers it),
and optionally the global ACAO (the feed-scoped grant now lives in `_headers`
under /opinions.xml).

## One heads-up, not a problem

`pages_build_output_dir = "."` means the whole repo serves publicly:
INTEGRATION.md, BACKLOG.md, PIPELINE.md, scripts/*.py, and the pipeline state
files all return 200 on the apex (probed). Consistent with the open-source
footer; flagged only so it stays a choice rather than a surprise.

## After committing

1. CI on the commit: both the invariants step and the render-idempotency step
   pass (replicated locally on this exact tree).
2. Post-deploy: `curl -sI https://horowitz-law.pages.dev/resume` → 301 to
   https://horowitz.law/resume.
3. After the dashboard cleanup: the font request shows a single Cache-Control.
4. Optional drill any time: break a `?v=` token by hand, run
   `python scripts/render.py`, watch it heal.
