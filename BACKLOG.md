# Georgia Appellate Watch: backlog and known behaviors

Optional hardening items and recorded gotchas for the opinions pipeline (`scripts/`).
None of these are required. The pipeline is healthy and the editorial PR gate is the
real backstop. This file exists so a future revision has the context that otherwise
lived only in working sessions.

## Open items

### 1. Negative controls in the golden set

`scripts/golden_set.json` contains only keepers, cases that should pass the funnel.
Nothing guards the other direction: a screen prompt that drifts more permissive would
let noise in without the golden `check` mode noticing.

- Add a few entries with `expect_relevant: false`, real opinions that should be screened
  or triaged out (criminal, immigration, a routine procedural dismissal, an ordinary
  contract dispute with no insurance nexus).
- Extend `check` mode in `scripts/golden_check.py` to assert those entries are dropped
  (screen `pass` false, or triage `relevant` false), not only that keepers survive.
- Pick the control cases by hand, then run `build` to cache their text.
- Keep the existing posture: the guard never auto-loosens a filter. Controls catch
  over-permissiveness; they do not license relaxing the screen.

### 2. Thin practice-area anchors

Negligent security and bad faith have one golden anchor each, so a regression that dropped
one of those areas on that single case would be the only signal. Add one or two more
landmark cases per thin area and `build` their text.

### 3. Cross-check and completeness run at the default temperature

`crosscheck` and `completeness_check` in `scripts/update.py` use the triage model at the
model default temperature, so a flag on a real carding PR can vary run to run. They are
advisory (they ride the PR for the editor and never drop a card), so this is tolerable.
If they ever feel noisy:

- Apply the same retry idea the summarize guard uses (re-run, treat only a persistent flag
  as real), or
- Lower their temperature, but only per model and only after confirming that model accepts
  the value. See the temperature gotcha below.

### 4. Completeness check on published cards

`completeness_check` runs only on new cards in the funnel. The existing cards predate it and
are never re-checked. `scripts/maintain.py` already re-validates a rotating slice of published
cards with `crosscheck`; add a `completeness_check` call to that slice to cover the back
catalog over time and surface its flags the same way.

### 5. Failure-alert parity across workflows

The find-or-create issue alert on failure exists on the funnel and maintenance workflows.
Several scheduled workflows fail without one. Audit `.github/workflows/` for which lack the
`if: failure()` issue step (`render-sync` is one) and add it, or factor it into a reusable
step, so a broken scheduled job is noticed.

### 6. Periodic log digest

`opinions_pipeline_log.jsonl` accumulates one record per funnel run (cards, drop reasons,
CourtListener calls, cross-check and completeness flag counts) but nothing summarizes it.
A small reader plus a weekly scheduled job could post rolling stats to the run summary or a
digest issue, turning the log into something glanceable.

## Known behaviors and decisions (not bugs)

- **Recall is unmeasured, but now reviewable.** The golden `check` mode is a regression tripwire,
  not a measured recall percentage, and the funnel’s true miss rate is still unknown. The editorial
  gate controls precision, not recall. What the screen and triage drop is now logged to
  `opinions_rejections.jsonl` (most recent 5000 records, tunable with `OPINIONS_REJECT_CAP`) and
  listed on each run’s Actions page, so misses can be caught by periodic review of what was thrown
  out. This is the accepted handle on recall for a one-person curated feed.
- **The summarizer is nondeterministic.** It runs at temperature 1 (see below), so area tags
  vary run to run. The summarize guard absorbs this with retries (`OPINIONS_GOLDEN_RETRIES`,
  default 3). If a genuinely flaky area still misses every attempt now and then, raise that
  value rather than editing the golden expectations to match a single noisy run.
- **Temperature gotcha: do not set a global temperature 0.** `claude-opus-4-8`, the summarizer,
  rejects any temperature other than 1 with an HTTP 400, which the error handler can mislabel
  as a retired-model problem. A global temperature 0 once broke every summarize call, the funnel
  included. There is intentionally no global temperature override. If you want determinism on a
  tier, set it for that model only and confirm the model accepts the value first.
- **Manual uploads are the main fragility.** Pages and scripts are uploaded by hand through the
  GitHub web UI, and an upload occasionally does not land, leaving a stale file. After any change,
  confirm it on `main`, and that the rendered pages stay in lockstep with `opinions.json`, before
  trusting it. A git push workflow would remove this whole class of error.
- **The rigor exceeds the volume, deliberately.** The feed is small, and the guard layers,
  scheduler, and golden sets are more machinery than a few dozen cards strictly need. The feed
  is public and under one name, so reliability is valued over minimalism here.