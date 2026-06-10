# Roadmap: integrating the extension set

Sequenced by one principle: **marginal API cost.** Almost everything proposed is a
render-time transformation of `opinions.json`, client-side JavaScript, or static
pages — zero additional CourtListener calls, zero additional model tokens. The
only structurally expensive item is the second jurisdiction, which is why it goes
last. The instinct that Florida threatens the CourtListener budget and the model
bill is correct, and the numbers below say so.

## The budget picture, plainly

**CourtListener.** The free tier is 5/min, 50/hr, 125/day (cl_rate paces all of
it, and opinion *text* already rides the storage PDFs, which cost no REST quota).
Georgia is two state courts. Florida is a supreme court plus **five** District
Courts of Appeal — a volume multiple, not an increment. Levers when the time
comes: a Free Law Project membership (raises the limits *and* funds the data
source — the one donation that literally buys capacity), `OPINIONS_MAX`, a
court-subset ramp (fla + one DCA first), and schedule offsets so the two states
never contend for the same windows.

**Claude.** The three-tier cascade is the cost control: Haiku absorbs the
firehose, Sonnet reads only survivors, Opus writes only keepers. Florida’s token
growth is roughly proportional to its candidate volume. Everything in Phases
1–4 costs **$0 ongoing**; the two one-time exceptions are pennies and are
flagged inline.

## Engineering invariants for every phase

Any new page must carry the byte-identical pre-paint inline `<script>` (the CSP
pin) and be added to `check_site.py`’s `PAGES` list so the guard covers it. Any
new generated artifact must be deterministic from `opinions.json` so the CI
render-idempotency step stays green. Asset `?v=` tokens self-stamp via
`render.py`; nothing new to remember there.

-----

## Phase 0 — truth and footing *(this commit)*

- **Colophon truth-pass.** Three claims had drifted: “No newsletter signups”
  (there is one now), “No third-party scripts” (Turnstile loads on /subscribe),
  and “nothing is rendered server-side” (the subscribe endpoint is a Function).
  All three corrected in the site’s own voice — the exception named honestly
  rather than the claim quietly weakened.
- **“Under the hood” section** (`/colophon#under-the-hood`): the funnel, the
  tripwire doctrine, the golden set, the rate governor, with terminal-lines to
  /PIPELINE.md and the feed. This section is the future home of the specialized
  feeds directory as Phase 2–3 ship them.
- **“Keeping it running” section**: the support ask, drafted deletable (one
  self-contained `<section id="keeping-it-running">`). **Decision gate before
  merge:** github.com/sponsors/devinhorowitz currently redirects to the profile
  — Sponsors is not enrolled. Either enable it (Settings → Sponsors, Stripe
  onboarding, ~15 min), swap the link (Ko-fi / Buy Me a Coffee work but take
  fees), or delete the section and sleep on it. Everything else in the file
  stands alone.

Cost: $0.

## Phase 1 — reader tools *(client-side only)*

- **Filter state in the URL.** Only `?q=` survives a share today; push the area
  chips, court toggle, and jurisdiction select through `URLSearchParams` so
  “every trucking case” is a sendable link.
- **Copy-citation button per card**, assembled from fields the JSON already
  holds, labeled with the house verify-on-Shepard’s caveat.
- **Print stylesheet** for /opinions and /archive: a filtered set becomes a
  binder-ready digest.

Files: opinions.js, base.css, render.py card chrome. Cost: $0.

## Phase 2 — new surfaces from existing data *(render-time only)*

- **Corrections changelog + corrections RSS.** The treatment machinery already
  stamps `treatment_date` and the email digest already reports it; give it a web
  face (a section or /changes) and an RSS item type. The most distinctive thing
  the pipeline does, finally public outside email.
- **Per-opinion permalinks**: deterministic stub pages per card (own OG tags,
  Article JSON-LD, sitemap entries). Extend `check_site.PAGES` accordingly.
- **Stats page**: volume by court/area/quarter, disposition mix, treatment
  counts — rendered from the JSON in the terminal aesthetic.

Files: render.py (+ small templates), sitemap handling, check_site. Cost: $0.

## Phase 3 — distribution *(configuration, not computation)*

- **Per-area subscriptions**: the Resend architecture is already Topic-scoped;
  N topics, checkboxes on /subscribe, a per-topic filter in digest.py.
- **Digest archive page** for cadence-proof.
- **Instant-alert broadcast** for a landmark merge, gated by a flag in the PR
  body — reuses the merged card; one Resend call.

Files: subscribe.html/functions, digest.py, one workflow tweak. Cost: $0 model
spend; Resend free tier holds.

## Phase 4 — taxonomy and trust *(one-time pennies)*

- **SB 68 / tort-reform tracker tag** + **first-impression badge.** Tag forward
  at triage ($0 ongoing); backfill the ~32 archive cards from their existing
  synopses with Haiku (cents) or by hand in an evening.
- **`editor_note` field**: a human-analysis layer rendered visually distinct
  from the AI synopsis — the credibility complement to the AI label.
- **Golden-set hardening** (BACKLOG #1 and #2: negative controls, thin-area
  anchors). One-time build fetches for the controls; check-mode pennies. This is
  the explicit prerequisite for ever exercising PIPELINE.md’s “dropping the
  gate” path.

## Phase 5 — Florida *(last, and here is the math that says so)*

The registry was built for this (`jurisdictions.py`: one entry per state), and
its own comments name the remaining work: parameterize the three Georgia-written
prompts, “validated against a second-state test.” Sequence inside the phase:

1. **Florida golden set first** — known keepers and controls from the FL
   practice, cached once.
1. **Prompt parameterization**, checked against that set before anything runs
   on a schedule.
1. **Court-subset ramp**: fla + 3rd DCA to start; expand DCA-by-DCA as the
   budget proves out. Decide on Free Law Project membership at this point, not
   before — it is the capacity lever and the right donation.
1. **Output design question** to settle: unified pages with the jurisdiction
   filter (the selector already exists and is pre-wired) plus per-state feeds,
   versus separate /fl pages. Recommendation: unified page, namespaced feeds.
1. **Schedule offsets** so GA and FL runs never share a rate window.

## Carried items (outside the repo)

Two zone-level Cloudflare dashboard items remain from the header audit: the
doubled `Cache-Control` on /fonts/* and the global
`Access-Control-Allow-Origin: *` — both come from a Transform/Cache rule or
Worker, not from `_headers`.