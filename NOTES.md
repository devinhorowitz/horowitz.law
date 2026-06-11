# opinions.json fix · 2026-06-11

The hand edit removed the LAST property of Martin's object, leaving a trailing
comma before the closing brace — illegal JSON. Symptom: render-sync (and every
scheduled job that loads opinions.json, including the 4-hourly pipeline)
crashed; Issue #18 was filed automatically. The live site never wavered: it
serves committed HTML.

This set is the same edit done validly, plus the two pages it changes, so no
render-sync run is needed:

- **opinions.json** — Martin's `tort_reform` removed properly; everything else
  byte-equivalent. Quynn keeps both its tags per your ruling.
- **archive.html** and **o/5749712.html** — Martin's badge off the page.

Upload all three (the o/ file via the pencil on the existing o/5749712.html,
or skip it and let the nightly bot catch it — but archive.html should go up
with the JSON so CI's idempotency holds). Then close Issue #18.

**The habit that prevents this:** when hand-editing opinions.json in the web
editor, choose "Create a new branch and start a pull request" instead of
committing straight to main. CI runs on the PR and is your JSON linter — a
trailing comma fails the smoke check before it can touch the pipeline.
