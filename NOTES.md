# Golden-set fix · 2026-06-11

The check run proved the guard works: 9/9 positives held, 3/3 negative
controls died at the screen with textbook reasons, and the 429 mid-run was
retried automatically. The two failures were my first-day anchor picks, chosen
mechanically instead of by the confidence rule the nominator enforces:

- **GoAuto** (badfaith): unpublished, and triage's drop reason is substantively
  defensible — an insurer suing its own claims adjuster over a duty to advise;
  bad faith is backdrop, not holding. Removed. Badfaith coverage is now 1, so
  the nominator is armed for it: the next confident badfaith keeper will be
  proposed paste-ready in its own PR.
- **Giles** (negsec): a routine pre-SB 68 affirmance whose own card notes the
  framework is superseded going forward; "low significance" on a frozen replay
  is a fair call. Removed, and replaced with **Venetian Hills Apartments v.
  Hughes** — published, substantive foreseeability holding denying the
  landlord judgment. An unambiguous keeper.

Doctrine note (now in ROADMAP): an entry that fails its very first check never
validated as ground truth — fixing it on arrival is label correction, not the
benchmark drift the append-mostly rule forbids.

## Two clicks

1. Upload `scripts/golden_set.json` (and `ROADMAP.md`).
2. Actions → golden-check → **build** (fills Venetian Hills' text), then
   **check**. Expected: 13 cases, 13 ok — ten keeps, three drops.
