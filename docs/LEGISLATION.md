# Legislative Watch

The appellate watch reads decisions. This reads **statutes** — it watches the Georgia General
Assembly for bills that *became law* (signed by the Governor, or allowed to become law without
signature) and vetoes, and drafts a plain-language card for the ones that touch a
personal-injury / insurance-defense / civil-litigation practice. It is a sibling pipeline to the
opinion funnel, deliberately built to reuse its shape rather than duplicate it.

## Why statutes are a different beast (and a different source)

Case law, regulations, and statutes are three different watches, not one:

- **Statutes** — the General Assembly (and, rarely for this practice, Congress). The trigger is
  *enactment*: signed, or became law without signature. This is what v1 covers, for Georgia.
- **Agency regulations** — e.g. **FMCSA** rulemaking, which lives in the **Federal Register**
  (49 CFR 350–399), not in any legislature. A different source (the Federal Register API), a
  different cadence, tracked separately. Not in v1.
- **Court rules** — the **FRCP / FRE**, which change through the Rules Enabling Act on a slow
  annual cycle and are published by uscourts.gov, not enacted as ordinary statutes. Lowest ROI to
  automate; a manual once-a-year check is often the right tool. Not in v1.

Congress matters to this practice mainly through a handful of statutes — the **FAAAA**
preemption that governs motor-carrier claims, the statutes that authorize FMCSA, the federal
jurisdiction statutes (diversity, removal, CAFA) — and those ride the *same* funnel as Georgia
with `LEGISLATION_STATES=GA,US`. Georgia is the curated core, screened permissively; the federal
overlay is screened **strictly** (default DROP), because almost all enacted federal law
(appropriations, the NDAA, foreign affairs) is irrelevant and only a narrow set reaches a state
civil practice. This is exactly the core-plus-federal shape the opinion feed already has. Note the
federal *legislative* surface relevant here is genuinely thin — the federal *regulatory* surface
(FMCSA) is the part that actually moves, and that is a Federal Register job (above), not this one.

## The data-source unlock: LegiScan

There is no clean, free, machine-readable "enacted Georgia statutes" feed from the state;
legis.ga.gov sits behind the same class of WAF that already blocks the Court of Appeals docket
(see `official_ga.py`). **LegiScan** (legiscan.com) is the unlock: it normalizes all 50 states'
and Congress's legislation into one JSON API with a **normalized status enum**, so Georgia's
"signed" and "became law without signature" both collapse onto a single value we can filter on —
we do not have to parse each bill's constitutional timing ourselves.

```
LegiScan bill.status:
  1 Introduced   2 Engrossed   3 Enrolled   4 Passed/enacted   5 Vetoed   6 Failed/dead
```

We card **4 (became law)** and **5 (vetoed)** — a vetoed tort bill is news to this audience too —
and skip everything below. `status_date` is the enactment/veto date. Each bill carries a
`change_hash`, so a run only re-fetches a bill's detail when it actually moved: a scan that finds
nothing new costs a **single master-list call**.

The API is RPC-style: `https://api.legiscan.com/?key=KEY&op=OP&...`. We use `getSessionList`
(find the live Georgia biennium), `getMasterList` (per-session bill summaries with status +
change_hash), and `getBill` (full detail for a survivor). A free key enables all three.

## The funnel (mirrors the opinion pipeline)

`scripts/legislation.py`, standard library only, with the same discipline as `update.py`:
**network and model calls are injectable seams** (`fetch=`, `ai=`) so the whole thing is
unit-testable with no network and no key, and everything **fails open** — no key, a LegiScan
outage, or a model error yields an empty run and a logged note, never a crash and never a false
card.

1. **Discover** — `getSessionList` → the sessions covering the live two-year term; `getMasterList`
   per session → the enacted/vetoed bills whose `change_hash` moved since `legislation_state.json`
   last recorded them.
2. **Screen (cheap, Haiku)** — reads number + title + description and drops the bulk
   (appropriations, licensing boards, local/special acts, criminal-only, elections). **Fails
   open**: a model error keeps the bill for the writer rather than dropping a real law.
3. **Detail** — `getBill` for each survivor (title, description, progress, `state_link`).
4. **Write (Opus)** — a tight, neutral, plain-English card: what the law changes and why a civil
   litigator should care, grounded in the provided text (no invented code sections or dollar
   figures). **Fails closed**: a decline or an error yields no card, never a partial one.
