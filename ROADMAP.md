# Roadmap: Georgia Appellate Watch

Most of the original plan has shipped. This rewrite records what is done and sequences what
is left, by the one principle that has always ordered this work: **marginal API cost.**

## The organizing principle

Almost everything worth doing here is a render-time transformation of `opinions.json`,
client-side JavaScript, or a static page: zero additional CourtListener calls, zero
additional model tokens, $0 ongoing. The only structurally expensive move is promoting a
second jurisdiction to full curated coverage, which is why it goes last. The instinct that
Florida threatens the CourtListener budget and the model bill is correct, and the numbers
below say so.

## The budget picture, plainly

**CourtListener.** The free tier is 5/min, 50/hr, 125/day, `cl_rate` paces all of it, and
opinion *text* already rides the storage PDFs, which cost no REST quota. Georgia is two state
courts. A full Florida is a supreme court plus **six** District Courts of Appeal, a volume
multiple, not an increment. Levers when the time comes: a Free Law Project membership (raises
the limits *and* funds the data source, the one donation that literally buys capacity),
`OPINIONS_MAX`, a court-subset ramp, and schedule offsets so two states never contend for the
same windows. The Florida and Alabama supplementary overlays already live within this budget
because "also pulled" rides the same per-run cap; a full upgrade is the part that does not.

**Claude.** The four-tier cascade is the cost control: Haiku absorbs the firehose, Sonnet
reads only survivors, Opus writes only keepers. Token growth tracks candidate volume.
Everything below except the full-jurisdiction upgrade costs $0 ongoing.

## Engineering invariants for every change

Any new page must carry the byte-identical pre-paint inline `<script>` (the CSP pin) and be
added to `check_site.py`'s `PAGES` list so the guard covers it. Any new generated artifact
must be deterministic from its source (`opinions.json`, or `siteconfig.PAGES` for the page
list) so the CI render-idempotency step stays green, and its path must be added to that
step's diff list and to the render-sync and content-PR
add-paths so it actually commits. Asset `?v=` tokens self-stamp via `render.py`.

-----

## Shipped

The bulk of the original Phases 0 through 4, plus everything since:

- **Reader and truth layer.** Colophon truth-pass and the "under the hood" section; reader
  tools on /opinions and /archive (filter state in the shareable URL, copy-citation buttons,
  a print stylesheet).
- **Surfaces from existing data.** The corrections ledger at /changes plus the `changes.xml`
  feed; per-opinion permalinks under /o/ with their own OG and Article JSON-LD; the /stats
  coverage page.
- **Official court links.** A card's case name links to the issuing court's own opinion PDF,
  with CourtListener kept below as the full record: the Supreme Court of Georgia resolved from
  its own site by `official_ga.py`, the Eleventh Circuit and SCOTUS from the `download_url`
  CourtListener already carries. The Court of Appeals stays CourtListener-only behind its WAF.
- **Distribution.** Per-area subscription topics on /subscribe; the /digests archive;
  instant-alert broadcast for a landmark merge.
- **Taxonomy and trust.** The tort-reform tag and first-impression badge; the `editor_note`
  human-analysis layer; golden-set hardening, self-nomination, and the `law_applied` Erie
  refinement for the federal overlay.
- **The phone app.** Installable PWA: `manifest.webmanifest`, `sw.js`, and the app meta on
  every page, with offline reading, network-first pages, and cache-first hashed assets.
- **Treatment subsystem.** Forward escalation in the funnel plus the weekend reverse sweep,
  both recording through `treatment_core.py`; the machine only ever raises a card to caution.
- **Florida and Alabama, supplementary.** The registry's second and third jurisdictions ride
  the feed as "also pulled," same areas, lighter touch, the site still named for Georgia.
  Not yet curated-full and not yet per-state-subscribed (see the last section).
- **The authority watch (alert-out).** `skill_authorities.py` extracts the statutes and
  controlling cases the practice's drafting skills rely on into `skill-authorities.json`;
  `skill_alert.py` puts the case authorities on the triage watch-list; a confirmed adverse
  treatment is recorded in `skill_alert_state.json` and routed back to the relying skills.
- **Supply-chain hardening.** Every networked workflow runs under Harden-Runner block mode,
  declaring the hosts it may reach; Dependabot behind it.
- **Drip-in generation.** `render.py` publishes `/areas/<area>.json` and an index, a
  deterministic per-area extract of `opinions.json`. The source half of the integration
  below.
- **Single-source page registry.** `siteconfig.PAGES` is the one list of top-level pages;
  `render.py` builds both the `/404` link list and the sitemap's static URLs from it, so
  adding a page is a single tuple there and neither generated file is hand-edited.

