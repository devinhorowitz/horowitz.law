# Final mile, new strategy: upload SOURCES only, the bot renders · 2026-06-11

What happened: a drift-detecting bot (bot/opinions-render) re-rendered the
pages itself after your data PRs, your completion upload landed onto its PR
branch, and the merge kept the bot's versions of most pages. Net: ALL CARD
CONTENT IS NOW CORRECT AND LIVE (dupe gone, four new permalinks up, Aspen's
note rendered) — only the app layer is half-installed and CI is red on a
token mismatch. From now on, derived pages never ride an upload again; the
render bot owns them.

## Step 1 — upload these 9 source files (one small drag, same paths)
app.js (the service-worker registration — the missing heartbeat)
scripts/render.py (permalink heads get the app tags)
index, resume, colophon, subscribe, 404, archive, digests (.html — the seven
shells whose heads never landed; opinions/changes/stats already carry theirs)
Branch-and-PR. CI on this PR may still show the token complaint — merge
anyway; step 2 cures it.

## Step 2 — let the bot finish
Actions → render-sync → Run workflow (the same mechanism that produced
PR #24; it also self-runs daily at 05:37 UTC as a drift net). It re-stamps every token, regenerates all
36 permalinks with app heads, and opens a PR. Merge it. CI goes green; the
app goes fully live.

## Step 3 — the standing queue, unchanged
- Montgomery backfill: dry_run ✓ + seed `10858760:scotus` → review → live run → PR → merge.
- Shepard's → Aspen "treatment" → "negative".
- Phone: Safari → /opinions → Share → Add to Home Screen → airplane test.
- Litter (7): COMMIT_NOTES, INTEGRATION, NOTES, PHASE1/2/3_NOTES, UPLOAD-NOTES (.md).
- Branches to delete: bot/opinions-render, bot/opinions-update,
  bot/opinions-treatment, tagfill, devinhorowitz-patch-1/-2/-3.
