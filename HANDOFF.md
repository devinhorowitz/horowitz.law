# HANDOFF.md

Updated 2026-06-12. This file replaced BACKLOG.md as the single
session-to-session reference: outstanding work, carried backlog, operating
doctrine, and external loose ends. Shipped history lives in ROADMAP.md.

To brief a new assistant session: "Fetch and read
https://raw.githubusercontent.com/devinhorowitz/horowitz.law/main/HANDOFF.md
then <task>." The sandbox network allowlist already includes
raw.githubusercontent.com.

## State at handoff (verified 2026-06-12, main 20db712)

37 cards including the first Supreme Court entry (Montgomery v. Caribe
Transport II, cluster 10858760). Aspen (9391147) carries treatment
"negative" dated 2026-06-12 with a human Shepard's note; its treated_by
records Montgomery and Hodge. The PWA is fully live (manifest, sw.js,
registration, app meta on every page) and the "[ install the app ]" row is
serving on /opinions. CI is green and the tree is render-idempotent.

Assistant session ritual: re-sync the working copy from a fresh codeload
tarball of main, replicate CI locally (py_compile, the 16 imports, node
--check on app.js, opinions.js, subscribe.js, and sw.js, workflow YAML
parses, check_site.py, render idempotency via a temp commit and a
double-render diff) before any delivery, and verify against the live site
with curls afterward.

## Operating doctrine (hard-won, do not relearn)

- Sources only in uploads; derived pages belong to the render-sync bot. A
  json, shell, or script change goes up branch-and-PR, that PR's CI is
  EXPECTED to be red on render idempotency, merge it, run Actions ->
  render-sync, merge the bot PR, green.
- Upload technique: drag folders themselves plus loose files in one drag;
  per-file picking strands files. Never upload UPLOAD-NOTES or any
  instruction sheet; they are read-only bundle docs.
- Treatment doctrine: the machine may only set "caution". "negative" and
  "superseded" are human-only on a Shepard's read. editor_note is
  human-only everywhere; assistant drafts are ratified by upload.
- Golden set: append-mostly; anchors must be unambiguous keepers;
  fix-on-arrival applies only to never-validated entries.
- CourtListener from the sandbox: anonymous REST returns 401 and opinion
  HTML returns a 202 bot challenge. Quota-free paths are the court Atom
  feeds and storage.courtlistener.com PDFs. The funnel's feed window is
  anchored to the last state date minus two days, so late-ingested
  clusters (SCOTUS especially) can pass unseen; backfill seed
  (OPINIONS_SEED, for example "10858760:scotus", dry_run first) is the
  remedy.
- Outputs mount flakes on directory operations: stage everything in
  /home/claude, copy once, and use a fresh directory name on any retry.
- ** It runs at temperature 1 (see below), so area tags vary run to run. The summarize guard absorbs this with retries (`OPINIONS_GOLDEN_RETRIES`, default 3). If a genuinely flaky area still misses every attempt now and then, raise that value rather than editing the golden expectations to match a single noisy run. - **Temperature gotcha: do not set a global temperature 0.** `claude-opus-4-8`, the summarizer, rejects any temperature other than 1 with an HTTP 400, which the error handler can mislabel as a retired-model problem. A global temperature 0 once broke every summarize call, the funnel included. There is intentionally no global temperature override. If you want determinism on a tier, set it for that model only and confirm the model accepts the value first.

## Next session, first action: probe Georgia

The goal is direct use of the Georgia courts' own opinion publishing.
Requires the network allowlist to carry all four exact hostnames:
gaappeals.gov, www.gaappeals.gov, gasupreme.us, www.gasupreme.us. Egress
rules bind at container boot, so they take effect in a fresh session.

Probe spec:
1. gaappeals.gov docket endpoint
   (/wp-content/themes/benjamin/docket/docketdate/results_all.php with
   OPstartDate and OPendDate): disambiguate the date format (try 6-12-2026
   against 12-6-2026 on a date with known releases), extract the row
   structure (case name, docket like A26A0145, PDF link), and assess PDF
   URL stability. The theme path is a brittleness flag; it dies on their
   next redesign.
2. gasupreme.us/2026-opinions/: listing structure, PDF naming (s26a####
   style), date grouping.
3. Pull one sample PDF from each and run scripts/update.pdf_text plus the
   _pdf_ok quality gate.
4. Ground-truth join: match the Waddle Trucking card's docket (confirm it
   from opinions.json, cluster 10873711) against the court's own listing
   for its release date.

Locked design, whatever the probe finds at the edges: CourtListener's
cluster_id remains the identity spine (permalinks, treatment, golden,
dedup), so the Georgia sites supplement rather than replace. Two
mechanisms: (a) an official_url enrichment field on Georgia cards, official
link primary and CourtListener as backup in the rendered card, and (b) an
early-alert poller that notices court-posted opinions absent from the CL
feed and routes them to the seed list when the cluster appears, which
closes the late-ingestion hole (backlog item 6 below).

## Open projects

1. Professional-image set, one render bundle on request: a
   professional-responsibility footer line (not legal advice, no
   attorney-client relationship) on /opinions, /archive, and the
   permalinks (the audit confirmed index and subscribe have it and the
   legal-content pages do not); a curator byline on the Watch intro
   linking the resume; a "[ first impressions ]" quick-filter chip
   (?q=first impression).
