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

> **Layout.** The deployed site lives in `public/` (Cloudflare's `pages_build_output_dir`).
> Where this section names a served file by basename, it means the file under `public/` --
> e.g. `base.css` is `public/base.css`, `_headers` is `public/_headers`, `o/*.html` is
> `public/o/*.html`. Pipeline data and state (`opinions.json`, `opinions_state.json`, the
> logs, `queue.txt`) plus all tooling (`scripts/`, `functions/`, the workflows, `README.md`,
> config) stay at the repo root; these docs live in `docs/`. `functions/` must stay at the
> root, **not** inside `public/`, per Cloudflare's Pages Functions requirement.

### Hand-edit (the sources)

- `opinions.json`: the Georgia Appellate Watch data, and the source of truth for every
  opinion page, feed, and permalink. The pipeline writes it, but you hand-edit it to fix
  or remove a card.
- `../resume.md`: résumé content, rendered to `resume.html`.
- `../README.md`: the readme, and the source of the colophon's shared prose.
- `PIPELINE.md`, `ROADMAP.md`, `MAINTENANCE.md`: standalone docs, rendered into nothing.
  (`HANDOFF.md` is a development scratchpad, not part of the published site.)
- `siteconfig.py`: site identity, the `COVERAGE` coverage line, page descriptions, the
  practice-area labels, and `PAGES`, the registry of top-level pages that drives the `/404`
  link list and the sitemap's static URLs. One place for the copy and the page list that
  appear across pages.
- `scripts/*.py`: the pipeline, the renderer, and the helpers.
- `app.js`, `opinions.js`, `subscribe.js`, `sw.js`, `base.css`, `functions/*`: front end,
  the service worker, and the subscribe serverless function.
- Config and assets: `_headers`, `_redirects`, `wrangler.toml`, `robots.txt`, both
  manifests, `ruff.toml`, `lighthouserc.json`, `.gitignore`, `.lycheeignore`, the
  workflows, `.github/dependabot.yml`, `.well-known/mta-sts.txt`, `humans.txt`,
  `health.txt`, the fonts, icons, `og-card.jpg`, the portraits, and `contact.vcf`.
  (The QR contact card `devin-horowitz.vcf` is *generated* from `siteconfig.py`, not
  hand-edited — see the generated list below.)
- Page layout and heads: `index.html`, `404.html`, and `subscribe.html` are hand-edited
  shells. render stamps them and fills any marker regions they carry: the `/404` link list
  comes from `siteconfig.PAGES`, and `subscribe.html`'s area and jurisdiction choices from
  the registries. The heads of the generated pages are hand-edited the same way.

### Generated, never hand-edit (render writes these; run render-sync after editing the source)

- From `opinions.json`: `opinions.html`, `archive.html`, `opinions.xml`, `changes.html`,
  `changes.xml`, `stats.html`, `digests.html`, `o/*.html` (the permalinks), and
  `areas/*.json` (the per-area slices). Also `sitemap.xml`'s permalink entries; its static
  URLs come from `siteconfig.PAGES` (below).
- From `../resume.md`: `resume.html` (the four `resume-*` marker regions).
- From `../README.md`: `colophon.html` (the five `col-*` marker regions). Colophon-only text
  outside those regions is edited in `colophon.html` directly.
- From `siteconfig.py`: the meta, Open Graph, and Twitter descriptions and identity fields
  on the pages (the `data-cfg` hooks); the `/404` link list and `sitemap.xml`'s static URLs
  (both from `PAGES`); `.well-known/security.txt`; and `devin-horowitz.vcf`, the QR contact
  card, whose fields render stamps from the `siteconfig.py` identity.
- render also stamps the footer year and the asset `?v=` cache tokens on every page.

### Auto-written state, never hand-edit (committed on purpose, for transparency)

`opinions_state.json`, `opinions_rejections.jsonl`, `opinions_pipeline_log.jsonl`,
`treatment_state.json`, `status.json`, `scripts/golden_set.json`, `skill-authorities.json`,
`skill_alert_state.json`, and `.github/keepalive.txt`. These are written by the pipeline and
its tools.

### Not in the repo (gitignored, written at runtime)

`digest_preview.html`, `alert_preview.html`, the `scripts/*_pr_body.md` files, and the
workflow report/alert bodies (`scripts/court_drift.md`, `scripts/link_rot.md`,
`scripts/heartbeat_alert.md`, `scripts/feed_check_alert.md`, `scripts/diagnosis.md`,
`scripts/dep_review_comment.md`). These are dry-run previews and report bodies a workflow produces
and consumes; they are never committed.

## Common tasks

- **Change résumé content.** Edit `../resume.md`, run render-sync. `resume.html` regenerates.
- **Fix or remove an opinion card.** Edit the card object in `opinions.json`, run
  render-sync. It regenerates the page, archive, feed, sitemap, and area slices, and
  prunes the permalink. The pipeline will not re-add a removed card, because its cluster
  stays in `opinions_state.json` seen_clusters.
- **Edit the colophon's shared prose** (stack, hosting, under the hood, what isn't here,
  source). Edit `../README.md`, run render-sync. The five `col-*` regions regenerate.