-----

## Next, by marginal cost

### Drip-in consumption ($0, one decision)

Generation shipped; the slices publish at `horowitz.law/areas/<area>.json`. The remaining
half is wiring a drafting skill to read its area slice at draft time, so recent and flagged
law in the area surfaces during drafting rather than at Shepardizing. It forks on one fact:
do the qpwb skills have web access at draft time? If yes, the skill fetches its slice live and
there is no sync to build. If no, the slices sync into the skill tree and the skill reads the
local copy. The slices are designed to serve both, and a public per-area reader feed (below)
shares the same selection, so this is purely a consumption-wiring decision.

### A "cited by" view ($0, data already fetched)

The reverse sweep already walks every card's citation graph and tracks seen citers in
`treatment_state.json`, so the data is paid for. Surface "cited by N later decisions, M
flagged" per card, or a small graph. The pipeline's most distinctive work, made visible, at
no new call. The strongest novel render-time addition.

### Public per-area feeds ($0, render-time)

An `/areas/<area>.xml` RSS alongside the JSON slices, for a reader who wants only premises or
only trucking. It reuses the per-area selection already built for drip-in; the only new work
is an XML rendering. This is the colophon's standing "per-area feeds still to come," delivered.

### Statute-currency watch (pennies, extends alert-out)

The manifest already lists the statutes the skills rely on, but alert-out v1 is case-only. A
one-line triage-prompt addition can flag when a new opinion construes or invalidates a
relied-on statute, no new fetch. Closes the statute gap in the authority watch.

### Authority-watch surface ($0)

Beyond the Actions run summary: a weekly digest section or a small private page reading
`skill_alert_state.json`, so a relied-on authority going bad reaches you outside the log.

### opinions.json as a documented public API ($0)

It already is a structured public feed. A documented schema, and optionally a thin
`/api/opinions` endpoint, turns "the whole machine is public" into a usable interface.

### Web Push for the installed PWA (small infra, ~$0)

iOS supports push for home-screen PWAs: a VAPID keypair, a subscription endpoint (Pages
Function plus KV), and a sender step on the alert workflow. Turns the instant-alert email tier
into real push. The email tier covers notifications until then.

-----

## The expensive one, last: Florida or Alabama, full

The supplementary overlays are live. Promoting either to a curated-full jurisdiction with
per-state subscriptions is the one structurally expensive move, the CourtListener volume named
above. The scaffolding is already built: the registry takes one entry per state, the subscribe
form ships a registry-gated "which states" checkbox group that appears once a second full state
exists, `subscribe.js` already posts `juris`, and federal bindingness is derived, never stored
(`jurisdictions.court_binds()` maps ca11 to ga/fl/al and scotus to all, and render stamps
`data-jurisdiction` from it). The remaining work, in order:

1. **State golden set first**, known keepers and controls from that state's practice, cached
   once.
1. **Prompt parameterization**, the three Georgia-written prompts made jurisdiction-aware,
   checked against that set before anything runs on a schedule.
1. **Court-subset ramp**, one or two courts first, expanding as the budget proves out. Decide
   the Free Law Project membership here, not before; it is the capacity lever and the right
   donation.
1. **Schedule offsets** so the states never share a rate window.
1. **Wire the locked subscription design.** One Topic per state ("fl") and per state-area
   ("fl:premises") in a composite-key `RESEND_TOPIC_MAP` that supersedes `RESEND_AREA_TOPICS`,
   with bare area keys honored as Georgia-implicit during migration; a confirm-link HMAC v2
   signing `email.ts.a:<areas>.j:<juris>` with both slots always present, accepting the legacy
   shapes for the 7-day link TTL; `digest.py` filtering every selection by `e.jurisdiction`
   with per-state and per-state-area broadcasts; `alert.py` unchanged, since "worth knowing
   today" goes to the main Topic regardless of state.
1. **Overlay to full upgrade** is then flipping the registry entry's `mode` to "full" and
   filling its `courts`: the dropdown label drops " · federal", the subscribe checkboxes
   appear, and the Topics wire up. Nothing else moves.

-----

## Carried (outside the repo)

Two zone-level Cloudflare dashboard items remain from the header audit: the doubled
`Cache-Control` on /fonts/* and the global `Access-Control-Allow-Origin: *`, both from a
Transform or Cache rule or a Worker, not from `_headers`.

## Declined, deliberately

A native App Store app: $99/year, a second codebase, review latency on every tweak, and
Apple's minimum-functionality rule frowns on site wrappers, every cost this roadmap exists to
avoid, for no capability the PWA lacks except App Store presence.
