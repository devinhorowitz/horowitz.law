# Phase 1: reader tools · 2026-06-10

Three features, zero marginal API cost, eleven files. The token ripple across
all nine pages happened automatically — render.py's self-stamping recomputed
`base.css?v=3a028eaf4e` and `opinions.js?v=cf1eb0979d` everywhere, and
check_site verified it — so this set is upload-and-done.

## 1. Shareable filter URLs (opinions.js)

Every filter now mirrors into the URL: `?q=` (search, original case kept for
readability), `court=` (state/federal), `area=` (practice-area chip), `juris=`
(only when it departs from the page's default). Defaults are omitted so an
unfiltered page keeps a bare URL; params the script doesn't own are preserved;
`#op-` anchors survive. `replaceState`, not `pushState` — filtering is one
view, not a history trail. On load, params are matched against the controls
that actually exist, so a stale or hand-mangled param is ignored rather than
wedging the page. "Every premises decision in the archive" is now
`/archive?area=premises` — a link you can text to co-counsel. (Today's area
keys: coverage, badfaith, auto, premises, negsec, expert, procedure, damages.)

## 2. Copy-citation button (render.py + opinions.js + base.css)

Each card's foot gains `[ copy cite ]`, copying the Bluebook slip-opinion
form assembled from fields the JSON already holds — live examples from this
render:

    Vu v. City of Atlanta, No. A26A0563 (Ga. Ct. App. June 9, 2026)
    Ya Mon Expeditions, LLC v. YATCO, LLC, No. 25-10140 (11th Cir. June 10, 2026)

Bluebook month forms (May/June/July unabbreviated, Sept.), multi-docket "Nos."
handled by the existing helper, and the court parenthetical reuses the
registry's TITLE_SUFFIX — a second jurisdiction needs no edit here. No
reporter cite is ever included (the funnel deliberately strips them); the
button's title carries the house rule: confirm on Shepard's before filing.

The markup is server-rendered on every card, but the button stays hidden until
opinions.js confirms the async Clipboard API and adds `.can-copy` to `<html>`
— a no-JS or legacy reader never meets a dead control. Click → `[ copied ]`
for 1.4s. textContent only, per the Trusted Types CSP.

## 3. Print digest (base.css)

Print a filtered /opinions or /archive and the chrome drops out — topbar,
filters, year nav, footer, the copy buttons — while `[hidden]` cards are
already excluded by the browser, so whatever the screen shows is what lands
in the binder. Cards refuse to split across pages; CourtListener URLs print
after their links. Ink-safe palette mirrors the resume's print block
(`!important` because the pages' own screen variables load after base.css).
Deliberately KEPT on paper: the prototype banner and every per-card
"AI-drafted summary · verify against the opinion" line. The honesty travels.

## Upload set (same paths)

opinions.js · base.css · scripts/render.py · opinions.html · archive.html ·
index.html · resume.html · colophon.html · subscribe.html · 404.html

## After deploy

1. `/archive?area=negsec&court=state` — chips arrive pre-pressed, count shown.
2. Filter by hand; watch the URL follow; share it; Escape clears search.
3. Click `[ copy cite ]` on any card; paste — slip cite, ready to italicize.
4. Print preview a filtered archive: clean digest, disclaimers intact.
5. CI: invariants + render idempotency both replicated green locally.
