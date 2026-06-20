# Georgia Appellate Watch: self-publishing pipeline

A GitHub Actions job runs every four hours, checks CourtListener for new appellate
opinions across eight courts, filters them for an insurance-defense and civil-litigation
audience, drafts a short synopsis in the house style, and opens a pull request with the
additions. You review the PR and merge to publish. Cloudflare Pages deploys on merge.

Nothing publishes without your merge. That is the gate, and it is the recommended posture
while the feed matures.

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
Both default to the Sonnet triage model, so the summarizer is not grading its own work. Screen
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
alert-out half of a feed-to-drafting integration; the per-area drip-in half is still to come.

## How the pieces fit

- `opinions.json` is the single source of truth: a list of opinion entries.
- `scripts/update.py` is the pipeline: it fetches candidates, runs the funnel and the per-card
  checks, appends keepers to `opinions.json`, updates `opinions_state.json`, runs the forward
  escalation and the authority watch, calls the renderer, logs the run to
  `opinions_pipeline_log.jsonl` and drops to `opinions_rejections.jsonl`, and writes a PR summary
  to `scripts/pr_body.md`.
- `scripts/render.py` renders `opinions.json` into the cards in `opinions.html` (between the
  `opinions:start` / `opinions:end` markers), `archive.html`, the per-opinion permalink pages
  under `/o/`, the `opinions.xml` feed, and the `/changes` and `/stats` pages. It changes nothing
  outside its markers and stamps the footer year in place on the static pages.
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
- `scripts/safeio.py` is crash-safe atomic writes; `scripts/check_refs.py` and
  `scripts/check_site.py` are the CI guards.
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
- `maintain.yml` is the daily housekeeping: a keepalive commit, court-registry validation, and the
  golden cross-check.

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

Each run opens or updates a pull request. The description lists every added opinion with its court,
date, disposition, areas, and a link to the opinion, and calls out anything flagged for review (low
model confidence, a disposition it could not state, a citation that should not be there, a
cross-check or completeness flag, a treatment flag, or an authority-watch hit). Read the diff, fix
anything off in `opinions.json` on the branch if needed, and merge.

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

## Tuning

Set these as repository Variables, as `env:` on the funnel step in the workflow, or export them when
running locally:

- `OPINIONS_MODEL` (default `claude-opus-4-8`): the Tier 3 summarizer.
- `OPINIONS_TRIAGE_MODEL` (default `claude-sonnet-4-6`): the Tier 2 full-read gate. `""` disables it.
- `OPINIONS_SCREEN_MODEL` (default `claude-haiku-4-5-20251001`): the Tier 1 excerpt screen. `""`
  disables it.
- `OPINIONS_PRETRIAGE_MODEL` (default `claude-haiku-4-5-20251001` in code, shipped off in the
  workflow): the Tier 1.5 Haiku full-read screen. Set the repository Variable to enable it once the
  golden-set recall check passes. `""` disables it.
- `OPINIONS_CROSSCHECK_MODEL`, `OPINIONS_COMPLETENESS_MODEL` (default: the triage model): the
  per-card fidelity and completeness checks. `""` disables either.
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
          git add opinions.json opinions.xml opinions_state.json opinions.html opinions_rejections.jsonl opinions_pipeline_log.jsonl
          git diff --cached --quiet || git commit -m "opinions: add new appellate decisions"
          git push
```
Keep the prototype banner and the per-card "verify against the opinion" line in place while these are
AI-drafted summaries on a public page under your name.
