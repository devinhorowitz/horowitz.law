# Georgia Appellate Watch: self-publishing pipeline

A daily GitHub Actions job checks CourtListener for new published opinions from four
courts (the Supreme Court of Georgia, the Court of Appeals of Georgia, the U.S. Court of
Appeals for the Eleventh Circuit, and the U.S. Supreme Court), filters them for an
insurance-defense and civil-litigation audience, drafts a short synopsis in the house
style, and opens a pull request with the additions. You review the PR and merge to
publish. Cloudflare Pages deploys on merge.

Nothing publishes without your merge. That is the gate, and it is the recommended
posture while the feed matures.

## The funnel

Candidates pass through three model tiers, cheapest first, so the expensive model only
ever touches confirmed keepers:

- Tier 1, screen (Haiku): reads the case name and opening excerpt only and drops the
  obvious non-matches.
- Tier 2, triage (Sonnet): reads the full opinion and decides, against a narrow bar,
  whether it belongs in the feed.
- Tier 3, summarize (Opus): reads the full opinion plus the triage note and writes the
  published synopsis, or still declines.

Each drafted card is then checked twice more: a fidelity crosscheck (does the card match
the opinion) and a completeness check (does the card omit a material holding in a covered
area). Both default to the Sonnet triage model, so the summarizer is not grading its own
work. Screen and triage drops are appended to `opinions_rejections.jsonl` for periodic
recall review.

## How the pieces fit

- `opinions.json` is the single source of truth: a list of opinion entries.
- `scripts/update.py` is the pipeline: it fetches candidates, runs the three-tier funnel
  and the per-card checks, appends keepers to `opinions.json`, updates
  `opinions_state.json`, calls the renderer, logs the run to `opinions_pipeline_log.jsonl`
  and drops to `opinions_rejections.jsonl`, and writes a PR summary to `scripts/pr_body.md`.
- `scripts/render.py` renders `opinions.json` into the cards in `opinions.html` (between
  the `opinions:start` / `opinions:end` markers), the `archive.html` page, the per-opinion
  permalink pages under `/o/`, and the `opinions.xml` feed. It changes nothing else in a page.
- `opinions_state.json` records which opinions have already been evaluated so they are not
  reprocessed. Its seen list is capped (see `OPINIONS_SEEN_CAP`) to bound growth.
- `.github/workflows/opinions.yml` runs `update.py` on a daily schedule and opens the PR.

## One-time setup

1. Add repository secrets (Settings > Secrets and variables > Actions > New repository secret):
   - `ANTHROPIC_API_KEY` (required).
   - `COURTLISTENER_TOKEN` (optional but recommended; it raises CourtListener's rate
     limits). Create a free account at courtlistener.com and copy the token from your
     profile's API page.
2. Allow Actions to open pull requests (Settings > Actions > General > Workflow permissions):
   - Select "Read and write permissions".
   - Check "Allow GitHub Actions to create and approve pull requests".

The model ids live in repository Variables (Settings > Secrets and variables > Actions >
Variables) and fall back to built-in defaults when unset, so a model id can be changed
without editing code.

## Test it before trusting the schedule

- From GitHub: open the Actions tab, select "Georgia Appellate Watch update", and use
  "Run workflow" (the `workflow_dispatch` trigger). Watch the log, then review the PR it
  opens.
- Locally, without writing anything:
  ```
  ANTHROPIC_API_KEY=sk-... COURTLISTENER_TOKEN=... DRY_RUN=1 python scripts/update.py
  ```
  It prints the candidates, the keep/drop decision for each, and the PR summary, but
  touches no files.

## Reviewing and publishing

Each run opens or updates a pull request titled "Georgia Appellate Watch: new opinions".
The description lists every added opinion with its court, date, disposition, areas, and a
link to the opinion, and calls out any item flagged for review (low model confidence, a
disposition it could not state, or a citation that should not be there). Read the diff,
fix anything off in `opinions.json` on the branch if needed, and merge.

## Editing content by hand

`opinions.json` is the source of truth. To add, correct, or remove an entry by hand, edit
`opinions.json` and then rebuild:
```
python scripts/render.py
```
Do not edit the cards between the markers in `opinions.html`, the archive, or the permalink
pages directly; the renderer overwrites them.

## Tuning

Set these as repository Variables, as `env:` on the "Fetch, screen, triage, summarize,
render" step in the workflow, or export them when running locally:

- `OPINIONS_MODEL` (default `claude-opus-4-8`): the Tier 3 summarizer.
- `OPINIONS_TRIAGE_MODEL` (default `claude-sonnet-4-6`): the Tier 2 full-read gate. `""` disables it.
- `OPINIONS_SCREEN_MODEL` (default `claude-haiku-4-5-20251001`): the Tier 1 excerpt screen. `""` disables it.
- `OPINIONS_CROSSCHECK_MODEL`, `OPINIONS_COMPLETENESS_MODEL` (default: the triage model): the
  per-card fidelity and completeness checks. `""` disables either. Confirm model ids in the
  API docs; model names change.
- `OPINIONS_COURTS` (default `ga,gactapp,ca11,scotus`): CourtListener court ids. `ga` is the
  Supreme Court of Georgia, `gactapp` the Court of Appeals of Georgia, `ca11` the U.S. Court
  of Appeals for the Eleventh Circuit, and `scotus` the U.S. Supreme Court.
- `OPINIONS_LOOKBACK` (default `21`): days to look back on the first run, before state exists.
- `OPINIONS_MAX` (code default `25`; the daily workflow raises it to `80` for heavy filing
  days): cap on opinions evaluated per run.
- `OPINIONS_MAXCHARS` (default `60000`): characters of opinion text sent to triage and the
  summarizer.
- `OPINIONS_MAX_TOKENS` (default `4096`): summarizer output token cap.
- `OPINIONS_DEBUG=1`: log every model call and full API error bodies.
- Schedule: edit the `cron` line in the workflow. It is in UTC.

A handful of other guards (`OPINIONS_BUDGET_SEC`, `OPINIONS_BREAKER`,
`OPINIONS_SEARCH_BUDGET_SEC`, `OPINIONS_SEEN_CAP`, `DRY_RUN`) are documented at the top of
`scripts/update.py`.

## Cost

Modest, and the funnel keeps it that way: Haiku screens everything cheaply, Sonnet reads
only the survivors, and Opus only ever drafts confirmed keepers, so a typical day is a few
opinions through the full stack, pennies on the API.

## Dropping the gate later

Once you trust it, you can publish without the PR step. Replace the "Open pull request"
step with a direct commit:
```yaml
      - name: Commit and push
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add opinions.json opinions.xml opinions_state.json opinions.html opinions_rejections.jsonl opinions_pipeline_log.jsonl
          git diff --cached --quiet || git commit -m "opinions: add new appellate decisions"
          git push
```
Keep the prototype banner and the per-card "verify against the opinion" line in place while
these are AI-drafted summaries on a public page under your name.
