# Completion upload: everything PRs #22/#23 didn't carry · 2026-06-11
(read me, don't upload me — and the repo gained an UPLOAD-NOTES.md from #22;
add it to the litter-delete list, making seven)

Nothing here is new work: byte-identical remainder of the v2 bundle, filtered
by comparison against main so nothing already-landed repeats. Until this
merges, the live feed still shows the Bloodworth duplicate, Aspen's note is
invisible, the four new cards' permalinks 404, and CI is red on 13 files.
One merge fixes all of it.

## Step 0: one tiny web edit (replaces dragging a dot-folder)
Open .github/workflows/ci.yml in the GitHub editor on your new branch and
find the line containing:
    node --check subscribe.js
Append to that same line:
     && node --check sw.js
(That registers the new worker in CI's syntax checks; it was the only file
living under .github in this bundle, so now nothing dot-prefixed to drag.)

## Upload technique (why 54 files stayed behind last time)
On the new branch's upload screen, DRAG THE FOLDERS THEMSELVES from this
bundle — o, scripts, .github — plus all loose files, in one drag. Folder
drags preserve paths; per-file picking is what dropped the rest. You've done
the o-folder drag before (the 32-permalink upload).

## After it merges
1. Hard-refresh on desktop once; then iPhone Safari → /opinions → Share →
   Add to Home Screen → "GA Watch". Airplane-mode test.
2. Montgomery is STILL not carded (my earlier "you ran it" was wrong — the
   json hit was Aspen's treated_by pointer). Actions → backfill →
   dry_run ✓ + seed `10858760:scotus` → review the log → re-run unchecked
   → review the PR → merge.
3. Then Shepard's → flip Aspen "treatment" to "negative" (branch-and-PR).
4. Litter delete, now seven: COMMIT_NOTES.md, INTEGRATION.md, NOTES.md,
   PHASE1/2/3_NOTES.md, UPLOAD-NOTES.md.
