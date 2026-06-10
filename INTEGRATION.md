# horowitz.law — patch bundle · 2026-06-10

Everything from the site review, implemented and verified. Each file below is a
drop-in replacement for (or addition to) the file of the same name at the repo
root. Diff before committing — these were built from the deployed files, which
are the source files (no build step), but your working tree is the truth.

## What changed, file by file

### index.html — new QR code (the one substantive engineering fix)

The contact QR was a Version 3 symbol at error-correction level **M (15%)**.
The 9×9 logo carve-out consumes ~10 of its 13 correctable codewords — ~77% of
the entire error budget before a camera adds any noise — and it failed every
desktop decoder test (zbar and OpenCV, with and without the DH monogram).

The replacement is a **Version 4 symbol (33×33 modules, viewBox 41) at EC
level Q (25%)**, same canonical payload `https://horowitz.law/devin-horowitz.vcf`,
rendered in the exact same SVG vocabulary: identical classes (`qr-code`,
`qr-logo-bg`, `qr-logo-dh`), the run-length `M{x},{y}h{w}v1h-{w}z` path style,
the inline no-CSS fallback fills, the theme-recolor behavior, the centered 9×9
carve with the DH monogram, and a 4-module quiet zone. Your fixed 150px CSS box
scales the new viewBox automatically; nothing else in the page changes.

Verified before shipping (decoding the SVG actually embedded in this
index.html): format bits read back as **EC=Q, mask 3**; zbar decodes it **with**
the logo carve and DH text, with added blur, and after polarity inversion of the
dark-theme rendering. The uninverted dark-theme render still doesn't decode in
zbar — that's zbar's known no-inverted-polarity limitation, not the symbol
(flipping polarity decodes perfectly); phone cameras handle inversion, matching
the existing comment in your source. The old V3-M code failed all of these.

One correction to my earlier review: I said the 40-character URL "fits V3-Q's
53-byte limit" — wrong; 53 bytes is V3-**L**. V3-Q holds 32 bytes, which is why
the original was M: the canonical URL maxes out V3's error correction at M. Q
therefore required V4. If you ever want the smaller 29×29 symbol back, the
tested alternative is **V3-Q with a short alias** (`https://horowitz.law/dh.vcf`,
27 bytes ≤ 32) — it passed the same battery; the alias redirect is included,
commented out, in `_redirects`.

`qr-contact-card.svg` is a standalone copy of the new symbol for visual review.

### app.js — keyboard map catches up to the site

`g o` → /opinions, `g a` → /archive, `g s` → /subscribe, added to the
destination map, the `?` console help, and the header comment. Syntax-checked
with `node --check`.

**Latent bug found and fixed by this bump:** the deployed app.js content hashes
to sha256[:10] = `96a3bf7b52`, but every page references `?v=967dadb77a`.
(base.css, opinions.js, and subscribe.js all match their declared hashes
exactly, which is how I confirmed the scheme.) That means app.js was edited
after its version stamp was last computed — and under your one-year `immutable`
caching, returning visitors are pinned to the **older** app.js until the query
string changes. This bundle recomputes it; going forward, recompute
`sha256[:10]` on every edit (that is demonstrably your scheme).

### base.css — arrival cue for deep-linked opinion cards

Ports the landing page's `section:target` accent wash to `.opinion:target`
(2.2s wash + `scroll-margin-top`, self-cancelling under reduced motion). It
lives in base.css — alongside your `.op-treatment` styles — precisely so the
pipeline-generated /opinions and /archive pages inherit it without a template
change.

### 404.html — `ls /` now tells the truth

The terminal listing gains `archive/`, `subscribe`, and `colophon`, following
your existing convention (trailing slash for collections, bare names for
single documents).

### resume.html, colophon.html, subscribe.html — version bumps only

No content changes; they reference app.js and base.css, so their `?v=` strings
move with the new hashes.

### _redirects — NEW

Canonicalizes `horowitz-law.pages.dev` onto the apex with a 301, preserving
deep links (Cloudflare Pages host-matching syntax). Your `rel=canonical` tags
were already mitigating the duplicate origin; this closes it. The optional
`/dh.vcf` vCard alias is included commented out.

### _headers — RECONSTRUCTED, diff before adopting

I cannot see your real `_headers` (Pages doesn't serve it), so this is rebuilt
from the live response headers. The one functional change: exactly **one** rule
matches font paths, removing the duplicated `Cache-Control` value currently
emitted on `/fonts/*` (two of your rules both match — keep one). It also scopes
`Access-Control-Allow-Origin: *` to the RSS feed instead of globally; delete
that block if you prefer the current global behavior. Everything else
(CSP with your inline-script hash, HSTS, COOP, Permissions-Policy, the
Pratchett header) is reproduced verbatim.

## New version strings (sha256[:10], your scheme)

    app.js    ?v=42eb28acec
    base.css  ?v=1494e98128

**Pipeline note — the one thing this bundle can't reach:** the Appellate Watch
generator's page template also references `base.css?v=` and `app.js?v=`. Update
those two strings in the template to the values above, or the generated
/opinions and /archive pages will keep loading the old immutable assets and
miss the `:target` cue and the new shortcuts.

## Untouched on purpose

The inline theme script (its CSP sha256 pin stays valid), opinions.js and
subscribe.js (content verified to match their declared hashes), all fonts and
images, the watch pipeline's HTML.

## Post-deploy verification checklist

1. `curl -sI https://horowitz-law.pages.dev/resume` → 301 to
   `https://horowitz.law/resume`.
2. `curl -sI https://horowitz.law/fonts/jetbrains-mono-400.subset.woff2` →
   a single `cache-control` value.
3. Scan the QR with iPhone Camera and Google Lens, in **both** themes →
   contact card opens.
4. Press `g` then `o` / `a` / `s` anywhere; press `?` and check the console
   list shows all six destinations.
5. Visit `/opinions#op-10872456` → the Vu v. City of Atlanta card gets the
   amber arrival wash (after the pipeline template bump, per above).
6. Visit any bad URL → `ls /` now lists archive/, subscribe, colophon.
7. Hard-refresh once; confirm the new `?v=` strings in DevTools → Network.
