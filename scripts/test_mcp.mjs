// Hermetic tests for the public MCP server (functions/mcp/index.js).
//
// No dependencies and no package.json: the runner is node:test (built in), and the Function is
// loaded by reading the file and importing it as a data: URL module -- the same trick
// test_functions.mjs uses, which keeps test files out of functions/, where Cloudflare derives its
// routes from the file tree. Every network read is stubbed, so these run offline.
//
// The contract worth defending here is not the JSON-RPC plumbing. It is this:
//
//     A CANARY THAT GOES QUIET BECAUSE NOTHING HAPPENED MUST NOT LOOK LIKE
//     A CANARY THAT GOES QUIET BECAUSE THE PIPELINE STOPPED.
//
// Those are indistinguishable in any feed that reports only content, and the second is far more
// dangerous, because the whole value of a canary is being trusted when it says nothing. The server
// therefore reads two sources with different update rules -- status.json (written every scan) and
// api/feed.json (written when content changes) -- and never returns a bare empty list. The tests
// below drive both stale and fresh clocks and assert on `trust_silence` in each case. This repo has
// already had an 82-hour content gap behind a healthy scan, so the case is not hypothetical.
//
// Run: node --test scripts/test_mcp.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = fs.readFileSync(path.join(REPO, "functions/mcp/index.js"), "utf8");
const mcp = await import("data:text/javascript," + encodeURIComponent(src));

const NOW = Date.parse("2026-08-30T12:00:00Z");
const hoursAgo = (h) => new Date(NOW - h * 3600000).toISOString().replace(/\.\d+Z$/, "Z");

const FEED = {
  schema: 1,
  generated: hoursAgo(2),
  counts: { cards: 3, watches: 1, by_area: { premises: 1, coverage: 1, damages: 1, auto: 1 }, treated: 1 },
  cards: [
    { cluster_id: 111, name: "Alpha v. Beta", court: "ctapp", date: "2026-08-20",
      areas: ["premises"], url: "https://cl/111", why: "premises point", synopsis: "long text",
      changed: "2026-08-20", change: "new" },
    { cluster_id: 222, name: "Aspen American Ins. Co. v. Landstar Ranger, Inc.", court: "ca11",
      date: "2023-04-13", areas: ["coverage"], url: "https://cl/222", why: "duty to defend",
      treatment: "negative", treatment_date: "2026-06-12", treatment_note: "Overruled in part",
      treated_by: [{ cluster_id: 333, name: "Later Case" }],
      changed: "2026-06-12", change: "treatment" },
    { cluster_id: 444, name: "Gamma v. Delta", court: "scotga", date: "2025-01-05",
      areas: ["damages", "auto"], url: "https://cl/444", why: "damages point",
      changed: "2025-01-05", change: "new" },
  ],
  watches: [{ number: "HB 1", title: "A bill", changed: "2026-07-01", change: "legislation" }],
};

/** Stub global fetch for the two documents the server reads. */
function stub({ status = { scanned_at: hoursAgo(3), content_updated_at: hoursAgo(30) },
                feed = FEED, feedFails = false } = {}) {
  const calls = [];
  globalThis.fetch = async (url) => {
    const u = String(url);
    calls.push(u);
    if (u.endsWith("/status.json")) {
      return status === null
        ? new Response("nope", { status: 404 })
        : new Response(JSON.stringify(status), { status: 200 });
    }
    if (u.endsWith("/api/feed.json")) {
      return feedFails
        ? new Response("boom", { status: 500 })
        : new Response(JSON.stringify(feed), { status: 200 });
    }
    return new Response("?", { status: 404 });
  };
  return calls;
}

