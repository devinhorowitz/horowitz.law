# Multistate v3 — federal overlay · 2026-06-11
**(supersedes both earlier multistate bundles — upload this one)**

Your argument, implemented: the feed already holds decisions that bind
Florida and Alabama by judicial hierarchy, so the jurisdiction dropdown now
says so — live on upload, at zero added cost, with the screening untouched.

## Your four claims, answered in code

1. **"We're already finding Florida cases"** — yes, as *Georgia authority*:
   the Eleventh Circuit binds GA/FL/AL federal courts alike. The dropdown now
   exposes that bindingness: it offers `georgia`, `florida · federal`, and
   `alabama · federal`, and every ca11 card is stamped
   `data-jurisdiction="ga,fl,al"`.
2. **Alabama screening** — already safe, no change needed: the funnel screens
   for Georgia *relevance*, never for state of origin. An Alabama-origin
   Eleventh Circuit case that matters to Georgia practice has always passed.
3. **SCOTUS from anywhere** — handled by derivation, forever:
   `jurisdictions.court_binds()` maps each federal court to the jurisdictions
   it binds (`ca11 -> ga/fl/al`, `scotus -> "*"` = every registered state,
   present and future). Bindingness is computed at render time, never stored
   on cards — so no backfills, and a fourth state registered years from now
   retroactively inherits every SCOTUS card with zero data changes. (No
   SCOTUS cards in the record yet; the rule is wired and waiting.)
4. **Free** — confirmed: intake, prompts, and API usage are untouched.

## The one caveat for your ruling (Erie)

Court-level bindingness slightly overclaims on diversity cases: an Eleventh
Circuit decision applying *Georgia substantive law* will appear under
`florida · federal` too. The label itself carries the honesty at the point of
choice, and the synopsis self-identifies the law applied. If you ever want it
tighter, ROADMAP Phase 4 now logs a one-word triage field ("substantive law
applied") that would let render narrow overlay membership per card — pennies.
Until then the label does the work.

## Why the subscribe form stays dormant

Filters show what exists; subscriptions promise curation. The screen curates
for Georgia relevance only, so a "Florida" mailing list today would promise a
stream we don't curate. The form's gate counts *fully-covered* states: the
moment Florida's registry entry flips `mode` to "full" with real courts
(Phase 5), the checkboxes appear and the topic wiring follows the locked
design. Overlay states get the dropdown now, the inbox later.

## Verified

Dropdown renders 3 options on opinions + archive; ca11 cards stamped
`ga,fl,al`; state cards `ga`; subscribe form dormant with the new comment;
`?juris=fl` is a working shareable filter (membership test shipped in v2);
full CI replication green; render idempotency clean.

## Upload set (same paths)

ROADMAP.md · opinions.js · subscribe.html · subscribe.js ·
scripts/jurisdictions.py · scripts/render.py · opinions.html · archive.html

**Optional:** o/9391147.html, o/10872097.html, o/10872980.html (the three
Eleventh Circuit permalinks pick up the new stamp). Skip them if you like —
the nightly render-sync bot will commit them on its own.
