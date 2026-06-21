# Maintaining horowitz.law

How the site is kept current: what to edit by hand, what is generated and should be
left alone, and how a change reaches production. For the pipeline's internals and
tuning, see [PIPELINE.md](./PIPELINE.md); for what is planned, [ROADMAP.md](./ROADMAP.md).

## The one rule

The site is half hand-written and half machine-generated. Every generated file is
built from a source by `scripts/render.py`. Editing a generated file directly is wasted
work: the next render overwrites it, or it drifts out of sync with its source and CI
flags the drift. So edit the source, never the output.

A file is generated if either is true:

- it carries `<!-- name:start -->` / `<!-- name:end -->` markers (the region between
  them is injected from a source), or
- it appears in the generated list under "What to edit, and what to leave alone" below.

## The deploy loop

Almost every change follows one loop:

1. Edit the source file, in the GitHub web editor or a local copy.
2. Commit it to `main` (upload via the web UI, or commit directly).
3. If the source feeds a generated file, run the **render-sync** workflow from the
   Actions tab. It re-renders from the committed source and, only if the pages drifted,
   opens one review PR.
4. Review and merge that PR.

Two things to expect:

- A source-only change with no rendered output (a script, a workflow, `base.css`,
  `.gitignore`) needs no render-sync. CI runs on the push and that is enough.
- After a source PR that changes rendered output, the CI render-idempotency check goes
  red until render-sync catches up. That red is expected, not a failure: the source is
  ahead of the pages, which is exactly what render-sync exists to reconcile.

render-sync also runs once a day on its own, so a forgotten render heals within 24 hours.
If you upload regenerated pages alongside the source (the pre-rendered path), CI stays
green and no render-sync PR is needed.

Uploading by hand: drag a whole folder rather than selecting files one by one, which
silently strands some. The web UI drops dotfiles (anything starting with `.`) on a
folder drag, so upload `.gitignore`, `.well-known/`, and `.github/` files individually.

## What to edit, and what to leave alone

### Hand-edit (the sources)

- `opinions.json`: the Georgia Appellate Watch data, and the source of truth for every
  opinion page, feed, and permalink. The pipeline writes it, but you hand-edit it to fix
  or remove a card.
- `resume.md`: résumé content, rendered to `resume.html`.
- `README.md`: the readme, and the source of the colophon's shared prose.
- `siteconfig.py`: site identity, the `COVERAGE` coverage line, page descriptions, and
  the practice-area labels. One place for the copy that appears across pages.
- `scripts/*.py`: the pipeline, the renderer, and the helpers.
- `app.js`, `opinions.js`, `subscribe.js`, `base.css`, `functions/*`: front end and the
  subscribe serverless function.
- Config and assets: `_headers`, `_redirects`, `wrangler.toml`, `robots.txt`, both
  manifests, `.gitignore`, `.lycheeignore`, the workflows, `.github/dependabot.yml`,
  `.well-known/mta-sts.txt`, `humans.txt`, the fonts, icons, `og-card.jpg`, the portraits,
  and the vCard.
- The `<head>` and layout of any page, meaning the parts outside the markers and outside
  render's content blocks.

### Generated, never hand-edit (render writes these; run render-sync after editing the source)

- From `opinions.json`: `opinions.html`, `archive.html`, `opinions.xml`, `sitemap.xml`,
  `changes.html`, `changes.xml`, `stats.html`, `digests.html`, `o/*.html` (the
  permalinks), and `areas/*.json` (the per-area slices).
- From `resume.md`: `resume.html` (the four `resume-*` marker regions).
- From `README.md`: `colophon.html` (the five `col-*` marker regions). Colophon-only text
  outside those regions is edited in `colophon.html` directly.
- From `siteconfig.py`: the meta, Open Graph, and Twitter descriptions and identity fields
  on the pages (the `data-cfg` hooks), plus `.well-known/security.txt`.
- render also stamps the footer year and the asset `?v=` cache tokens on every page.

### Auto-written state, never hand-edit (committed on purpose, for transparency)

