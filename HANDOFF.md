# HANDOFF.md

Updated 2026-06-12. This file replaced BACKLOG.md as the single
session-to-session reference: outstanding work, carried backlog, operating
doctrine, and external loose ends. Shipped history lives in ROADMAP.md.

To brief a new assistant session: “Fetch and read
<https://raw.githubusercontent.com/devinhorowitz/horowitz.law/main/HANDOFF.md>
then <task>.” The sandbox network allowlist already includes
raw.githubusercontent.com.

## State at handoff (verified 2026-06-12; GA Supreme official links now live on main)

37 cards including the first Supreme Court entry (Montgomery v. Caribe
Transport II, cluster 10858760). Aspen (9391147) carries treatment
“negative” dated 2026-06-12 with a human Shepard’s note; its treated_by
records Montgomery and Hodge. The PWA is fully live (manifest, sw.js,
registration, app meta on every page) and the “[ install the app ]” row is
serving on /opinions. CI is green and the tree is render-idempotent.

GA Supreme Court official links are live: a scotga card’s name links to the
court’s own opinion PDF (resolved from gasupreme.us by scripts/official_ga.py),
with CourtListener kept below as the full record. 7 of 10 scotga cards carry
official_url; Phillips, Toyo, and Scapa are pre-2017 with no PDF on the current
site. Martin’s empty docket was fixed to S16G0743 and S16G0750 in the process.
The Eleventh Circuit and SCOTUS official-link extension is the next planned
build, see the federal-courts section below.

Assistant session ritual: re-sync the working copy from a fresh codeload
tarball of main, replicate CI locally (py_compile, the 17 imports
(official_ga added this round), node –check on app.js, opinions.js,
subscribe.js, and sw.js, workflow YAML parses, check_site.py, render
idempotency via a temp commit and a double-render diff) before any delivery,
and verify against the live site with curls afterward.

## Operating doctrine (hard-won, do not relearn)

- Sources only in uploads; derived pages belong to the render-sync bot. A
  json, shell, or script change goes up branch-and-PR, that PR’s CI is
  EXPECTED to be red on render idempotency, merge it, run Actions ->
  render-sync, merge the bot PR, green.
- Upload technique: drag folders themselves plus loose files in one drag;
  per-file picking strands files. Never upload UPLOAD-NOTES or any
  instruction sheet; they are read-only bundle docs.
- Treatment doctrine: the machine may only set “caution”. “negative” and
  “superseded” are human-only on a Shepard’s read. editor_note is
  human-only everywhere; assistant drafts are ratified by upload.
- Golden set: append-mostly; anchors must be unambiguous keepers;
  fix-on-arrival applies only to never-validated entries.
- CourtListener from the sandbox: anonymous REST returns 401 and opinion
  HTML returns a 202 bot challenge. Quota-free paths are the court Atom
  feeds and storage.courtlistener.com PDFs. The funnel’s feed window is
  anchored to the last state date minus two days, so late-ingested
  clusters (SCOTUS especially) can pass unseen; backfill seed
  (OPINIONS_SEED, for example “10858760:scotus”, dry_run first) is the
  remedy.
- Outputs mount flakes on directory operations: stage everything in
  /home/claude, copy once, and use a fresh directory name on any retry.
- ** It runs at temperature 1 (see below), so area tags vary run to run. The summarize guard absorbs this with retries (`OPINIONS_GOLDEN_RETRIES`, default 3). If a genuinely flaky area still misses every attempt now and then, raise that value rather than editing the golden expectations to match a single noisy run. - **Temperature gotcha: do not set a global temperature 0.** `claude-opus-4-8`, the summarizer, rejects any temperature other than 1 with an HTTP 400, which the error handler can mislabel as a retired-model problem. A global temperature 0 once broke every summarize call, the funnel included. There is intentionally no global temperature override. If you want determinism on a tier, set it for that model only and confirm the model accepts the value first.

## Georgia courts’ own publishing: probe done, official links shipped