- **Change a page description, the site identity, or the coverage line.** Edit
  `siteconfig.py` (`IDENTITY`, `COVERAGE`), run render-sync. The pages pick it up through
  the `data-cfg` hooks.
- **Add or change a top-level page.** Edit the `PAGES` registry in `siteconfig.py`, run
  render-sync. One tuple `(path, label, changefreq, priority, lastmod)` adds the page to
  both the `/404` link list and the sitemap's static URLs. An empty label keeps it out of
  the `/404` list (home is sitemap-only); an empty lastmod lets render date it from the
  feed. Never hand-edit `404.html`'s link list or `sitemap.xml`'s URLs.
- **Add a monitored court or jurisdiction.** Edit one `JURISDICTIONS` entry in
  `scripts/jurisdictions.py`. If it joins the curated core, also update `COVERAGE` in
  `siteconfig.py` so the descriptions name it.
- **Add a case by hand.** Commit its CourtListener URL or cluster id to `queue.txt`; the
  queue workflow picks it up.
- **Bump a dependency (GitHub Action or Python).** Dependabot opens the PR. A patch or minor bump
  auto-merges on green CI; a **major** bump is held for you, and `dep-review` posts an AI good-to-go/
  caution note on it first — read that, then merge (or close) by hand.
- **Tune the pipeline** (model ids, budgets, thresholds). These live in repository
  Variables, documented in PIPELINE.md, not in the code.

## The workflows

Most run themselves; a few you trigger by hand from the Actions tab.

Automatic, on a schedule:

| Workflow | When | What it does |
| --- | --- | --- |
| opinions | every 4 hours | the funnel: scans CourtListener, auto-publishes clean cards straight to `main`, and routes held cases to a review PR |
| treatment | Saturdays | reverse citation sweep for adverse treatment of published cards |
| maintain | daily | budget-gated upkeep: golden check, court check, feed-shape canary, repo keepalive |
| render-sync | daily | re-renders pages from `opinions.json`; opens a PR if they drifted |
| model-watch | daily | checks the Models API for a newer Claude model in a pinned tier; opens a bump PR gated by the golden set |
| heartbeat | daily | dead-man's-switch: opens a tracking issue if the funnel stalls (no scan in 48h) or stops finding cards (30d); the one alert that fires when runs stop |
| automerge | every 6 hours | merges the verified-safe self-healing PRs (render-sync, and patch/minor dependabot bumps for GitHub Actions and Python deps) so they ship untended; holds major bumps and model-watch for a human |
| dep-review | every 6 hours | posts a one-time AI good-to-go/caution note (Fable, via the 50%-priced Batch API) on each held major dependabot bump, scoped to how this repo uses the dep, from the PR's changelog. Advisory; merges nothing |
| digest | Mondays | the weekly email digest, sent last in the cycle |
| lighthouse | Mondays | performance scores of the deployed site |
| links | Mondays | link-rot check, gentle on CourtListener |

Automatic, on every push or pull request:

| Workflow | What it does |
| --- | --- |
| ci | render idempotency and import checks for the pipeline and site |
| ruff | Python lint |

Automatic, when a monitor opens a tracking issue:

| Workflow | What it does |
| --- | --- |
| diagnose | posts one best-effort AI first-pass diagnosis (Fable, via the 50%-priced Batch API, from the issue text + this runbook) as a comment, so you start with a hypothesis. An aid, not an authority; fails silently and never comments on human-filed issues |

Automatic, on a review-lane event (the held-case PR the funnel opens):

| Workflow | What it does |
| --- | --- |
| review-veto | on a `/veto <id>` or `/decline <id>` comment on the review PR, drops that case from the bundle before you merge |
| review-apply | when the review PR merges, applies the cases that remained to `main` and re-renders |

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
- Internal links use extensionless clean URLs (`/archive`, `/o/<cluster_id>`), which
  Cloudflare Pages serves from the matching `.html` file. Link to `/archive`, not
  `/archive.html`.
- The committed state files are written by the pipeline and kept public on purpose. Do not
  hand-edit them.
- The service worker `sw.js` is the one script render does not cache-stamp, by design. When
  you change its caching strategy, bump its `CACHE` constant by hand.
- Every page carries one identical inline pre-paint script, kept on `opinions.html` and
  spliced verbatim into the other pages by render, with its `sha256` allow-listed in the
  `_headers` CSP. If you edit that script, regenerate its hash and set it in `_headers` in
  the same commit; the ci `check_site` step fails until they match, and prints the exact
  hash to use.