async function rpc(method, params, env = {}) {
  const req = new Request("https://horowitz.law/mcp", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const res = await mcp.onRequestPost({ request: req, env });
  return { res, body: res.status === 202 ? null : await res.json() };
}

async function call(name, args, env = {}) {
  const { body } = await rpc("tools/call", { name, arguments: args || {} });
  return body.result;
}

// ------------------------------------------------------------------ protocol

test("initialize advertises the protocol, the server and how to use it", async () => {
  stub();
  const { body } = await rpc("initialize", {});
  assert.equal(body.result.serverInfo.name, "horowitz-law-mcp-server");
  assert.ok(body.result.protocolVersion);
  assert.equal(body.result.capabilities.tools.listChanged, false);
  // The instructions must warn about silence, because a client that ignores the feed block is
  // exactly the client that will misread a stalled pipeline as a quiet week.
  assert.match(body.result.instructions, /trust_silence/);
  assert.match(body.result.instructions, /not primary law/);
});

test("tools/list returns three read-only tools with schemas", async () => {
  stub();
  const { body } = await rpc("tools/list", {});
  const names = body.result.tools.map((t) => t.name).sort();
  assert.deepEqual(names, ["hlaw_check_authorities", "hlaw_feed_status", "hlaw_whats_new"]);
  for (const t of body.result.tools) {
    assert.equal(t.annotations.readOnlyHint, true, `${t.name} must be read-only`);
    assert.equal(t.annotations.destructiveHint, false);
    assert.equal(t.inputSchema.type, "object");
    assert.ok(t.description.length > 80, `${t.name} needs a real description`);
  }
});

test("a notification gets 202 and no body; an unknown method is a JSON-RPC error", async () => {
  stub();
  const req = new Request("https://horowitz.law/mcp", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
  });
  const res = await mcp.onRequestPost({ request: req, env: {} });
  assert.equal(res.status, 202);
  const { body } = await rpc("no/such/method", {});
  assert.equal(body.error.code, -32601);
});

test("malformed JSON is a parse error, not a crash", async () => {
  stub();
  const req = new Request("https://horowitz.law/mcp",
    { method: "POST", headers: { "content-type": "application/json" }, body: "{not json" });
  const res = await mcp.onRequestPost({ request: req, env: {} });
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error.code, -32700);
});

test("GET describes the server rather than erroring", async () => {
  const res = await mcp.onRequestGet();
  assert.equal(res.status, 200);
  const b = await res.json();
  assert.equal(b.server.name, "horowitz-law-mcp-server");
  assert.equal(b.tools.length, 3);
});

// ------------------------------------------------------------------ THE staleness contract

test("a fresh scan with quiet content: empty is trustworthy", async () => {
  // Scanned 3h ago, content unchanged for 30h. Nothing happened -- and that is a real answer.
  stub({ status: { scanned_at: hoursAgo(3), content_updated_at: hoursAgo(30) } });
  const r = await call("hlaw_whats_new", { since: "2026-08-29" });
  assert.equal(r.structuredContent.feed.health, "ok");
  assert.equal(r.structuredContent.feed.trust_silence, true);
  assert.equal(r.structuredContent.results.items.length, 0);
  assert.match(r.structuredContent.feed.note, /genuinely means nothing changed/);
});

test("a stale scan: empty is NOT trustworthy, and the response says so", async () => {
  // The failure this whole design exists to prevent. Same empty result as above; opposite meaning.
  stub({ status: { scanned_at: hoursAgo(200), content_updated_at: hoursAgo(400) } });
  const r = await call("hlaw_whats_new", { since: "2026-08-29" });
  assert.equal(r.structuredContent.feed.health, "stale");
  assert.equal(r.structuredContent.feed.trust_silence, false);
  assert.match(r.structuredContent.feed.note, /CANNOT be read as/);
  assert.match(r.structuredContent.feed.note, /pipeline stopped/);
});

test("the two silences are distinguishable from the payload alone", async () => {
  // A consumer must be able to tell them apart WITHOUT knowing the wall clock.
  stub({ status: { scanned_at: hoursAgo(3), content_updated_at: hoursAgo(400) } });
  const quiet = (await call("hlaw_whats_new", { since: "2026-08-29" })).structuredContent;
  stub({ status: { scanned_at: hoursAgo(400), content_updated_at: hoursAgo(400) } });
  const broken = (await call("hlaw_whats_new", { since: "2026-08-29" })).structuredContent;
  assert.equal(quiet.results.items.length, broken.results.items.length, "both empty");
  assert.notEqual(quiet.feed.trust_silence, broken.feed.trust_silence, "but not equally trustworthy");
});

test("a missing status.json is treated as stale, never as healthy", async () => {
  stub({ status: null });
  const r = await call("hlaw_feed_status", {});
  assert.equal(r.structuredContent.feed.health, "stale");
  assert.equal(r.structuredContent.feed.trust_silence, false);
});