Probe outcome (allowlist carries gaappeals.gov, [www.gaappeals.gov](http://www.gaappeals.gov),
gasupreme.us, [www.gasupreme.us](http://www.gasupreme.us)):

- Supreme Court ([www.gasupreme.us](http://www.gasupreme.us)): clean, no WAF, HTTP 200. Year-index page
  per year at /<year>-opinions/, structured `<h3>Month</h3>`,
  `<p><strong>Date</strong></p>`, `<ul><li><a href="PDF">DOCKET. NAME</a></li>`.
  PDF URL is /wp-content/uploads/<year>/<month>/<docket-lowercased>.pdf, a
  WordPress media path stable across the site’s redesigns. Filename is the
  lowercased docket, verified across a release. pdf_text plus _pdf_ok pass on a
  sample (s26a0017.pdf, about 20,800 chars). Apex gasupreme.us 503s, use [www](http://www).
  Year pages exist back to 2017; /2015- and /2016-opinions/ 404 and the older
  PDFs are not on the current uploads tree, so pre-2017 cards get no official
  link.
- Court of Appeals (gaappeals.gov): blocked. The live docket data endpoint is
  /wp-content/themes/benjamin/docket/results_all.php (the recorded path’s extra
  docketdate/ segment was stale), behind a persistent AWS WAF JS challenge
  (window.gokuProps), HTTP 405 on GET and POST, all date formats and retries.
  The human /docket-search/ form page loads, but the data endpoint a headless
  funnel needs does not. Not usable, and the date-format question (6-12 vs 12-6)
  stays undetermined as a result. CourtListener remains the source for the
  Court of Appeals.

Shipped this session: official_url enrichment, Supreme Court of Georgia only. A
scotga card’s case name links to the court’s own opinion PDF, and the
CourtListener link below is relabeled “Full record on CourtListener” (CL carries
citations, treatment, and docket data the court’s own listing omits, so it
stays). A card without official_url is unchanged: plain name, CourtListener as
the read link. Identity still keys on the CourtListener cluster_id.

- Mechanism: scripts/official_ga.py. official_url_for(card) fetches the
  /<year>-opinions/ page for the card’s decision year and matches a docket to a
  PDF basename, fail-open (any miss or error returns None). Reused two ways: a
  backfill CLI (`python scripts/official_ga.py [--apply]`) that filled the
  existing cards, and a forward hook in update.py that populates new scotga cards
  at assembly time (also fail-open; if the year page lags CL, a later backfill
  fills it).
- Coverage now: 7 of 10 scotga cards carry official_url (SMG, Walmart, Miller,
  Adventure Motorsports, Quynn, Cooper Tire, Martin). Phillips, Toyo, and Scapa
  are pre-2017 with no PDF on the current site, so they keep CourtListener-only
  rendering.
- Ground-truth join re-anchored from the Waddle Trucking card (ctapp, behind the
  WAF) to the current civil Supreme Court opinion S26A0807, Sockwell Corners v.
  Newton County, which official_ga resolves cleanly.
- Data fix in passing: the Martin v. Six Flags card (cluster 5749712) had an
  empty docket; set to S16G0743 and S16G0750 (the consolidated cross-appeals,
  confirmed against the court’s 2017 listing), which also let the docket-based
  resolver find its PDF. Worth a sanity-check on review.

Still locked, still pending: the cluster_id stays the identity spine, so the
Georgia sites supplement rather than replace. The early-alert poller (notice
court-posted opinions absent from the CL feed and route them to the seed list
when the cluster appears, closing the late-ingestion hole, backlog item 6) is
viable for the Supreme Court only given the Court of Appeals WAF, and is not yet
built.

## Federal courts official links (Eleventh Circuit and SCOTUS), and their own feeds (next)

Found 2026-06-12. Both federal courts can get the linked-name treatment the GA
Supreme cards now have, sourced from CourtListener, no new host needed.

CourtListener already carries each court’s own PDF URL in opinion.download_url.
Verified on all four federal cards we hold:

- ca11 Aspen (cluster 9391147, opinion 9386623):
  <http://media.ca11.uscourts.gov/opinions/pub/files/202210740.pdf>
- ca11 Ya Mon (cluster 10872980, opinion 11340448):
  <http://media.ca11.uscourts.gov/opinions/unpub/files/202510140.pdf>
- scotus Montgomery (cluster 10858760, opinion 11326162):
  <https://www.supremecourt.gov/opinions/25pdf/24-1238_1b7d.pdf>
- scotus Keathley (cluster 10873663, opinion 11341134):
  <https://www.supremecourt.gov/opinions/25pdf/25-6_d1o2.pdf>

All are the courts’ official files. ca11 URLs arrive as http (normalize to
https, fall back to the http CL gives if a file 404s on https) and carry pub vs
unpub in the path, the filename being “20” + docket without the dash (22-10740
-> 202210740). The SCOTUS slip-opinion URLs are already https and end in
<docket>_<hash>.pdf under /opinions/25pdf/ (the 25 is the term, OT2025), where
the 4-char hash is not derivable from the docket, so the URL must be read from
CL, not reconstructed. Reaching either host is not required for this: the URL is
stored and rendered for the reader’s browser, not fetched server-side.

The render already linkifies any card carrying official_url, court-agnostic
(card_html in render.py), so this is a data plus forward-population change, no
render change:

- One resolver that reads a cluster’s sub_opinion download_url from
  CourtListener and returns it https-normalized, used for any federal court.
  Either a sibling to scripts/official_ga.py or, cleaner, generalize official_ga
  into a per-court dispatch: scotga keeps the gasupreme.us scrape, ca11 and
  scotus read CL’s download_url.
- Backfill the 7 federal cards: ca11 9391147, 10872097, 10872980, 10873765,
  10873661; scotus 10858760, 10873663. Forward-populate ca11 and scotus in
  update.py’s assembly with the same fail-open shape as the scotga hook. Add the
  module to the CI import list (now 17).

Each court also publishes its own opinions, the source for the early-alert
poller (backlog item 6 below), which catches opinions before CL ingests:

- Eleventh Circuit published-opinions RSS:
  <https://media.ca11.uscourts.gov/opinions/rss/pubopnsfeed.php>
- SCOTUS slip opinions: <https://www.supremecourt.gov/opinions/slipopinion/25>
  (the trailing 25 is the term). Note this differs from the orders page
  <https://www.supremecourt.gov/orders/ordersofthecourt/25>, which lists cert
  decisions and procedural orders, not argued-case opinions; our scotus cards
  are slip opinions. SCOTUS is the strongest early-alert case: the hole this
  closes is exactly the Montgomery slip-opinion late-ingestion noted in item 6.

Both feed hosts (media.ca11.uscourts.gov and [www.supremecourt.gov](http://www.supremecourt.gov)) were blocked
in this session (“Host not in allowlist,” though both are nominally listed), so
neither feed could be read directly. A fresh session with the hosts reachable
can probe each: item or list structure, the PDF URL pattern, docket and
case-name extraction, and a sample through update.pdf_text plus the _pdf_ok
gate. The poller needs them reachable both in a build session and in the Actions
runner; the official-link build does not.

Open call for the new session: ship the federal official-link extension now via
CourtListener (no host needed, all 7 cards), or get the hosts reachable first
and do the official links and the feed-based pollers together.

1. Professional-image set, one render bundle on request: a
   professional-responsibility footer line (not legal advice, no
   attorney-client relationship) on /opinions, /archive, and the
   permalinks (the audit confirmed index and subscribe have it and the
   legal-content pages do not); a curator byline on the Watch intro
   linking the resume; a “[ first impressions ]” quick-filter chip
   (?q=first impression).
1. Annotated landmarks, the editor’s pen: Phillips, Quynn, Martin, and
   Toyo are carded and noteless; Montgomery is the flagship candidate.
1. Phase 5, Florida in full (design locked in earlier sessions): composite
   RESEND_TOPIC_MAP keys (“fl”, “fl:premises”), HMAC v2 confirm token
   email.ts.a:<csv>.j:<csv> with a 7-day grace window, per-state digests
   by membership, and the overlay-to-full mode flip springs the dormant
   subscribe state checkboxes.
1. Web Push for the installed app: VAPID keypair, a subscribe endpoint
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
(The apparent Hodge twin that day was a false alarm: “Willy Hodge” was the
treated_by styling on Aspen, not a second card.)

### 6. Late-ingestion recall

CourtListener can publish a cluster weeks after its dateFiled, SCOTUS slip
opinions especially; the since-window then skips them unseen (Montgomery
left zero trace in state or rejections). Remedy exists (backfill seed);
the durable fix is the early-alert poller (the Georgia Supreme site, the
Eleventh Circuit RSS feed, and the SCOTUS slip-opinions page, see the
federal-courts section above), plus detection for feed items whose dateFiled
sits well behind the window.

### 7. backfill.py predates Phase 4

Its summarizer prompt and assembly are frozen pre-Phase 4, so seeded cards
arrive without first_impression, tort_reform, and law_applied (Montgomery
did). Sync them with update.py, or refactor backfill to import update’s
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
  panel); Rules -> Snippets; the zone’s Workers Routes; legacy Page
  Rules. Paste for Cloudflare’s AI:
  
  “For the zone horowitz.law: list every entry in this zone’s
  http_response_headers_transform ruleset, every Snippet, every Workers
  route bound to this zone, every legacy Page Rule, and every Zero Trust
  Access application whose domain matches horowitz.law (including its
  CORS settings). For each, show the match expression and the actions or
  headers it sets. I am hunting whatever adds Access-Control-Allow-Origin:
  - to all responses and appends a duplicate Cache-Control on font files.
    Enumerate first, change nothing.”
- Ko-fi: one-line colophon sponsor swap when the URL exists.
- [devin@horowitz.law](mailto:devin@horowitz.law) via Cloudflare Email Routing, still an open question.
- LinkedIn cadence from the Monday digest: a habit, not code; permalinks
  already unfurl with proper cards.