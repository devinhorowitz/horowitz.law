# horowitz.law

The source for [horowitz.law](https://horowitz.law), the hand-coded personal site of Devin R.
Horowitz, a civil-litigation attorney in Marietta, Georgia. It also runs Georgia Appellate
Watch, an automated feed of new appellate opinions for an insurance-defense and civil-litigation
practice. The pipeline's architecture and setup are in [PIPELINE.md](./PIPELINE.md); how the site
is maintained is in [MAINTENANCE.md](./MAINTENANCE.md); what's planned is in
[ROADMAP.md](./ROADMAP.md).

## stack

This site is hand-coded HTML, CSS, and JavaScript. No framework. No build step. No compiled
bundles. JetBrains Mono is self-hosted in four weights. The QR code, portrait corner brackets,
favicon, and other ornamentation are SVG. Everything else is plain CSS.

The typing animation in the hero, the analog flicker on theme transitions, and the click sound on
the theme toggle each carry a small amount of randomization, so no two page loads are exactly
alike. Small analog gestures in a digital medium.

## hosting

Deployed automatically from GitHub on every push and served over a global CDN. DNSSEC is active,
HSTS is set, and the Content Security Policy is strict. The site is static — nothing is rendered
server-side, and nothing is fetched at runtime from anywhere but the CDN edge — with one
deliberate seam: the subscribe endpoint runs as a small serverless function, because a mailing
list cannot be static.

## under the hood

The opinions feed is not hand-typed. Every four hours a pipeline wakes on GitHub Actions, checks
CourtListener for new appellate decisions from Georgia and the federal courts above it, with
Florida and Alabama alongside, and runs them through three models, cheapest first: one glances at
case names and openings and discards the categorically unrelated, one reads the survivors in full
against a narrow relevance bar, and one drafts the card — and may still decline. Nothing publishes
without a human merge.

A second process is paid to distrust the first. Each Saturday a reverse sweep walks every
published card's citation graph looking for later decisions that treat it adversely. It is a
tripwire, not a citator: the machine may only ever raise a flag to caution. Declaring a case bad
law remains human work, done on Shepard's, by hand.

The site is also installable: a web-app manifest and a sixty-line service worker make it a
home-screen app on a phone — Share, then Add to Home Screen — with offline reading of the
last-fetched cards. Pages load network-first so signal always means fresh; hash-stamped assets
cache-first, because the tokens make them immutable; the feeds and the subscribe API are never
intercepted.

A golden set of known cases re-runs against the live prompts daily, so a quiet model change
cannot silently move the editorial line. A rate governor paces every CourtListener call inside the
free tier's rolling windows. The CI refuses any page whose cards have drifted from the data. Every
job that touches the network runs under egress control: the ones with a fixed host list are locked
to it, so a poisoned dependency cannot phone home, and the two that must crawl the open web, the
link and Lighthouse checks, run in audit instead. The whole machine is public — the pipeline
doc, the prompts, the state files, even the rejections.

## what isn't here

No analytics. No cookies. No fingerprinting. No tracking pixels. No ads. No popups. No funnels.

One honest exception each way: the subscribe form loads a single third-party script — a bot check
— and a confirmed subscriber's address is kept by the mail service for exactly one weekly digest,
with a one-click way out. *Beyond that, the site does not know you came here, and will not remember
if you come back.*

## source

The source is open. Borrow bits for a personal project: that's how the open web is supposed to
work. For commercial use, please reach out first.