test("an unreadable content feed reports unavailable, not silence", async () => {
  stub({ feedFails: true });
  const r = await call("hlaw_whats_new", {});
  assert.equal(r.structuredContent.feed.health, "unavailable");
  assert.equal(r.structuredContent.feed.trust_silence, false);
  assert.match(r.structuredContent.feed.note, /No conclusion/);
});

test("the staleness threshold is configurable", async () => {
  stub({ status: { scanned_at: hoursAgo(40), content_updated_at: hoursAgo(40) } });
  const strict = await mcp.onRequestPost({
    request: new Request("https://horowitz.law/mcp", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call",
        params: { name: "hlaw_feed_status", arguments: {} } }),
    }), env: { MCP_STALE_HOURS: "100" },
  });
  const b = await strict.json();
  assert.equal(b.result.structuredContent.feed.health, "ok", "40h is fresh under a 100h threshold");
});

// ------------------------------------------------------------------ deltas

test("since returns only what moved after the cursor", async () => {
  stub();
  const r = await call("hlaw_whats_new", { since: "2026-07-01" });
  const ids = r.structuredContent.results.items.map((i) => i.cluster_id).sort();
  assert.deepEqual(ids, [111], "only the 2026-08-20 card is after 2026-07-01");
});

test("a treated 2023 opinion surfaces in a 2026 window -- the reason this exists", async () => {
  stub();
  const r = await call("hlaw_whats_new", { since: "2026-06-01" });
  const hit = r.structuredContent.results.items.find((i) => i.cluster_id === 222);
  assert.ok(hit, "the Aspen card must appear");
  assert.equal(hit.change, "treatment");
  assert.equal(hit.treatment, "negative");
  assert.equal(hit.date, "2023-04-13", "decided in 2023...");
  assert.equal(hit.changed, "2026-06-12", "...but it MOVED in 2026");
});

test("areas filter to the practice a skill covers", async () => {
  stub();
  const r = await call("hlaw_whats_new", { areas: ["coverage"] });
  const ids = r.structuredContent.results.items.map((i) => i.cluster_id);
  assert.deepEqual(ids, [222]);
  const multi = await call("hlaw_whats_new", { areas: ["auto"] });
  assert.deepEqual(multi.structuredContent.results.items.map((i) => i.cluster_id), [444],
    "a card matches on any of its areas");
});

test("kinds default to opinions; watches are opt-in", async () => {
  stub();
  const def = await call("hlaw_whats_new", {});
  assert.ok(!def.structuredContent.results.items.some((i) => i.change === "legislation"));
  const withLeg = await call("hlaw_whats_new", { kinds: ["legislation"] });
  assert.equal(withLeg.structuredContent.results.items.length, 1);
  const onlyTreat = await call("hlaw_whats_new", { kinds: ["treatment"] });
  assert.deepEqual(onlyTreat.structuredContent.results.items.map((i) => i.cluster_id), [222]);
});

test("results carry why and a source url, and hide bulk unless verbose", async () => {
  stub();
  const terse = await call("hlaw_whats_new", { areas: ["premises"] });
  const t = terse.structuredContent.results.items[0];
  assert.equal(t.why, "premises point", "why is what lets a model act without a second call");
  assert.equal(t.url, "https://cl/111");
  assert.equal(t.synopsis, undefined, "bulk is withheld by default");
  const full = await call("hlaw_whats_new", { areas: ["premises"], verbose: true });
  assert.equal(full.structuredContent.results.items[0].synopsis, "long text");
});

test("every delta response ships the coverage denominator", async () => {
  stub();
  const r = await call("hlaw_whats_new", {});
  assert.equal(r.structuredContent.coverage.premises, 1);
  assert.ok(Object.keys(r.structuredContent.coverage).length > 0);
});

test("pagination reports total, has_more and next_offset", async () => {
  stub();
  const p = await call("hlaw_whats_new", { limit: 2 });
  assert.equal(p.structuredContent.results.total, 3);
  assert.equal(p.structuredContent.results.count, 2);
  assert.equal(p.structuredContent.results.has_more, true);
  assert.equal(p.structuredContent.results.next_offset, 2);
  const last = await call("hlaw_whats_new", { limit: 2, offset: 2 });
  assert.equal(last.structuredContent.results.has_more, false);
  assert.equal(last.structuredContent.results.next_offset, null);
});