- Cloudflare: Rocket Loader and Email Address Obfuscation must stay off. Both rewrite the
  HTML and inject scripts that break the hash-based Content Security Policy. The apex and
  `www` records stay proxied for certificate issuance, and the CAA record must include
  `pki.goog`.
- Model pins do not drift. From the 4.6 generation on, a Claude model id is a fixed
  snapshot: a newer model ships under a new id (`claude-sonnet-5`), and the pinned id keeps
  serving the same weights. The pins live as the `|| 'id'` fallback in the funnel workflows
  (and the matching defaults in `update.py` / `treatment.py`); a repo Variable overrides a
  pin without editing a file. `model-watch` proposes upgrades, but a pin only changes when
  a PR merges (or you set a Variable).
- `model-watch` needs a `MODEL_WATCH_TOKEN` secret to open its PR, because a bump edits the
  workflow files and the default token cannot push to `.github/workflows/`. Use a
  fine-grained PAT scoped to this repo with Contents, Pull requests, and Workflows set to
  write. Without it the daily check and the golden-set eval still run and report on the
  Actions summary; only the auto-PR is skipped. If a bump PR touches a tier whose pin is
  also set as a repo Variable, update that Variable to match, or the Variable will keep
  overriding the merged default.
- The self-healing PRs merge themselves, so the fix ships without you. The `automerge` workflow
  merges a **render-sync** PR only after re-verifying it is a faithful re-render (data untouched,
  only `public/` changed, and re-rendering the PR head yields no further diff), and a **dependabot**
  bump (a GitHub Actions pin or a Python dependency) only when its CI checks are all green — but
  **patch and minor only**. A **major** dependabot bump is held for you: CI here is hermetic and
  never runs the risky runtime paths (pypdf on real PDFs), so green CI does not prove a major bump
  safe. It fails closed: a bump whose major it cannot parse as unchanged (an odd title, a grouped
  update) is held too. **model-watch is likewise never auto-merged** — it changes the model that
  writes the legal cards, and CI never runs that model, so you review and merge those by hand. To
  pause all auto-merging, disable the `automerge` workflow in the Actions tab.
- Held major dependabot bumps get an AI second opinion. `dep-review` (a scheduled poll, like
  automerge) posts one comment per major-bump PR — good-to-go, caution, or hold — reasoning over the
  changelog in the PR body against a note of how this repo actually uses that dependency
  (`scripts/dep_review.py`, `DEP_USAGE`). It is advisory and merges nothing; a broken run posts
  nothing. Add a `DEP_USAGE` entry when a new dependency is worth a repo-specific review.
- Latency-tolerant model calls run through the **50%-priced Batch API** (`batch.py`). `diagnose` and
  `dep-review` always do; `maintain`'s daily guard trickle does **by default** now (`MAINTAIN_BATCH`,
  set it to `0` to force the synchronous path). The trade is latency: the run polls for the batch
  (usually minutes), capped by the matching `*_BATCH_SEC` budget (`diagnose`/`dep-review` 480s;
  `maintain` 900s, which must stay under the 30-min job); a slow batch just posts nothing / defers
  the slice that run (best-effort). `backfill` batches too, but stays opt-in (`BACKFILL_BATCH`) since
  it's a manual one-off. The **daily funnel is deliberately synchronous** — it publishes cards
  promptly and the Batch API is async. `diagnose` / `dep-review` models are repo Variables
  (`DIAGNOSE_MODEL` / `DEP_REVIEW_MODEL`, default Fable) if you ever want to trade quality for a
  still-lower price.
- `heartbeat` is the backstop for silent stalls. Every other alert is an issue opened by a
  workflow that *failed* — which only helps if that workflow still runs. Heartbeat instead
  reads the committed freshness markers (`public/status.json` `scanned_at`, newest card
  `first_seen`) and opens one tracking issue if the funnel stops scanning (48h) or stops
  finding cards (30d). It cannot catch a *total* 60-day auto-disable (that takes its own cron
  down too); for that, set a `HEARTBEAT_PING_URL` secret to an external cron-monitor ping URL
  (e.g. a free healthchecks.io check) and add its host to the workflow's allowed-endpoints —
  heartbeat pings it on healthy runs, so the monitor alerts you if the pings ever stop.
- The `maintain` **feed-shape canary** is heartbeat's faster, more specific sibling for the
  scariest failure mode: a silent CourtListener feed-format change that zeroes discovery. Heartbeat
  would catch it as content-stale after 30 days; the canary runs the funnel's real feed parser over
  the live feeds daily and files a "Feed-shape drift detected" issue **same-day** when the feeds have
  entries but the parser extracts none. If it fires, compare a live feed
  (`courtlistener.com/feed/court/<id>/`) against `_parse_feed` in `scripts/update.py` and fix the
  parser — the `/opinion/<id>/` link pattern is the usual culprit.