`opinions_state.json`, `opinions_rejections.jsonl`, `opinions_pipeline_log.jsonl`,
`treatment_state.json`, `status.json`, `scripts/golden_set.json`, `skill-authorities.json`,
and `.github/keepalive.txt`. These are written by the pipeline and its tools.

### Not in the repo (gitignored, written at runtime)

`digest_preview.html`, `alert_preview.html`, the `scripts/*_pr_body.md` files,
`scripts/court_drift.md`, and `scripts/link_rot.md`. These are dry-run previews and report
bodies a workflow produces and consumes; they are never committed.

## Common tasks

- **Change résumé content.** Edit `resume.md`, run render-sync. `resume.html` regenerates.
- **Fix or remove an opinion card.** Edit the card object in `opinions.json`, run
  render-sync. It regenerates the page, archive, feed, sitemap, and area slices, and
  prunes the permalink. The pipeline will not re-add a removed card, because its cluster
  stays in `opinions_state.json` seen_clusters.
- **Edit the colophon's shared prose** (stack, hosting, under the hood, what isn't here,
  source). Edit `README.md`, run render-sync. The five `col-*` regions regenerate.
- **Change a page description, the site identity, or the coverage line.** Edit
  `siteconfig.py` (`IDENTITY`, `COVERAGE`), run render-sync. The pages pick it up through
  the `data-cfg` hooks.
- **Add a monitored court or jurisdiction.** Edit one `JURISDICTIONS` entry in
  `scripts/jurisdictions.py`. If it joins the curated core, also update `COVERAGE` in
  `siteconfig.py` so the descriptions name it.
- **Add a case by hand.** Commit its CourtListener URL or cluster id to `queue.txt`; the
  queue workflow picks it up.
- **Bump a GitHub Action.** Dependabot opens the PR; review and merge it.
- **Tune the pipeline** (model ids, budgets, thresholds). These live in repository
  Variables, documented in PIPELINE.md, not in the code.

## The workflows

Thirteen workflows. Most run themselves; a few you trigger by hand from the Actions tab.

Automatic, on a schedule:

| Workflow | When | What it does |
| --- | --- | --- |
| opinions | every 4 hours | the funnel: scans CourtListener, proposes new cards via a review PR |
| treatment | Saturdays | reverse citation sweep for adverse treatment of published cards |
| maintain | daily | budget-gated upkeep: golden check, court check, repo keepalive |
| render-sync | daily | re-renders pages from `opinions.json`; opens a PR if they drifted |
| digest | Mondays | the weekly email digest, sent last in the cycle |
| lighthouse | Mondays | performance scores of the deployed site |
| links | Mondays | link-rot check, gentle on CourtListener |

Automatic, on every push or pull request:

| Workflow | What it does |
| --- | --- |
| ci | render idempotency and import checks for the pipeline and site |
| ruff | Python lint |

Run by hand, from the Actions tab:

| Workflow | When you run it |
| --- | --- |
| render-sync | after editing any source that feeds a generated page |
| golden-check | after changing a prompt or adding cases to the regression set |
| backfill | a one-off historical fill; leave dry_run checked the first time |
| queue | runs when you commit a URL to `queue.txt` |
| alert | an off-cadence email for a decision worth sending the day it lands |

## Things worth remembering

- render-sync owns every generated page. Never hand-upload a single regenerated page in
  isolation; it will drift from `opinions.json` or be overwritten on the next render. Let
  render-sync produce them, or upload the source and all regenerated files together.
- The `o/*.html` permalinks and `areas/*.json` slices are fully managed by render, which
  creates, updates, and deletes them. Do not add or remove them by hand.
- The committed state files are written by the pipeline and kept public on purpose. Do not
  hand-edit them.
- Cloudflare: Rocket Loader and Email Address Obfuscation must stay off. Both rewrite the
  HTML and inject scripts that break the hash-based Content Security Policy. The apex and
  `www` records stay proxied for certificate issuance, and the CAA record must include
  `pki.goog`.