test("an absurd limit is clamped rather than honoured", async () => {
  stub();
  const r = await call("hlaw_whats_new", { limit: 100000 });
  assert.ok(r.structuredContent.results.count <= 100);
});

// ------------------------------------------------------------------ the canary tool

test("a flagged authority comes back flagged, with the later case", async () => {
  stub();
  const r = await call("hlaw_check_authorities", { cluster_ids: [222] });
  const a = r.structuredContent.authorities[0];
  assert.equal(a.status, "flagged");
  assert.equal(a.treatment, "negative");
  assert.equal(a.treated_by[0].name, "Later Case");
  assert.equal(r.structuredContent.flagged, 1);
});

test("an unflagged authority is NOT reported as cleared", async () => {
  // The most dangerous possible bug in this tool is implying clearance it has not earned.
  stub();
  const r = await call("hlaw_check_authorities", { cluster_ids: [111] });
  const a = r.structuredContent.authorities[0];
  assert.equal(a.status, "no_flag");
  assert.match(a.note, /NOT a clearance/);
  assert.match(r.structuredContent.disclaimer, /not a statement that a case remains good law/);
});

test("a case the feed does not track says so plainly", async () => {
  stub();
  const r = await call("hlaw_check_authorities", { cluster_ids: [999999] });
  assert.equal(r.structuredContent.authorities[0].status, "not_in_feed");
  assert.match(r.structuredContent.authorities[0].note, /not a clearance/i);
});

test("names match case-insensitively on a fragment", async () => {
  stub();
  const r = await call("hlaw_check_authorities", { names: ["aspen american"] });
  assert.equal(r.structuredContent.authorities[0].status, "flagged");
  const miss = await call("hlaw_check_authorities", { names: ["no such case"] });
  assert.equal(miss.structuredContent.authorities[0].status, "not_in_feed");
});

test("checking nothing is a usage answer, not an empty clearance", async () => {
  stub();
  const r = await call("hlaw_check_authorities", {});
  assert.equal(r.structuredContent.authorities.length, 0);
  assert.match(r.structuredContent.note, /No authorities supplied/);
});

test("the canary still answers when the feed is stale, but flags the answer", async () => {
  stub({ status: { scanned_at: hoursAgo(500), content_updated_at: hoursAgo(500) } });
  const r = await call("hlaw_check_authorities", { cluster_ids: [222] });
  assert.equal(r.structuredContent.authorities[0].status, "flagged");
  assert.equal(r.structuredContent.feed.trust_silence, false,
    "a stale feed cannot vouch for the absence of NEW flags");
});

// ------------------------------------------------------------------ status + formats

test("feed_status publishes the per-area denominator and the summary disclaimer", async () => {
  stub();
  const r = await call("hlaw_feed_status", {});
  assert.equal(r.structuredContent.counts.by_area.premises, 1);
  assert.match(r.structuredContent.coverage_note, /DENOMINATOR/);
  assert.match(r.structuredContent.content_disclaimer, /not primary law/);
});

test("markdown format is human-readable and still leads with health", async () => {
  stub();
  const r = await call("hlaw_whats_new", { response_format: "markdown" });
  const text = r.content[0].text;
  assert.match(text, /\*\*Feed:\*\* ok/);
  assert.match(text, /Trust an empty result: yes/);
  assert.match(text, /Alpha v\. Beta/);
});

test("markdown says NO when silence is untrustworthy", async () => {
  stub({ status: { scanned_at: hoursAgo(500), content_updated_at: hoursAgo(500) } });
  const r = await call("hlaw_whats_new", { since: "2026-08-29", response_format: "markdown" });
  assert.match(r.content[0].text, /Trust an empty result: NO/);
});

test("an unknown tool errors inside the result, listing what exists", async () => {
  stub();
  const r = await call("hlaw_nope", {});
  assert.equal(r.isError, true);
  assert.match(r.content[0].text, /Unknown tool/);
  assert.match(r.content[0].text, /hlaw_whats_new/);
});

test("both a text block and structuredContent come back", async () => {
  stub();
  const r = await call("hlaw_feed_status", {});
  assert.equal(r.content[0].type, "text");
  assert.ok(r.structuredContent, "clients that can read structured output should not have to parse");
});
