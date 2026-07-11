# Georgia Appellate Watch: self-publishing pipeline

A GitHub Actions job runs every four hours, checks CourtListener for new appellate
opinions across eight courts, filters them for an insurance-defense and civil-litigation
audience, drafts a short synopsis in the house style, and publishes. It runs in two lanes.
A clean, additive card -- a new opinion that touches no existing card and trips no guard --
is machine-verified and published straight to `main` with no human step. A card that would
overrule or modify an existing card, or that a guard flags (low confidence, a check that
could not run, a citation that should not be there), is held in a single bundled review PR
for a person to merge or veto. Cloudflare Pages deploys on every push to `main`.

Read the trust model below before assuming a card on the site was seen by a human. Most
were not.

> **Layout.** The deployed site lives in `public/` (Cloudflare's `pages_build_output_dir`).
> Where this doc names a rendered/served file by basename -- `opinions.html`, `archive.html`,
> `sitemap.xml`, the `/o/` permalinks -- it means the file under `public/`. Pipeline data
> (`opinions.json`, `opinions_state.json`, the logs), the scripts, and `functions/` stay at
> the repo root; `functions/` must NOT move into `public/` (Cloudflare requirement).

## Trust model

This is the single most important fact about the system, and it changed with the two-lane
design: **a card published in the auto lane may never have passed human eyes.** The site once
had one gate -- every card was human-verified before publish. Now the auto lane is
*machine*-verified before publish, and only a flagged or unverifiable card reaches a person.

What "machine-verified" means for an auto card: it cleared the Tier 1 screen, the Tier 2
full-read triage, the Opus summarizer, and the post-summary guards (self-citation, a
disposition it could actually state, a cross-check against the opinion text, completeness).
Those guards **fail closed** -- anything they cannot verify (a check that errored, opinion
text that would not load) is treated as a failure and routed to the review lane, not
published -- and anything that would overrule or modify an existing card is held the same
way. That is deliberate and correct.

The residual exposure is narrow and specific: a fluent, confident, *wrong* card that clears
both the screen and the guards and publishes with no human. For a tool consulted at drafting
time, a card that looks right and is not is the worst failure mode -- worse than a late card
or a missing one. Three things make it tolerable for a solo curator, and none removes it: the
changes ledger (`/changes`) records every later correction, so a bad card leaves a trail; the
daily maintenance job re-validates a rotating slice of already-published cards against their
opinion text; and every card is hand-editable in `opinions.json` (see "Editing content by
hand"). Every card also carries the prototype banner and the per-card "verify against the
opinion before relying" line.

If you maintain this: do not assume a card on the site was reviewed. Raise the bar -- disable
the auto lane and route everything to review -- by making the guards stricter or by treating
more cases as "held" in `scripts/review_store.py`; the two-lane machinery does not otherwise
change.

## Coverage

Eight courts across three jurisdictions, in three postures:

- Georgia is the curated core: the Supreme Court of Georgia and the Court of Appeals of
  Georgia, read to the full editorial bar.
- A federal overlay rides alongside Georgia: the U.S. Court of Appeals for the Eleventh
  Circuit and the U.S. Supreme Court, for the decisions that reach a Georgia civil practice.
- Florida and Alabama are supplementary, "also pulled" rather than curated: the Supreme
  Court of Florida and the District Court of Appeal of Florida, and the Supreme Court of
  Alabama and the Court of Civil Appeals of Alabama. Same relevance areas, lighter touch,
  and the site keeps the Georgia Appellate Watch name.

The court set, labels, and citation suffixes are defined once in `scripts/jurisdictions.py`;
`OPINIONS_JURISDICTION` selects the active jurisdiction and `OPINIONS_COURTS` narrows the
active court set, both without editing code.

## The funnel

Candidates pass through four model tiers, cheapest first, so the expensive model only ever
touches confirmed keepers:

- Tier 1, screen (Haiku): reads the case name and opening excerpt only and drops the obvious
  non-matches.
- Tier 1.5, pretriage (Haiku): reads the full opinion at the same permissive bar and drops
  only what the full text shows cannot belong, so the costly Sonnet read lands only on
  plausible keepers. High-recall: anything in scope or in doubt passes. Off by default in the
  workflow; enable via `OPINIONS_PRETRIAGE_MODEL` once `python scripts/golden_check.py recall`
  passes.
- Tier 2, triage (Sonnet): reads the full opinion and decides, against a narrow bar, whether
  it belongs in the feed.
- Tier 3, summarize (Opus): reads the full opinion plus the triage note and writes the
  published synopsis, or still declines.

Each drafted card is then checked twice more: a fidelity crosscheck (does the card match the
opinion) and a completeness check (does the card omit a material holding in a covered area).
Both default to the Sonnet triage model, so the summarizer is not grading its own work. Both are
hardened against a false flag: each must quote the verbatim text it faults (the crosscheck the card
span it says is wrong, the completeness check the opinion span it says was omitted), so a flag whose
quote is not present is dismissed, and on a flag each re-asks and stands only on a majority of
attempts (`OPINIONS_CROSSCHECK_TRIES`, `OPINIONS_COMPLETENESS_TRIES`). Screen
and triage drops are appended to `opinions_rejections.jsonl` for periodic recall review.

## Catching law that moves

A published card can be overtaken by a later decision. Two processes watch for it, and both
only ever raise a card to "caution"; declaring a case bad law stays human work, done on a
citator and applied by editing `opinions.json`.

- Forward escalation (inside the funnel): while triage reads a new opinion, it is also handed
  the cases already carded and asked to flag any the new opinion treats adversely. Each flag is
  confirmed by an Opus audit before the cited card is raised to caution, whether or not the new
  opinion itself earns a place in the feed.
- The reverse sweep (`scripts/treatment.py`, on weekends): walks every card's citation graph on
  CourtListener, reads each new citing passage, and judges adverse treatment. It is the thorough
  backstop to the forward pass and reaches citers, including criminal and out-of-scope ones, that
  the daily screen drops before triage ever sees them.

Both record a finding through `scripts/treatment_core.py`, so a card carries one consistent
shape no matter which path flagged it, and a flag a human has cleared is never re-raised.
Confirmed corrections surface in the weekly digest and the `/changes` ledger.

## The authority watch

The forward-escalation tripwire also guards the authorities the practice's drafting skills rely
on, not only the published cards. `scripts/skill_authorities.py` extracts, from those skills, the
statutes and controlling cases each depends on into `skill-authorities.json`. `scripts/skill_alert.py`
adds the case authorities to the triage watch-list, most of them older controlling cases that are
not in the feed at all; when a new opinion is confirmed to treat one adversely, the finding is
recorded in `skill_alert_state.json` and routed back to the skills that rely on it. This is the
alert-out half of a feed-to-drafting integration. Its per-area drip-in half now has a source:
`scripts/render.py` publishes `/areas/<area>.json` per practice area, a deterministic extract of
`opinions.json` carrying each opinion's synopsis and treatment state. What remains is wiring a
drafting skill to read its area slice at draft time.

## How the pieces fit

- `opinions.json` is the single source of truth: a list of opinion entries.
- `scripts/update.py` is the pipeline: it fetches candidates, drops any that duplicate a carded
  case (a CourtListener twin or corrected republish, matched on court and a shared docket or
  same-day parties), runs the funnel and the per-card
  checks, appends keepers to `opinions.json`, updates `opinions_state.json`, runs the forward
  escalation and the authority watch, calls the renderer, logs the run to
  `opinions_pipeline_log.jsonl` and drops to `opinions_rejections.jsonl`, and writes a PR summary
  to `scripts/pr_body.md`.
- `scripts/render.py` renders `opinions.json` into the cards in `opinions.html` (between the
  `opinions:start` / `opinions:end` markers), `archive.html`, the per-opinion permalink pages
  under `/o/`, the `opinions.xml` and `changes.xml` feeds, the `/changes`, `/stats`, and
  `/digests` pages, the `sitemap.xml` permalink entries (its static URLs come from
  `siteconfig.PAGES`), and the per-area slices under `/areas` (the drip-in source, see The
  authority watch). It changes nothing outside its markers and stamps the footer year in
  place on the static pages.
- `scripts/jurisdictions.py` is the single source of truth for the court set, labels, and the
  citation and docket patterns; `update.py`, `render.py`, and `treatment.py` all read it.
- `scripts/cl_rate.py` is the rate governor: it paces every CourtListener call inside the free
  tier's rolling per-minute, per-hour, and per-day windows, waiting for a window to refill rather
  than overrunning it.
- `scripts/golden_check.py` re-runs a set of known cases against the live prompts so a quiet model
  change cannot silently move the editorial line.
- `scripts/digest.py` and `scripts/alert.py` build the weekly and ad-hoc emails (Resend).
- `scripts/backfill.py` and `scripts/queue_cases.py` fill historical gaps and resolve specific
  cases on demand.
- `scripts/safeio.py` is crash-safe atomic writes; `scripts/check_refs.py`,
  `scripts/check_site.py`, and `scripts/test_update.py` (hermetic unit tests for the per-card
  guards) are the CI guards.
- The state files (`opinions_state.json`, `treatment_state.json`, `skill_alert_state.json`) record
  what has already been processed so nothing is redone; each is capped to bound growth.

## The workflows

Thirteen workflows under `.github/workflows/`:

- `opinions.yml` runs the funnel every four hours and opens the PR. If a content PR is already
  open, a scheduled run skips, so review is never raced.
- `treatment.yml` runs the weekend reverse sweep.
- `golden-check.yml` runs the golden set; `backfill.yml` and `queue.yml` are the historical and
  on-demand fetchers.
- `digest.yml` and `alert.yml` send the email.
- `render-sync.yml` regenerates derived HTML when a source edit changes it (see "Editing content
  by hand").
- `ci.yml`, `ruff.yml`, `links.yml`, and `lighthouse.yml` are the guards: a compile-and-import
  smoke test with a render-idempotency check, the linter, link-rot, and a performance budget.
- `maintain.yml` is the daily housekeeping: a keepalive commit, court-registry validation, the
  golden regression check, and a budget-gated re-validation of a rotating slice of published cards
  that re-runs both per-card guards (fidelity and completeness) against each card's own opinion.

Every workflow that touches the network runs under Harden-Runner in block mode: each job declares
the exact hosts it may reach and the runner blocks the rest, so a compromised dependency cannot
exfiltrate. The two jobs that fetch unbounded external URLs, link-checking and the performance
audit, run in audit mode by necessity. Dependency bumps come through Dependabot.

## Editorial and commit model

Two kinds of run, kept apart on purpose:

- A content run (new cards, or a card raised to caution) writes `opinions.json` and rides a review
  PR titled "Georgia Appellate Watch: new opinions". Nothing on the public page changes until you
  merge.
- A no-op run (the funnel carded nothing) commits only bookkeeping, the seen-state and run stats,
  straight to main with `[skip ci]`.

The state files that must stay in step with `opinions.json` ride the PR on a content run and commit
straight to main on a no-op run. The run guard means only one PR is ever open, so that state never
double-advances.

## One-time setup

1. Add repository secrets (Settings > Secrets and variables > Actions > New repository secret):
   - `ANTHROPIC_API_KEY` (required).
   - `COURTLISTENER_TOKEN` (optional but recommended; it raises CourtListener's rate limits).
     Create a free account at courtlistener.com and copy the token from your profile's API page.
   - `RESEND_API_KEY` (only if you send the email digest).
2. Allow Actions to open pull requests (Settings > Actions > General > Workflow permissions):
   - Select "Read and write permissions".
   - Check "Allow GitHub Actions to create and approve pull requests".

The model ids live in repository Variables (Settings > Secrets and variables > Actions > Variables)
and fall back to built-in defaults when unset, so a model id can be changed without editing code.

## Test it before trusting the schedule

- From GitHub: open the Actions tab, select "Georgia Appellate Watch", and use "Run workflow" (the
  `workflow_dispatch` trigger). Watch the log, then review the PR it opens.
- Locally, without writing anything:
  ```
  ANTHROPIC_API_KEY=sk-... COURTLISTENER_TOKEN=... DRY_RUN=1 python scripts/update.py
  ```
  It prints the candidates, the keep/drop decision for each, and the PR summary, but touches no
  files.

## Reviewing and publishing

Only the review lane opens a pull request; the auto lane publishes straight to `main` (see the
trust model above). When a run holds one or more cases, it opens or updates a single bundled
review PR on the `bot/opinions-review` branch. The description lists every held case with its
court, date, disposition, areas, and a link to the opinion, and says why it was held (low model
confidence, a disposition it could not state, a citation that should not be there, a cross-check
or completeness flag, an overrule/modify of an existing card, a treatment flag, or an
authority-watch hit). Read the diff, fix anything off in `opinions.json` on the branch if needed,
and **merge to accept the batch** -- or drop a single case by commenting on the PR. Two commands
drop a case (both apply only the remaining cases on merge), differing in what happens to the
dropped case:

- **`/veto <cluster_id>`** -- the draft was bad; leave the case un-seen and redraft-log it, so a
  later run rediscovers and redrafts it. Not permanent.
- **`/decline <cluster_id>`** -- the case is not worth carding (a thin jurisdictional order, out
  of scope); mark it **seen** so the funnel never redrafts it. Permanent, and it costs nothing on
  later scans -- the paper cut a bare veto leaves (the case returns, at Opus cost, every scan it is
  still in the feed window) is exactly what `/decline` closes. Recorded in `review/declined.json`
  on the branch and read by `review_apply` at merge.

How the review branch is built, and why the push step looks the way it does -- do not
"simplify" it. Every run **rebuilds** `bot/opinions-review` from current `main` and re-adds the
staged `review/` files, so the branch's only diff from `main` is those data files. That is the
load-bearing invariant: merging the PR can never revert an auto card that landed on `main`
meanwhile, which is what fixed the old stuck-PR failure. Because the branch is rebuilt each run,
the funnel force-pushes it -- but with `--force-with-lease`, not `-f`, so a `/veto` that landed
since the run's fetch makes the lease stale and the push is **rejected** rather than silently
clobbering the veto (the run then alerts, and the next scan rebuilds with the veto intact). The
`/veto` workflow correspondingly retries against a moved branch and shouts "do not merge until
confirmation posts" if it cannot converge. This reconciliation currently lives in workflow shell
(`opinions.yml`, `review-veto.yml`) and is not under test; treat it as correctness-critical.

## Editing content by hand

`opinions.json` is the source of truth. To add, correct, or remove an entry by hand, edit
`opinions.json` and then rebuild:
```
python scripts/render.py
```
Do not edit the cards between the markers in `opinions.html`, the archive, the permalink pages, or
the feeds directly; the renderer overwrites them. A source edit that changes derived HTML rides a
branch and PR, where CI flags render-idempotency (expected), and `render-sync.yml` then regenerates
and commits the HTML. An edit that touches no derived output, a workflow, a config, a doc, or a
state file, goes straight to main.

Each entry is one JSON object. The fields:

- `cluster_id` (integer): the CourtListener cluster id. It is the unique key and the
  permalink slug (`/o/<cluster_id>`), so it must be unique and must never change.
- `name`, `court`, `division`, `date` (`YYYY-MM-DD`), `dockets` (list), `disposition`, and
  `precedential` (`published` or `unpublished`): the case identity. `division` may be empty
  for a court without one.
- `areas` (list): practice-area codes from `siteconfig.AREA_CODES`; they drive the filters,
  the `data-areas` attribute, and the RSS categories.
- `url`: the CourtListener opinion link. `official_url` (optional): the court's own PDF.
- `synopsis`: the summary shown on the card and in the feed. `why`: the one-line relevance.
- Optional enrichment: `law_applied`, `additional_holdings` (list), `first_impression`,
  `tort_reform`, and `editor_note`.
- Pipeline-managed, leave them alone: `first_seen`, and the `treatment*` and `treated_by`
  fields the authority watch sets.

When adding an entry by hand, copy an existing one for shape.

## Tuning

Set these as repository Variables, as `env:` on the funnel step in the workflow, or export them when
running locally:

- `OPINIONS_MODEL` (default `claude-opus-4-8`): the Tier 3 summarizer.
- `OPINIONS_TRIAGE_MODEL` (default `claude-sonnet-5`): the Tier 2 full-read gate. `""` disables it.
- `OPINIONS_SCREEN_MODEL` (default `claude-haiku-4-5-20251001`): the Tier 1 excerpt screen. `""`
  disables it.
- `OPINIONS_PRETRIAGE_MODEL` (default `claude-haiku-4-5-20251001` in code, shipped off in the
  workflow): the Tier 1.5 Haiku full-read screen. Set the repository Variable to enable it once the
  golden-set recall check passes. `""` disables it.
- `OPINIONS_CROSSCHECK_MODEL`, `OPINIONS_COMPLETENESS_MODEL` (default: the triage model): the
  per-card fidelity and completeness checks. `""` disables either.
- `OPINIONS_CROSSCHECK_TRIES` (default `3`): how many times the fidelity crosscheck re-asks on a
  flag, standing only on a majority, so a one-roll false flag is damped. `1` keeps the
  verbatim-quote grounding but disables the re-ask.
- `OPINIONS_COMPLETENESS_TRIES` (default `3`): the same re-ask consensus for the completeness check.
  `1` keeps the grounding but disables the re-ask.
- `OPINIONS_AUDIT_MODEL` (default: the summarizer model): confirms a flagged adverse-treatment event,
  for both a carded case and a watched authority, before anything is flagged. Not disablable.

The three derived checks track a primary model by default: cross-check and completeness follow
`OPINIONS_TRIAGE_MODEL`, and the audit follows `OPINIONS_MODEL`. So a full model-generation upgrade is
a handful of Variable edits and the derived checks move with them unless you set their own Variables.

- `OPINIONS_JURISDICTION` (default `ga`): the active jurisdiction key in `scripts/jurisdictions.py`.
- `OPINIONS_COURTS` (default: the active jurisdiction's full court set, currently the eight courts
  listed under Coverage): CourtListener court ids, comma-separated, to narrow the active set.
- `OPINIONS_LOOKBACK` (default `21`): days to look back on the first run, before state exists.
- `OPINIONS_MAX` (code default `25`; the workflow raises it to `80` for heavy filing days): cap on
  opinions evaluated per run.
- `OPINIONS_MAXCHARS` (default `60000`): characters of opinion text sent to triage and the summarizer.
- `OPINIONS_MAX_TOKENS` (default `4096`): summarizer output token cap.
- `OPINIONS_DEBUG=1`: log every model call and full API error bodies.
- Schedule: edit the `cron` line in the workflow (`17 */4 * * *`, every four hours). It is in UTC.

A handful of other guards (`OPINIONS_BUDGET_SEC`, `OPINIONS_BREAKER`, `OPINIONS_SEARCH_BUDGET_SEC`,
`OPINIONS_SEEN_CAP`, `DRY_RUN`) are documented at the top of `scripts/update.py`. The CourtListener
rate limits and the reverse-sweep budget are documented at the top of `scripts/cl_rate.py` and
`scripts/treatment.py`.

## Cost

Modest, and the funnel keeps it that way: Haiku screens everything cheaply, Sonnet reads only the
survivors, and Opus only ever drafts confirmed keepers, so a typical four-hour slot is a few
opinions through the full stack, pennies on the API. The only fixed bill is the yearly .law renewal;
the CDN, the runners, and the repository are free.

## Dropping the gate later

Once you trust it, you can publish without the PR step. Replace the "Open pull request" step with a
direct commit:
```yaml
      - name: Commit and push
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add opinions.json public/opinions.xml opinions_state.json public/opinions.html opinions_rejections.jsonl opinions_pipeline_log.jsonl
          git diff --cached --quiet || git commit -m "opinions: add new appellate decisions"
          git push
```
Keep the prototype banner and the per-card "verify against the opinion" line in place while these are
AI-drafted summaries on a public page under your name.
