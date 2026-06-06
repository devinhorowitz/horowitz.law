# Georgia Appellate Watch: self-publishing pipeline

A daily GitHub Actions job checks CourtListener for new published Georgia appellate
opinions, asks Claude to filter them for an insurance-defense / civil-litigation
audience and draft a short synopsis in the house style, and opens a pull request with
the additions. You review the PR and merge to publish. Cloudflare Pages deploys on merge.

Nothing publishes without your merge. That is the gate, and it is the recommended
posture while the feed matures.

## How the pieces fit

- `opinions.json` is the single source of truth: a list of opinion entries.
- `scripts/render.py` renders `opinions.json` into the cards in `opinions.html`
  (between the `opinions:start` / `opinions:end` markers) and into `opinions.xml`.
  It changes nothing else in the page.
- `scripts/update.py` is the pipeline: it fetches, classifies, summarizes, appends to
  `opinions.json`, updates `opinions_state.json`, calls the renderer, and writes a PR
  summary to `scripts/pr_body.md`.
- `opinions_state.json` records which opinions have already been evaluated so they are
  not reprocessed.
- `.github/workflows/opinions.yml` runs `update.py` daily and opens the PR.

## One-time setup

1. Add repository secrets (Settings > Secrets and variables > Actions > New repository secret):
   - `ANTHROPIC_API_KEY` (required).
   - `COURTLISTENER_TOKEN` (optional but recommended; it raises CourtListener's rate
     limits). Create a free account at courtlistener.com and copy the token from your
     profile's API page.
2. Allow Actions to open pull requests (Settings > Actions > General > Workflow permissions):
   - Select "Read and write permissions".
   - Check "Allow GitHub Actions to create and approve pull requests".

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

The CourtListener field plumbing is written defensively but has not been exercised from
this build environment, so the first manual run is the real smoke test. If a field name
has drifted, the log will show it on the affected opinion and the run will continue.

## Reviewing and publishing

Each run opens or updates a pull request titled "Georgia Appellate Watch: new opinions".
The description lists every added opinion with its court, date, disposition, areas, and a
link to the opinion, and calls out any item flagged for review (low model confidence, a
disposition it could not state, or a reporter-style citation that should not be there).
Read the diff, fix anything off in `opinions.json` on the branch if needed, and merge.

## Editing content by hand

`opinions.json` is the source of truth. To add, correct, or remove an entry by hand,
edit `opinions.json` and then rebuild:
```
python scripts/render.py
```
Do not edit the cards between the markers in `opinions.html` directly; the renderer
overwrites them.

## Tuning

Set these as `env:` on the "Fetch, classify, summarize, render" step in the workflow, or
export them when running locally:

- `OPINIONS_MODEL` (default `claude-sonnet-4-6`): the Claude model id. Confirm the
  current id in the API docs; model names change. A smaller model lowers cost.
- `OPINIONS_COURTS` (default `ga,gactapp`): CourtListener court ids. `ga` is the Supreme
  Court of Georgia; `gactapp` is the Court of Appeals of Georgia.
- `OPINIONS_LOOKBACK` (default `21`): days to look back the very first run, before state
  exists.
- `OPINIONS_MAX` (default `25`): cap on opinions evaluated per run.
- `OPINIONS_MAXCHARS` (default `14000`): characters of opinion text sent to the model.
- Schedule: edit the `cron` line in the workflow. It is in UTC.

## Cost

Tiny. A handful of opinions a day, each a few thousand tokens to classify and summarize,
is pennies a day on the API.

## Dropping the gate later

Once you trust it, you can publish without the PR step. Replace the "Open pull request"
step with a direct commit:
```yaml
      - name: Commit and push
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add opinions.json opinions.xml opinions_state.json opinions.html
          git diff --cached --quiet || git commit -m "opinions: add new Georgia appellate decisions"
          git push
```
Keep the prototype banner and the per-card "verify against the opinion" line in place
while these are AI-drafted summaries on a public page under your name.
