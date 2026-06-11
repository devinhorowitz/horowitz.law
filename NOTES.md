# Card corrections from the re-validation flags · 2026-06-11

Issue #19 is the published-card re-validation layer working: the maintenance
run crosschecks three live cards per pass against their opinions, and it
flagged two summaries for misstating the holding. Per house rule I verified
the flags before acting — with different results for each card, handled
differently and labeled as such.

## Giles v. Greenhouse — VERIFIED, quote for quote

I recovered the full opinion (it was cached in golden-set history) and the
flag is right; the old card was wrong in both directions. The opinion:

- Division 1: "Greenhouse concedes that, as his landlord, it owed him such a
  duty. We agree." (the card claimed the court *rejected* foreseeability)
- Division 3, the actual holding: "Giles failed to establish the causation
  element of his negligence claim."
- Division 4, verbatim: "...barred by the equal-or-superior-knowledge
  doctrine and that he assumed the risk... we need not address these
  arguments because Giles failed to establish the causation element." (the
  card claimed the court *applied* equal-or-superior knowledge)

New card: duty conceded; affirmance rests solely on causation; defenses
expressly not reached. The pre-SB 68 transition note stays — it was accurate.

## Mejia v. SK Battery — corrected, BOUNDED TO THE FLAG

CourtListener's bot challenge blocks this sandbox from the Mejia opinion, so
this correction adopts exactly what the flag asserted and nothing more.
**Verify two points against the opinion (linked from the card) before
merging:**

1. Assumption of risk is analyzed as a distinct affirmative defense (not as
   a sub-rule of O.C.G.A. § 51-3-1), and it is the dispositive ground.
2. The primary basis is the decedent's ACTUAL knowledge — he watched a
   coworker fall through the same louvers twelve days earlier — with the
   training record and written acknowledgment as secondary support.

The draft deliberately omits any party name the flag mentioned but the card
never used, and keeps "the decedent."

## Editor's notes — yours to ratify

Both cards carry a short public editor's note recording the correction. They
are drafted in your voice for your ratification: edit or strike either in
opinions.json before uploading; merging adopts them as yours.

## Upload set (same paths) and closeout

opinions.json · opinions.html · archive.html · opinions.xml ·
o/10785227.html · o/10779964.html

Recommended route: web-edit/upload via "Create a new branch and start a pull
request" so CI lints everything first. After it lands: close Issue #19. The
next maintenance pass over these cards re-validates the corrected text; if
Mejia's two points check out on your read, that pass should come back clean.