5. **Card** — assembled and keyed on LegiScan `bill_id` (unique, stable, the permalink slug).

Practice areas reuse `siteconfig.AREA_CODES` (the opinion taxonomy transfers cleanly: tort reform →
`damages`/`procedure`, trucking → `auto`, and so on), so the same filters and RSS categories serve
both watches.

## Trust model: no auto-publish

The opinion pipeline has a machine-verified auto lane. **Legislation does not.** Enactments are
low-volume (a few dozen relevant Georgia bills a year, trickling in near session end) and
high-consequence — a wrong statute card is worse than a late one — so v1 routes **every** relevant
bill to a human to confirm before publish. There is no path by which a legislation card reaches the
public page without a person merging it. That is deliberate and matches the appellate trust model's
spirit: auto-publish only what is both high-volume and cheaply machine-verifiable, which enacted
statutes are not.

## Card schema (`legislation.json`)

Keyed on `bill_id`. One object per bill:

| field | meaning |
|---|---|
| `bill_id` | LegiScan bill id — unique key and permalink slug. Never changes. |
| `state` | `GA` (or `US` for the optional federal watch). |
| `number` | `SB 68`, `HB 900`, … |
| `title` | the bill's official title. |
| `status` | normalized: `enacted` or `vetoed`. |
| `status_date` | enactment / veto date (`YYYY-MM-DD`). |
| `effective_date` | when it takes effect, if the text states it; else empty. |
| `areas` | practice-area codes from `siteconfig.AREA_CODES`. |
| `synopsis` | 2–4 sentence plain-English summary of what changed. |
| `impact` | one-line relevance to a civil practice. |
| `url` | LegiScan bill page. `state_link` | the state's own page. |
| `change_hash` | LegiScan change_hash at card time — drives cheap re-scan. |
| `first_seen` | discovery date; preserved across later amendments. |

`merge_cards` keys on `bill_id`: a re-carded bill (its `change_hash` moved — an amendment, a
correction) replaces its card but keeps the original `first_seen`.

## The workflow

`.github/workflows/legislation.yml` runs weekly (Sundays), under Harden-Runner block mode with only
`api.legiscan.com`, `api.anthropic.com`, and the install/GitHub hosts on its allowlist. It mirrors
the opinion pipeline's commit model:

- **New/updated cards** → re-render and open (or update) the `bot/legislation-review` PR for a
  person to confirm. Merge to publish; edit `legislation.json` on the branch to correct.
- **No new cards** → commit only the advanced seen-state and run log straight to `main` with
  `[skip ci]` (a settled non-match must not be re-screened, but nothing public changes).

A manual dispatch defaults to `dry_run` (prints the drafted cards, writes nothing).

## Going live

One operator step: **add the `LEGISCAN_API_KEY` secret** (Settings → Secrets and variables →
Actions). A free key from legiscan.com/legiscan. Until it is set, every scheduled run is a clean
no-op — the workflow is already committed and safe to leave enabled. The `ANTHROPIC_API_KEY` secret
the opinion pipeline already uses is reused here.

Preview it by hand first (needs both keys):

```
LEGISCAN_API_KEY=... ANTHROPIC_API_KEY=... python scripts/legislation.py --json
```

Optional repo Variables tune it without editing code: `LEGISLATION_STATES` (default `GA,US` — a
comma list of LegiScan jurisdictions; set to `GA` to drop the federal overlay), plus
`LEGISLATION_SCREEN_MODEL`, `LEGISLATION_MODEL`, `LEGISLATION_MAX`.

## Status

- **Shipped:** the core funnel (`scripts/legislation.py`) and its hermetic tests
  (`scripts/test_legislation.py`); the public `/legislation` page and `legislation.xml` feed
  (rendered by `scripts/render.py`, registered in `siteconfig.PAGES` and `check_site.py`, covered
  by the CI idempotency and CSP/token guards); and the weekly `legislation.yml` workflow with its
  review-PR wiring; and the **federal statute overlay** (`LEGISLATION_STATES=GA,US`) — the FAAAA /
  motor-carrier and federal-jurisdiction statutes that reach a Georgia practice, screened strictly,
  rendered with a `U.S.` jurisdiction label beside the Georgia core on the same page. Add the key
  to turn it all on.
- **Later, by source:** the FMCSA **regulatory** watch (Federal Register API — where FMCSA actually
  moves); the FRCP/FRE rules check (uscourts.gov, low-frequency).
