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

### The Fable senior review of held cases

There is a second machine layer, and it can *reduce* the human queue. When a card is held, the
most capable model -- **Claude Fable 5** -- adjudicates the flag against the actual opinion text
(`scripts/fable_review.py`). It runs in one of three modes (`OPINIONS_FABLE_REVIEW`):

- `off` -- no Fable step.
- `advisory` -- Fable assesses every held case and its verdict + recommendation (accept/veto/
  decline) is attached to the review PR, but it clears nothing; every held card still waits for you.
- `clear` (current default) -- in addition, Fable **auto-publishes** a held card it is highly
  confident is a false positive, moving it from the review lane to the auto lane.

This is a deliberate extension of the trust model: in `clear` mode, **a card that a guard held can
be published without human eyes if Fable clears it.** It is fail-closed and narrow. A clear
requires the full triple -- `is_false_positive` AND `high` confidence AND an `accept`
recommendation -- on adequate opinion text; any error, thin text, lesser confidence, or a
`veto`/`decline` recommendation leaves the card held. Fable can only ever *reduce* holds, never add
a publish on doubt. Overrule/modify holds are effectively never cleared, because Fable is told not
to clear one unless the text makes plain it does not change existing law -- a bar it rarely clears.

Every Fable verdict is logged to `opinions_fable_review.jsonl` (bounded), and an auto-cleared card's
publish PR records that it was Fable-cleared and why, so nothing clears invisibly. To watch Fable's
judgment before trusting it, set `OPINIONS_FABLE_REVIEW=advisory` in the environment of the run; to
turn it off, `off`. It is an env var, not a repo Variable -- no workflow plumbs one (see "Changing a
model" below).

If you maintain this: do not assume a card on the site was reviewed by a person -- most were not,
and in `clear` mode even some *held* cards were resolved by Fable, not you. Raise the bar -- route
everything to a human -- by setting `OPINIONS_FABLE_REVIEW=advisory` (Fable still advises, clears
nothing) or `off`, by making the guards stricter, or by treating more cases as "held" in
`scripts/review_store.py`.

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
  plausible keepers. High-recall: anything in scope or in doubt passes. On by default in the
  workflow (the `python scripts/golden_check.py recall` gate passed before it went live);
  `OPINIONS_PRETRIAGE_MODEL` in the run's environment is a break-glass override, `""` disables.
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

That recall review is itself automated (tier 2.5, the "smell test"), and it runs in TWO passes
that cover different stages. Keep them apart, because which drops get a same-run second opinion
and which wait a week is not obvious from the config:

- **In-run**, inside `update.py`: **triage drops only**, held as they are dropped and audited
  before the run ends, so a suspect one can be escalated to the summarizer in the same run and
  still be carded. This is hardcoded at the triage drop site; `SMELL_STAGES` does not reach it.
- **Weekly**, `smell.yml` running `scripts/smell_check.py` (Mondays 13:43 UTC): the retro pass
  over the logged backlog, and the ONLY pass that audits **screen** drops. It reads
  `siteconfig.SMELL_STAGES`, and a suspect it finds is surfaced on a tracking issue with
  ready-to-paste `queue.txt` force lines rather than escalated automatically.

So a screen drop's recall check is weekly and editor-mediated, not same-run and automatic. That
is the deliberate trade: screen runs on every candidate, so auditing it in-run would put the
audit in the funnel's hot path for the cheapest, highest-volume gate.

`siteconfig.SMELL_STAGES` is today `["triage", "screen"]`. Screen was added on 2026-08-16 after an
audit of the log found 12 adversarial `In re: A v. B` captions discarded on the prefix alone, two
of them Supreme Court of Alabama insurance decisions that belonged in the feed: of 1,502 logged
drops, the 1,163 screen drops had never had a reason checked, because screen is the blindest gate
and was the only unwatched one. Pretriage is deliberately still absent -- its drops are unaudited
too, but it reads the full opinion, so its reasons are not caption guesses; it goes in when there
is evidence it needs watching, not on symmetry. `SMELL_STAGES=triage,screen,pretriage` overrides
for one run.

The audit model is `siteconfig.SMELL_MODEL`. Empty (the default) means INHERIT: it resolves to the
treatment-audit model, which inherits the tier-3 summarizer pin, so repinning the summarizer once
carries the whole chain. Set an id there to pin the audit independently, or `"off"` to disable the
pass; `OPINIONS_SMELL_MODEL` is a one-run override only. It is written that way because the
inheritance had been severed in practice -- two workflows set `OPINIONS_SMELL_MODEL` to a
hardcoded id, which always won over the fallback, so a repin of the summarizer would have left the
audit on the old model silently and forever.

Both passes judge the same way. The one-line drop reason is the only trace a drop leaves, so the
smell model reads those reasons on their face -- no opinion text -- and marks any that state no disqualifier
the triage standard recognizes (a keep-shaped topic label, a ground like "unpublished" that the
standard does not use, a missing reason). It is also shown the case NAME, which is what makes the
pass work on screen drops specifically: a reason of juvenile, dependency or probate for a case
captioned against a named insurer, company or government body is a guess from the caption rather
than a disqualifier, and `In re` / `Ex parte` carry no subject at all. In the in-run pass a
suspect drop is escalated to the tier-3 summarizer for one full read in the same run, which cards
it or confirms the drop -- the same second
opinion the queue's `!` force flag buys, automated, capped at
`OPINIONS_SMELL_MAX_ESCALATIONS` per run (default 5) and twin-checked against the run's dedup
index like every other route to the summarizer. The pass is fail-open in both senses: nothing
in it -- including a misconfigured smell model -- ever aborts the run or changes a drop, and a
drop the model never actually judged is left un-stamped rather than marked clean, so it stays
visible downstream. Each genuinely audited rejection record is stamped with the verdict
(`smell`, `smell_note`, `smell_outcome`; an escalation whose read never happened is stamped
`deferred`), and the weekly `smell.yml` job (`scripts/smell_check.py`) closes the loop: it
audits the logged backlog plus anything un-stamped or deferred, persisting progress after
every chunk, and surfaces suspects on a tracking issue with ready-to-paste `queue.txt` force
lines for editor review.

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
alert-out half of a feed-to-drafting integration.

One thing to know about that manifest: it is generated from the skill tree at `/mnt/skills/user`,
which no workflow has, so it cannot regenerate in CI. It is regenerated by hand and committed, and
between those commits it is frozen against a skill tree that keeps moving -- while `skill_alert.py`
fails open on a manifest it cannot read, so a stale one looks exactly like a working watch. The
daily heartbeat therefore checks its `generated_at` as a separate signal
(`python scripts/heartbeat.py --skill-manifest`, exit 5) and files its own tracking issue past
`siteconfig.SKILL_MANIFEST_MAX_AGE_DAYS` (90). Regenerate with `python scripts/skill_authorities.py`
where the tree is mounted; curated edits survive. Set the threshold to `0` to retire the check if
the skill tree is no longer maintained -- deliberately, rather than by ignoring the issue.

Its per-area drip-in half now has a source:
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

## Changing a model

The model ids live **in the code**, not in repository Variables. Every funnel tier is pinned in
`scripts/update.py` (`MODEL`, `TRIAGE_MODEL`, `SCREEN_MODEL`, and the audit/crosscheck models that
inherit from them), and each watch pins its own in its own script. Changing one is a commit, which
is the point: the pin is reviewable, it is what the golden set runs against, and `model_watch.py`
can find and rewrite every occurrence of it (its `PIN_FILES`).

It used to work the other way. Each funnel workflow restated its tier's pin as
`${{ vars.OPINIONS_X_MODEL || 'claude-...' }}`, 25 restatements in six files. Two things were wrong
with that. A repository Variable set once in the GitHub UI silently outranked the repo forever, so
the id the code said it used and the id it actually used could differ with nothing erroring. And
the duplicated literal severed the inheritances the scripts had deliberately built -- the smell
model inherits the treatment-audit model, which inherits the tier-3 summarizer -- so repinning the
summarizer moved only the summarizer. All 25 were removed on 2026-08-18, and
`test_model_watch.py` now fails if a workflow ever restates a pin again.

The corresponding environment variables (`OPINIONS_MODEL`, `OPINIONS_TRIAGE_MODEL`,
`OPINIONS_SCREEN_MODEL`, `OPINIONS_SMELL_MODEL`, ...) still exist and still win when set. They are
**one-run overrides** for a local experiment or a `workflow_dispatch` you edit by hand -- not the
place a model id lives.

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
confirmation posts" if it cannot converge.

That branch-level lease is not the only line of defense. A `/veto` or `/decline` also records the
cluster id in `review/vetoed.json` / `review/declined.json` on the branch, and `review_apply`
treats those markers as **authoritative**: it refuses to publish a case a human dropped even if a
racing scan restored its staged file (the apply-side backstop). So the veto guarantee is enforced
in tested Python (`scripts/test_review.py`), not only in the workflow shell -- the shell rebuild is
an optimization, the marker is the guarantee. Still, treat the reconciliation as correctness-
critical and do not "simplify" the lease.

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

- `OPINIONS_MODEL` (default `claude-opus-5`): the Tier 3 summarizer.
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
`OPINIONS_SEEN_CAP`, `OPINIONS_FEED_MAX_BYTES`, `OPINIONS_PDF_MAX_BYTES`, `DRY_RUN`) are documented at
the top of `scripts/update.py`. The CourtListener rate limits and the reverse-sweep budget (including
`TREATMENT_PENDING_TRIES`, the per-citer re-sweep giveup) are documented at the top of
`scripts/cl_rate.py` and `scripts/treatment.py`.

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