2. Annotated landmarks, the editor's pen: Phillips, Quynn, Martin, and
   Toyo are carded and noteless; Montgomery is the flagship candidate.
3. Phase 5, Florida in full (design locked in earlier sessions): composite
   RESEND_TOPIC_MAP keys ("fl", "fl:premises"), HMAC v2 confirm token
   email.ts.a:<csv>.j:<csv> with a 7-day grace window, per-state digests
   by membership, and the overlay-to-full mode flip springs the dormant
   subscribe state checkboxes.
4. Web Push for the installed app: VAPID keypair, a subscribe endpoint
   (Pages Function plus KV), and a sender step on the alert workflow. iOS
   supports push for installed PWAs.

## Backlog (carried from BACKLOG.md and renumbered)

### 1. Cross-check and completeness run at the default temperature

`crosscheck` and `completeness_check` in `scripts/update.py` use the triage model at the
model default temperature, so a flag on a real carding PR can vary run to run. They are
advisory (they ride the PR for the editor and never drop a card), so this is tolerable.
If they ever feel noisy:

- Apply the same retry idea the summarize guard uses (re-run, treat only a persistent flag
  as real), or
- Lower their temperature, but only per model and only after confirming that model accepts
  the value. See the temperature gotcha below.

### 2. Completeness check on published cards

`completeness_check` runs only on new cards in the funnel. The existing cards predate it and
are never re-checked. `scripts/maintain.py` already re-validates a rotating slice of published
cards with `crosscheck`; add a `completeness_check` call to that slice to cover the back
catalog over time and surface its flags the same way.

### 3. Failure-alert parity across workflows

The find-or-create issue alert on failure exists on the funnel and maintenance workflows.
Several scheduled workflows fail without one. Audit `.github/workflows/` for which lack the
`if: failure()` issue step (`render-sync` is one) and add it, or factor it into a reusable
step, so a broken scheduled job is noticed.

### 4. Periodic log digest

`opinions_pipeline_log.jsonl` accumulates one record per funnel run (cards, drop reasons,
CourtListener calls, cross-check and completeness flag counts) but nothing summarizes it.
A small reader plus a weekly scheduled job could post rolling stats to the run summary or a
digest issue, turning the log into something glanceable.

### 5. Duplicate-cluster guard in the funnel

CourtListener issued twin clusters for one consolidated Eleventh Circuit
appeal (10873764 and 10873765, identical docket lists) and the funnel
carded both; the existing dedup keys on cluster id, the one thing that
differed. Guard at the queue or update stage: same court, same dateFiled,
identical docket list or identical party-token set means one case; keep
the consolidated-titled cluster. The 2026-06-11 pair is the test fixture.
(The apparent Hodge twin that day was a false alarm: "Willy Hodge" was the
treated_by styling on Aspen, not a second card.)

### 6. Late-ingestion recall

CourtListener can publish a cluster weeks after its dateFiled, SCOTUS slip
opinions especially; the since-window then skips them unseen (Montgomery
left zero trace in state or rejections). Remedy exists (backfill seed);
the durable fix is the Georgia-direct early-alert poller above, plus
detection for feed items whose dateFiled sits well behind the window.

### 7. backfill.py predates Phase 4

Its summarizer prompt and assembly are frozen pre-Phase 4, so seeded cards
arrive without first_impression, tort_reform, and law_applied (Montgomery
did). Sync them with update.py, or refactor backfill to import update's
summarize and assembly directly.

### 8. treated_by hyperlinks

The permalink treatment block renders treated_by names as plain text. When
the citing cluster is itself carded (Aspen to /o/10858760), render the
name as a link. About five lines in render.py.

## Housekeeping (one sitting)

- Root litter delete, eight files in the same PR that adds this one:
  COMMIT_NOTES.md, INTEGRATION.md, NOTES.md, PHASE1_NOTES.md,
  PHASE2_NOTES.md, PHASE3_NOTES.md, UPLOAD-NOTES.md, and BACKLOG.md.
- Branch sweep: bot/opinions-backfill, bot/opinions-render,
  bot/opinions-treatment, bot/opinions-update, tagfill, and
  devinhorowitz-patch-1 through -4, plus any newer patch branches.
- Issues #18 and #19: close if still open.

## External, non-repo

- Cloudflare: an Access-Control-Allow-Origin: * header is injected
  zone-wide and the font Cache-Control value is duplicated; the origin
  (_headers and the middleware) is exonerated and the pages.dev
  differential localized it to the horowitz.law zone layer. Hunt order:
  Rules -> Overview; Zero Trust -> Access -> Applications (the CORS
  panel); Rules -> Snippets; the zone's Workers Routes; legacy Page
  Rules. Paste for Cloudflare's AI:

  "For the zone horowitz.law: list every entry in this zone's
  http_response_headers_transform ruleset, every Snippet, every Workers
  route bound to this zone, every legacy Page Rule, and every Zero Trust
  Access application whose domain matches horowitz.law (including its
  CORS settings). For each, show the match expression and the actions or
  headers it sets. I am hunting whatever adds Access-Control-Allow-Origin:
  * to all responses and appends a duplicate Cache-Control on font files.
  Enumerate first, change nothing."

- Ko-fi: one-line colophon sponsor swap when the URL exists.
- devin@horowitz.law via Cloudflare Email Routing, still an open question.
- LinkedIn cadence from the Monday digest: a habit, not code; permalinks
  already unfurl with proper cards.
