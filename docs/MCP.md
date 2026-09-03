# The MCP server

`POST https://horowitz.law/mcp` — a public, read-only MCP server over the Georgia Appellate Watch
content. JSON-RPC 2.0 over HTTP, stateless, no auth.

It is the same material the site already publishes at `/opinions.xml`, `/changes.xml` and the email
digest, **addressed to a model instead of an inbox**. The digest pushes on the sender's schedule and
arrives as prose a person has to re-key into a chat. A routine polling this pulls on *its* schedule,
filtered to the practice areas it cares about, and gets structured deltas it can act on — including
updating a skill that cites law which has since moved.

## Two ideas carry the design

### 1. `changed`, one timestamp per card

A canary answers *"what moved since &lt;date&gt;"*, and two very different things count as movement:

| | |
|---|---|
| A **new** case was carded | `change: "new"` |
| An **old** case was flagged as treated | `change: "treatment"` |

The second matters more — someone who relied on a case last month needs to hear it has been
questioned — and it is **invisible to any feed keyed on publication date**, because the card's date
never changes. The live example:

```
Aspen American Ins. Co. v. Landstar Ranger, Inc.
  date     2023-04-13     <- a publication-date feed shows this
  changed  2026-06-12     <- when it was flagged negative
```

A 2026 cursor must catch that 2023 opinion. So each entry carries
`changed = max(first_seen, treatment_date)` and a `change` kind naming which happened. One field, a
cursor compares it, both kinds are caught.

### 2. The cursor belongs to the caller

`since` is passed in; the server stores nothing per client. That single decision removes auth,
per-user state, and the entire subscription surface. A routine already remembers its own watermark.

## Silence is not success

**The rule this server exists to enforce.** A canary that goes quiet because nothing happened looks
exactly like one that goes quiet because the pipeline stalled — and the second is far more
dangerous, because a canary's whole value is being trusted when it says nothing.

So no tool returns a bare empty list. Every response carries a `feed` block built from two sources
with *different update rules*:

| Source | Written | Answers |
|---|---|---|
| `/status.json` → `scanned_at` | every scan, found anything or not | is the pipeline alive |
| `/status.json` → `content_updated_at` | only when content changed | quiet, or busy |
| `/api/feed.json` → `generated` | when content **changes** (carried over otherwise) | feed age |

`trust_silence` is the field that matters: *if this tool told me nothing changed, should I believe
it?* A routine should refuse to digest, and refuse to conclude nothing happened, when it is `false`.

This is not hypothetical: the live feed has reported a scan 0.7 hours old against content last
changed 113.9 hours ago — a genuinely quiet stretch behind a healthy pipeline, which a consumer
reading only content could not have told from an outage.

### `scanned_at` used to be only as fresh as the last deploy (fixed 2026-09-02)

`opinions.yml` committed `public/status.json` with `[skip ci]` — and **Cloudflare Pages honours that
token too**, which was never the intent. The freshness marker reached production only on the next
commit that *did* deploy, in practice the next content change. `scan_age_hours` therefore measured
**deploy age, not scan age**, and in a quiet stretch it crossed `stale_after_hours` while the funnel
was scanning normally every four hours. Over one 60-day window, 7 gaps between deploying commits
exceeded 36 hours, the longest 113.2h — the canary calling itself dead while perfectly healthy.

The error was in the safe direction (crying wolf, never vouching for a dead pipeline), and
`scripts/heartbeat.py` reads the *committed* file so the dead-man's switch was never affected. But a
routine that halts on `trust_silence: false` would have halted during quiet weeks for the wrong
reason — and the "scan 0.7 hours old" reading quoted above was only possible because an unrelated
merge had just deployed.

**The fix is a one-token swap: `[skip ci]` → `[skip actions]`** on that commit alone. `[skip actions]`
is a GitHub-only skip token, so Actions still skips (no CI burned on a timestamp) while Cloudflare,
whose documented skip list is CI- and CF-Pages-based, builds. Only the scan-status commit changes;
`record run state`, `keepalive` and `record drop-reason audit verdicts` write unpublished files and
keep `[skip ci]`.

**Verified, both halves.** The GitHub half: an empty commit carrying `[skip actions]` produced zero
`ci.yml` and `ruff.yml` runs while the pushes on either side of it produced runs.

The Cloudflare half was confirmed **against production** on 2026-09-03, and the reading is clean
despite a content publish landing four seconds before the scan-status commit — because the two
carry *different* `status.json` contents, so the live file identifies which one built:

| Commit | Deploys? | `scanned_at` in its tree |
|---|---|---|
| `ab81a57` content publish | yes — no skip token | `2026-09-02T19:34:53Z` |
| `a8a9172` scan status | `[skip actions]` | `2026-09-02T22:46:10Z` |
| **live `/status.json`** | | **`2026-09-02T22:46:10Z`** |

Had Cloudflare honoured the token, live would show the publish's `19:34:53`. It shows the
scan-status commit's content, so Cloudflare built it. `hlaw_feed_status` read `scan_age_hours: 2`
against a 36h threshold minutes later — the first time that field has measured scan age rather than
deploy age.

The accepted cost is a Cloudflare build per scan, roughly 5/day. CI is untouched.

## Use the apex host, not the `pages.dev` alias

`functions/_middleware.js` 301s `horowitz-law.pages.dev` onto the apex, preserving path and query.
That is right for the human site, but a 301 is where many HTTP clients turn a `POST` into a `GET`,
and this endpoint is POST-only. Point clients at **`https://horowitz.law/mcp`**. Hashed preview
deployments (`<hash>.horowitz-law.pages.dev`) are not redirected, so they work for review as-is.

## Tools

| Tool | Job |
|---|---|
| `hlaw_whats_new(since, areas, kinds, verbose, limit, offset)` | Deltas — new cards and treatment changes. Ships the per-area denominator with every response. |
| `hlaw_check_authorities(cluster_ids, names)` | The canary: are cases I rely on flagged? |
| `hlaw_feed_status()` | Health and per-area coverage |

All three are `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`. Both formats
are supported via `response_format` (`json` default, `markdown` for human reading).

## Two honesty constraints, deliberately built in

**Cards are editorial summaries, not primary law.** Every entry carries `url` (and often
`official_url`) to the opinion itself, and `hlaw_feed_status` returns that disclaimer explicitly. The
server feeds verification; it does not substitute for it.

**"Not flagged" is not a clearance.** `hlaw_check_authorities` says so in every unflagged result and
again in the tool description. The feed tracks its own ~113 curated cards and its treatment sweep is
incomplete (1 card flagged today), so an unflagged case has simply not been flagged *here*. A tool
that implied clearance it had not earned would be the same failure as a confident drop reason — and
a mutation test pins the disclaimer so it cannot be dropped.

The per-area denominator rides along for the same reason: `negsec` and `badfaith` hold 4 cards each,
and an answer drawn from 4 cards without saying so is misleading.

## How it is built and deployed

```
scripts/mcp_feed.py    builds public/api/feed.json   (pure; unit-tested)
scripts/render.py      writes it with the other outputs, best-effort
functions/mcp/index.js the server (plain JS, no deps, Cloudflare Pages Function)
```

`_write_mcp_feed` is deliberately best-effort: a render must not fail because the machine feed could
not be built. A failed render would strand the *human* site too, and the server already reports an
unreadable feed as `health: "unavailable"` rather than as silence — so the safe failure is a stale
feed the server correctly refuses to vouch for, never a broken deploy.

`public/api/feed.json` is in `render.OUTPUT_PATHS`, so `publish.py` stages it and `check_site.py`
asserts every workflow's `add-paths` lists it. Adding it without updating those workflows fails CI
rather than stranding the file — which is exactly what happened while building this, in all five.

## Tests

- `scripts/test_mcp_feed.py` — 37 checks on the feed builder
- `scripts/test_mcp.mjs` — 31 checks on the server (`node --test`, no dependencies)

Both run in CI, glob-derived so a new `test_*.mjs` is picked up without editing a hand list.

`test_mcp.mjs` builds its freshness fixtures from `Date.now()`, never a frozen epoch, and a
tripwire test asserts it. An earlier version pinned `NOW` to the day it was written while the
server compared against the real clock: the suite was correct for 33 hours and then began failing
on the calendar rather than on a defect, turning CI red for a day and a half. A test for
*silence is not success* that fails silently is the joke writing itself.

Six mutations were run and all six caught: hardcoding `trust_silence` true, reading a missing
`status.json` as healthy, treating an unreadable feed as ordinary silence, implying clearance in
`check_authorities`, dropping the coverage denominator, and keying `changed` on the opinion date
instead of the treatment date.

## Using it from a routine

```
1. hlaw_feed_status()            -> if trust_silence is false, stop; do not digest
2. hlaw_whats_new(since=<your watermark>, areas=[...])
3. act on results; advance the watermark to the newest `changed` you consumed
4. hlaw_check_authorities(...)   -> for cases your existing material already cites
```

Step 1 is the one to keep. Everything else is convenience; that step is what stops a stalled
pipeline from being read as a quiet week.
