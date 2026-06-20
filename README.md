# horowitz.law

The source for [horowitz.law](https://horowitz.law), the personal site of Devin R. Horowitz,
a civil-litigation attorney in Marietta, Georgia. Hand-coded, no framework, no build step. It
also runs **Georgia Appellate Watch**, an automated feed of new appellate opinions written for
an insurance-defense and civil-litigation practice.

## The site

Vanilla HTML, CSS, and JavaScript, served as static files from Cloudflare Pages and deployed on
every push to `main`. No bundler, no framework, no compile step: what is in the repo is what
ships. JetBrains Mono is self-hosted in four weights, the ornamentation is SVG, and the styling
is plain CSS. Dark and light themes, a strict hash-pinned Content Security Policy, and an
installable PWA layer (a web-app manifest plus a small service worker) with offline reading of
the last-fetched opinions.

The full rundown of stack, hosting, and principles is in the
[colophon](https://horowitz.law/colophon).

## Georgia Appellate Watch

A GitHub Actions pipeline that, every four hours, checks CourtListener for new opinions across
eight courts (the Georgia appellate courts at the core, an Eleventh Circuit and U.S. Supreme
Court overlay, and Florida and Alabama as a supplementary tier), filters them for relevance
through a cheapest-to-most-expensive chain of models, drafts a short synopsis in the house style,
and opens a pull request. A human reviews and merges to publish; nothing reaches the page on its
own.

It also watches for law that moves. A forward tripwire inside the funnel and a weekend reverse
sweep flag when a later decision treats a published opinion, or an authority the practice's
drafting relies on, adversely. The machine only ever raises a flag to caution; declaring a case
bad law stays human work.

The architecture, one-time setup, and tuning are documented in [PIPELINE.md](./PIPELINE.md).

## Layout

- Root: the pages (`index.html`, `opinions.html`, `colophon.html`, and the rest), the shared
  styles (`base.css`), the client scripts (`app.js`, `opinions.js`), `opinions.json` (the feed's
  source of truth), and the generated feeds and state files.
- `scripts/`: the Python pipeline (`update.py`, `render.py`, `treatment.py`, and the supporting
  modules).
- `.github/workflows/`: the workflows that run and guard it.
- `functions/`: the one serverless seam, the subscribe endpoint, because a mailing list cannot
  be static.
- `o/`: the per-opinion permalink pages.

## Running it

The site needs no build: edit and push, and Cloudflare Pages deploys. The pipeline is documented
in [PIPELINE.md](./PIPELINE.md), including a local dry run that prints its decisions without
writing anything. The steps that change published content are gated behind a review PR; the rest
run on a schedule. Generated HTML belongs to the renderer, so edit `opinions.json` and the page
templates, never the generated cards between their markers.

## Use

The source is open. Borrow bits for a personal project; that is how the open web is supposed to
work. For commercial use, please reach out first.

---

Maintained by Devin R. Horowitz. The [roadmap](./ROADMAP.md) lives alongside this file.
