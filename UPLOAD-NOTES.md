# One upload, three jobs: green CI + the day's editorial + the iPhone app
2026-06-11 · supersedes the earlier "iphone-app" bundle entirely — discard it.

## 1) Why main is red, and the root-cause fix
The pipeline's PR #21 carded FIVE decisions (the PR body lists them; check it
for a golden-set nomination block too), but .github/workflows/opinions.yml
still had the original seven add-paths — everything Phase 2+ (permalinks,
stats, changes, digests) was silently left out of every content PR. This
bundle carries the regenerated artifacts AND the workflow fix, so future
content PRs arrive complete. CI goes green on this PR's merge.

## 2) Editorial, for your ratification (merging = adopting)
- **The five, reviewed.** Hodge (the Montgomery remand) you know. Two more
  are keepers I've now read closely: **Waddle Trucking v. Johnson** (Ga. App.,
  published) — an untimely federal answer survives remand and defeats state
  default absent striking or a Rule 55 default; removal-mechanics gold for the
  procedural-offense file. **Keathley v. Buddy Ayers Construction** (SCOTUS,
  published) — judicial estoppel for a PI claim omitted from Chapter 13
  schedules now turns on totality-of-the-circumstances, not knowledge-plus-
  motive; that retires a defense shortcut and reshapes the
  undisclosed-claim playbook. Both synopses read accurate; no edits from me.
  (Keathley also proves the SCOTUS feed cards fine when the cluster is fresh —
  Montgomery's miss was purely the late-ingestion window, BACKLOG #8.)
- **Duplicate removed.** 10873764 and 10873765 were one consolidated CA11
  appeal (identical docket lists 24-11398 + 25-11185); CourtListener issued
  twin clusters and the funnel carded both. The consolidated-titled card
  (10873765) stays; the twin is removed. BACKLOG #7 specs the guard.
- **Aspen's editor's note** (drafted in your voice, edit freely in
  opinions.json before upload): SCOTUS answered — Montgomery abrogated the
  FAAAA-preemption holding; the Hodge remand is already applying it;
  treatment upgrade pending Shepard's.
- **Your Shepard's step, after upload:** confirm Montgomery's effect, then
  set Aspen's "treatment" from "caution" to "negative" in opinions.json
  (human-only by doctrine; the machine already recorded treated_by entries
  for Montgomery and Hodge, kind: abrogated). The ledger will log it.

## 3) Card Montgomery itself (the abrogator belongs in the archive)
Actions → **backfill** → Run workflow →
  dry_run: ✓ (leave checked)   ·   seed: `10858760:scotus`
Read the run log's preview card; if it looks right, run again with dry_run
unchecked → it opens a PR → review → merge. First SCOTUS card on the site;
it binds every jurisdiction in the dropdown, and Aspen's treated_by already
points at its cluster id. (Why the funnel missed it: CL published the
cluster weeks after its May 14 dateFiled, behind the since-window — BACKLOG
#8 records the class.)

## 4) The iPhone app rides along, unchanged in design
manifest + sw.js + app meta on every page (incl. all permalinks). After
merge: hard-refresh once on desktop, then iPhone Safari → /opinions →
Share → **Add to Home Screen** → "GA Watch". Acceptance test: Airplane Mode,
reopen, the Watch still reads. Zero CSP changes; the script hash never moved.

## Upload set — branch-and-PR as usual
Everything in this folder at the same paths. The o/ folder ships complete
(all heads gained the app tags; two NEW permalinks: Hodge 10873661 and
Bloodworth 10873765).

## Closeout while you're there
- Close Issues #18 and #19 if still open.
- Delete stale branches: bot/opinions-update, bot/opinions-treatment,
  tagfill, devinhorowitz-patch-1.
- Still pending from the tidy list: delete the six bundle sheets at repo
  root (COMMIT_NOTES.md, INTEGRATION.md, NOTES.md, PHASE1/2/3_NOTES.md).
  This bundle's BACKLOG.md supersedes the tidy bundle's copy.
