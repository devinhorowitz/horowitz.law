// Hermetic tests for the Cloudflare Pages Functions (functions/api/subscribe/).
//
// These 598 lines are the only user-facing, secret-handling code in the repo: they verify
// Turnstile, rate-limit by IP and by destination address, sign and verify the HMAC confirmation
// link, and write subscribers into Resend. Until now CI checked their SYNTAX (node --check) and
// nothing else. A behavioral break here is invisible in the pipeline's own tests and shows up as
// real people failing to subscribe, or as an open email relay pointed at our sending domain.
//
// No dependencies and no package.json: the runner is node:test (built in), and each Function is
// loaded by reading the file and importing it as a data: URL module. That matters twice over --
// it adds nothing to the supply chain, and it keeps test files out of functions/, where Cloudflare
// derives its routes from the file tree.
//
// Every network call is stubbed. globalThis.fetch is replaced per test with a recorder, so a test
// that expects "no email was sent" asserts on an empty call log rather than on a return value.
//
// Run: node --test scripts/test_functions.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

async function load(rel) {
  const src = fs.readFileSync(path.join(REPO, rel), "utf8");
  return import("data:text/javascript," + encodeURIComponent(src));
}
const subscribe = await load("functions/api/subscribe/index.js");
const confirm = await load("functions/api/subscribe/confirm.js");

const SECRET = "test-secret-not-a-real-one";
const SITE = "https://horowitz.law";

function baseEnv(over = {}) {
  return {
    RESEND_API_KEY: "re_test_key",
    SUBSCRIBE_SECRET: SECRET,
    TURNSTILE_SECRET_KEY: "ts_test_secret",
    RESEND_SEGMENT_ID: "seg_123",
    RESEND_TOPIC_ID: "top_main",
    SITE_URL: SITE,
    ...over,
  };
}

// --- fetch recorder -------------------------------------------------------
// handler(url, init) -> Response | undefined. Undefined means "unexpected call", which fails the
// test loudly rather than letting a Function silently proceed on a stubbed-away error.
function stubFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    calls.push({ url: u, init, body: init.body });
    const r = await handler(u, init);
    if (r === undefined) throw new Error("unstubbed fetch to " + u);
    return r;
  };
  return calls;
}
const jsonResponse = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });

// Turnstile verdict helper: a genuine success carries hostname + action.
const turnstileOK = (over = {}) =>
  jsonResponse({ success: true, hostname: "horowitz.law", action: "subscribe", ...over });

function postRequest(body, { url = SITE + "/api/subscribe", headers = {}, raw = null } = {}) {
  return new Request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: raw === null ? JSON.stringify(body) : raw,
  });
}

const call = (mod, fn, request, env) => mod[fn]({ request, env, waitUntil() {} });

// =========================================================================
// POST /api/subscribe -- configuration and request-shape gates
// =========================================================================
test("missing Resend config fails closed with 500", async () => {
  const calls = stubFetch(() => undefined);
  for (const missing of ["RESEND_API_KEY", "SUBSCRIBE_SECRET"]) {
    const env = baseEnv({ [missing]: "" });
    const r = await call(subscribe, "onRequestPost", postRequest({ email: "a@b.co" }), env);
    assert.equal(r.status, 500, missing);
    assert.match((await r.json()).message, /not configured/i);
  }
  assert.equal(calls.length, 0, "must not touch the network when unconfigured");
});

test("missing Turnstile secret fails closed with 500", async () => {
  stubFetch(() => undefined);
  const r = await call(subscribe, "onRequestPost", postRequest({ email: "a@b.co" }),
                       baseEnv({ TURNSTILE_SECRET_KEY: "" }));
  assert.equal(r.status, 500);
  assert.match((await r.json()).message, /Verification is not configured/i);
});

test("cross-origin POST is rejected, same-origin passes the check", async () => {
  const calls = stubFetch(() => undefined);
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "a@b.co" }, { headers: { Origin: "https://evil.example" } }), baseEnv());
  assert.equal(r.status, 403);
  assert.equal(calls.length, 0, "a cross-site POST must not reach Turnstile or Resend");
});

test("non-JSON content type is 415 and an oversized body is 413", async () => {
  stubFetch(() => undefined);
  const env = baseEnv();
  const form = await call(subscribe, "onRequestPost",
    postRequest({}, { headers: { "Content-Type": "application/x-www-form-urlencoded" } }), env);
  assert.equal(form.status, 415);
  const big = await call(subscribe, "onRequestPost",
    postRequest(null, { raw: "x".repeat(8193) }), env);
  assert.equal(big.status, 413);
});

test("malformed JSON is 400", async () => {
  stubFetch(() => undefined);
  const r = await call(subscribe, "onRequestPost", postRequest(null, { raw: "{not json" }), baseEnv());
  assert.equal(r.status, 400);
});

// =========================================================================
// The bot trap and the address gate
// =========================================================================
test("a filled honeypot looks successful but sends nothing", async () => {
  const calls = stubFetch(() => undefined);
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "a@b.co", company: "Acme", turnstileToken: "tok" }), baseEnv());
  assert.equal(r.status, 200);
  assert.equal((await r.json()).ok, true, "the bot must not learn it was caught");
  assert.equal(calls.length, 0, "no Turnstile call and no email may be spent on a honeypot hit");
});

test("an invalid address is 422 before any verification spend", async () => {
  const calls = stubFetch(() => undefined);
  for (const bad of ["", "nope", "a@b", "a b@c.co", "x".repeat(250) + "@b.co"]) {
    const r = await call(subscribe, "onRequestPost",
      postRequest({ email: bad, turnstileToken: "tok" }), baseEnv());
    assert.equal(r.status, 422, JSON.stringify(bad));
  }
  assert.equal(calls.length, 0);
});

// =========================================================================
// Turnstile: fails closed, and is bound to this host and this form
// =========================================================================
test("a missing token is rejected without calling Turnstile", async () => {
  const calls = stubFetch(() => undefined);
  const r = await call(subscribe, "onRequestPost", postRequest({ email: "a@b.co" }), baseEnv());
  assert.equal(r.status, 400);
  assert.equal(calls.length, 0);
});

test("Turnstile fails CLOSED on every non-success shape", async () => {
  const cases = {
    "success:false": () => jsonResponse({ success: false }),
    "network error": () => { throw new Error("boom"); },
    "wrong hostname": () => turnstileOK({ hostname: "evil.example" }),
    "wrong action": () => turnstileOK({ action: "login" }),
    "missing hostname": () => jsonResponse({ success: true, action: "subscribe" }),
    "missing action": () => jsonResponse({ success: true, hostname: "horowitz.law" }),
    "empty body": () => jsonResponse(null),
  };
  for (const [name, make] of Object.entries(cases)) {
    const calls = stubFetch((u) => {
      if (u.includes("siteverify")) return make();
      return undefined; // any Resend call here is a test failure
    });
    const r = await call(subscribe, "onRequestPost",
      postRequest({ email: "a@b.co", turnstileToken: "tok" }), baseEnv());
    assert.equal(r.status, 403, name);
    assert.equal(calls.filter((c) => c.url.includes("resend")).length, 0,
                 name + ": no email may be sent on a failed verification");
  }
});

// =========================================================================
// Rate limiting, including the ordering property the code documents
// =========================================================================
function limiter(decide) {
  const keys = [];
  return { keys, binding: { limit: async ({ key }) => { keys.push(key); return { success: decide(key) }; } } };
}

test("the IP limit rejects with 429 before Turnstile is consulted", async () => {
  const calls = stubFetch(() => undefined);
  const { keys, binding } = limiter(() => false);
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "a@b.co", turnstileToken: "tok" },
                { headers: { "CF-Connecting-IP": "203.0.113.9" } }),
    baseEnv({ SUBSCRIBE_RATELIMIT: binding }));
  assert.equal(r.status, 429);
  assert.deepEqual(keys, ["203.0.113.9"]);
  assert.equal(calls.length, 0);
});

test("a failed Turnstile cannot spend the victim address's rate budget", async () => {
  // The documented ordering property: if the email-keyed limit ran first, an attacker could
  // exhaust a chosen address's bucket with forged tokens and lock the real owner out, without
  // ever solving a challenge.
  stubFetch((u) => (u.includes("siteverify") ? jsonResponse({ success: false }) : undefined));
  const { keys, binding } = limiter(() => true);
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "victim@example.com", turnstileToken: "forged" },
                { headers: { "CF-Connecting-IP": "203.0.113.9" } }),
    baseEnv({ SUBSCRIBE_RATELIMIT: binding }));
  assert.equal(r.status, 403);
  assert.deepEqual(keys, ["203.0.113.9"], "the email-keyed bucket must NOT be touched");
  assert.ok(!keys.some((k) => k.startsWith("email:")));
});

test("the email-keyed limit applies after a good Turnstile, and blocks the send", async () => {
  const calls = stubFetch((u) => (u.includes("siteverify") ? turnstileOK() : undefined));
  const { keys, binding } = limiter((k) => !k.startsWith("email:"));
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "a@b.co", turnstileToken: "tok" }),
    baseEnv({ SUBSCRIBE_RATELIMIT: binding }));
  assert.equal(r.status, 429);
  assert.ok(keys.includes("email:a@b.co"));
  assert.equal(calls.filter((c) => c.url.includes("resend")).length, 0);
});

test("a limiter that throws never blocks a legitimate subscriber", async () => {
  const calls = stubFetch((u) =>
    u.includes("siteverify") ? turnstileOK() : jsonResponse({ id: "email_1" }));
  const binding = { limit: async () => { throw new Error("limiter down"); } };
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "a@b.co", turnstileToken: "tok" }),
    baseEnv({ SUBSCRIBE_RATELIMIT: binding }));
  assert.equal(r.status, 200);
  assert.equal(calls.filter((c) => c.url.endsWith("/emails")).length, 1);
});

// =========================================================================
// The happy path, the signed link, and area sanitization
// =========================================================================
function sentLink(calls) {
  const send = calls.find((c) => c.url.endsWith("/emails"));
  assert.ok(send, "a confirmation email should have been sent");
  const body = JSON.parse(send.body);
  const m = body.text.match(/https:\/\/\S+/);
  assert.ok(m, "the email should carry a confirmation link");
  return { link: m[0], body };
}

test("a verified request sends exactly one confirmation email with a signed link", async () => {
  const calls = stubFetch((u) =>
    u.includes("siteverify") ? turnstileOK() : jsonResponse({ id: "email_1" }));
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: " A@B.co ", turnstileToken: "tok" }), baseEnv());
  assert.equal(r.status, 200);
  const { link, body } = sentLink(calls);
  assert.equal(calls.filter((c) => c.url.endsWith("/emails")).length, 1, "exactly one send");
  assert.deepEqual(body.to, ["a@b.co"], "the address is trimmed and lowercased");
  const u = new URL(link);
  assert.equal(u.origin, SITE);
  assert.equal(u.searchParams.get("e"), "a@b.co");
  assert.match(u.searchParams.get("s"), /^[0-9a-f]{64}$/, "a hex HMAC signature");
  assert.ok(!link.includes(SECRET), "the secret must never appear in the link");
  assert.ok(!JSON.stringify(body).includes(SECRET));
});

test("area choices are sanitized, deduped, sorted and capped into the link", async () => {
  const calls = stubFetch((u) =>
    u.includes("siteverify") ? turnstileOK() : jsonResponse({ id: "e" }));
  await call(subscribe, "onRequestPost", postRequest({
    email: "a@b.co", turnstileToken: "tok",
    areas: ["premises", "coverage", "premises", "all", "BADCASE", "toolongareacode", "x", 7, null,
            "aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh", "ii", "jj"],
  }), baseEnv());
  const { link } = sentLink(calls);
  const areas = new URL(link).searchParams.get("a").split(",");
  assert.ok(!areas.includes("all"), "'all' is not an area");
  assert.ok(!areas.includes("BADCASE") && !areas.includes("toolongareacode") && !areas.includes("x"));
  assert.equal(new Set(areas).size, areas.length, "deduped");
  assert.deepEqual(areas, [...areas].sort(), "sorted, so the signed CSV is canonical");
  assert.ok(areas.length <= 10, "capped at 10");
});

test("no areas means no a= parameter at all", async () => {
  const calls = stubFetch((u) => (u.includes("siteverify") ? turnstileOK() : jsonResponse({})));
  await call(subscribe, "onRequestPost", postRequest({ email: "a@b.co", turnstileToken: "tok" }), baseEnv());
  assert.equal(new URL(sentLink(calls).link).searchParams.get("a"), null);
});

test("a trailing slash on SITE_URL does not produce a double slash", async () => {
  const calls = stubFetch((u) => (u.includes("siteverify") ? turnstileOK() : jsonResponse({})));
  await call(subscribe, "onRequestPost", postRequest({ email: "a@b.co", turnstileToken: "tok" }),
             baseEnv({ SITE_URL: "https://horowitz.law///" }));
  assert.ok(sentLink(calls).link.startsWith(SITE + "/api/subscribe/confirm?"));
});

test("a Resend failure surfaces as 500, never 502 (Cloudflare would eat a 502)", async () => {
  stubFetch((u) => (u.includes("siteverify") ? turnstileOK() : jsonResponse({ error: "nope" }, 422)));
  const r = await call(subscribe, "onRequestPost",
    postRequest({ email: "a@b.co", turnstileToken: "tok" }), baseEnv());
  assert.equal(r.status, 500);
  assert.equal((await r.json()).ok, false);
});

test("GET /api/subscribe is 405", async () => {
  const r = await subscribe.onRequestGet();
  assert.equal(r.status, 405);
});

// =========================================================================
// GET /api/subscribe/confirm -- verifies, and MUST NOT mutate
// =========================================================================
async function validLink(email = "a@b.co", areas = []) {
  const calls = stubFetch((u) => (u.includes("siteverify") ? turnstileOK() : jsonResponse({})));
  await call(subscribe, "onRequestPost",
    postRequest({ email, turnstileToken: "tok", areas }), baseEnv());
  return sentLink(calls).link;
}
const getConfirm = (link, env = baseEnv()) =>
  call(confirm, "onRequestGet", new Request(link), env);

function formPost(link, over = {}) {
  const u = new URL(link);
  const fd = new FormData();
  fd.set("e", over.e ?? u.searchParams.get("e"));
  fd.set("t", over.t ?? u.searchParams.get("t"));
  fd.set("a", over.a ?? (u.searchParams.get("a") || ""));
  fd.set("s", over.s ?? u.searchParams.get("s"));
  return new Request(SITE + "/api/subscribe/confirm", { method: "POST", body: fd });
}

test("GET on a valid link renders the button and changes NOTHING", async () => {
  // The whole reason the flow is two-step: Outlook SafeLinks and friends prefetch GET links in
  // inbound mail. A GET that subscribed on sight let a scanner confirm an address for someone
  // who never clicked.
  const link = await validLink();
  const calls = stubFetch(() => undefined);
  const r = await getConfirm(link);
  assert.equal(r.status, 200);
  const body = await r.text();
  assert.match(body, /<form method="post"/);
  assert.equal(calls.length, 0, "a GET must make no Resend call whatsoever");
});

test("the confirm page sets a restrictive CSP and is uncacheable", async () => {
  const link = await validLink();
  stubFetch(() => undefined);
  const r = await getConfirm(link);
  assert.match(r.headers.get("Content-Security-Policy"), /default-src 'none'/);
  assert.match(r.headers.get("Content-Security-Policy"), /form-action 'self'/);
  assert.equal(r.headers.get("Cache-Control"), "no-store");
  assert.equal(r.headers.get("Referrer-Policy"), "no-referrer");
  assert.equal(r.headers.get("X-Frame-Options"), "DENY");
});

test("a tampered or malformed link is refused", async () => {
  const link = await validLink();
  stubFetch(() => undefined);
  const u = new URL(link);
  const good = u.searchParams.get("s");
  const flipped = (good[0] === "a" ? "b" : "a") + good.slice(1);

  const cases = [
    ["tampered signature", { s: flipped }, 400],
    ["short signature", { s: "abc" }, 400],
    ["non-hex signature", { s: "z".repeat(64) }, 400],
    ["different address, same signature", { e: "attacker@evil.example" }, 400],
    ["missing signature", { s: "" }, 400],
    ["altered areas", { a: "premises" }, 400],
  ];
  for (const [name, over, want] of cases) {
    const u2 = new URL(link);
    for (const [k, v] of Object.entries(over)) u2.searchParams.set(k, v);
    const r = await getConfirm(u2.toString());
    assert.equal(r.status, want, name);
  }
});

test("an expired link is 410", async () => {
  const link = await validLink();
  stubFetch(() => undefined);
  const u = new URL(link);
  // Re-sign with a timestamp 49 hours old: a genuine but stale link, not a forgery.
  const old = String(Date.now() - 49 * 3600 * 1000);
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key,
    new TextEncoder().encode(`${u.searchParams.get("e")}.${old}`));
  u.searchParams.set("t", old);
  u.searchParams.set("s", [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join(""));
  const r = await getConfirm(u.toString());
  assert.equal(r.status, 410);
});

test("the reflected address is HTML-escaped", async () => {
  // The address is the one piece of user input these pages echo back.
  const link = await validLink("a\"><script>x</script>@b.co");
  stubFetch(() => undefined);
  const body = await (await getConfirm(link)).text();
  assert.ok(!body.includes("<script>x</script>"), "raw script tag must not survive");
  assert.match(body, /&lt;script&gt;/);
});

// =========================================================================
// POST /api/subscribe/confirm -- the state change
// =========================================================================
test("confirming a new contact creates it subscribed, in the segment and topic", async () => {
  const link = await validLink();
  const calls = stubFetch((u, init) =>
    u.endsWith("/contacts") && init.method === "POST" ? jsonResponse({ id: "c1" }, 201) : undefined);
  const r = await call(confirm, "onRequestPost", formPost(link), baseEnv());
  assert.equal(r.status, 200);
  assert.match(await r.text(), /You are subscribed/);
  assert.equal(calls.length, 1, "a new contact takes exactly one call");
  const body = JSON.parse(calls[0].body);
  assert.equal(body.email, "a@b.co");
  assert.equal(body.unsubscribed, false);
  assert.deepEqual(body.segments, [{ id: "seg_123" }]);
  assert.deepEqual(body.topics, [{ id: "top_main", subscription: "opt_in" }]);
});

test("an existing contact is repaired via PATCH, topics and segment", async () => {
  const link = await validLink();
  const calls = stubFetch((u, init) => {
    if (u.endsWith("/contacts") && init.method === "POST") return jsonResponse({ error: "exists" }, 409);
    if (init.method === "PATCH") return jsonResponse({ ok: true });
    if (init.method === "POST") return jsonResponse({ ok: true });   // segment add
    return undefined;
  });
  const r = await call(confirm, "onRequestPost", formPost(link), baseEnv());
  assert.equal(r.status, 200);
  const patched = calls.find((c) => c.init.method === "PATCH" && !c.url.endsWith("/topics"));
  assert.equal(JSON.parse(patched.body).unsubscribed, false, "resubscribes an unsubscribed contact");
  assert.ok(calls.some((c) => c.url.endsWith("/topics")), "re-opts into the topic");
  assert.ok(calls.some((c) => c.url.includes("/segments/seg_123")), "re-adds to the segment");
});

test("a 4xx from the segment add is tolerated but a 5xx fails the confirmation", async () => {
  const link = await validLink();
  const make = (segStatus) => (u, init) => {
    if (u.endsWith("/contacts") && init.method === "POST") return jsonResponse({}, 409);
    if (u.includes("/segments/")) return jsonResponse({}, segStatus);
    return jsonResponse({ ok: true });
  };
  stubFetch(make(422));
  assert.equal((await call(confirm, "onRequestPost", formPost(link), baseEnv())).status, 200,
               "already-in-segment must not fail the subscriber");
  stubFetch(make(503));
  assert.equal((await call(confirm, "onRequestPost", formPost(link), baseEnv())).status, 500);
});

test("area choices map to their own topics, falling back to the main topic", async () => {
  const link = await validLink("a@b.co", ["coverage", "premises"]);
  const env = baseEnv({ RESEND_AREA_TOPICS: JSON.stringify({ coverage: "top_cov" }) });
  const calls = stubFetch(() => jsonResponse({ id: "c" }, 201));
  await call(confirm, "onRequestPost", formPost(link), env);
  const topics = JSON.parse(calls[0].body).topics.map((t) => t.id);
  assert.deepEqual(topics, ["top_cov"], "a mapped area narrows to its own topic");

  const calls2 = stubFetch(() => jsonResponse({ id: "c" }, 201));
  await call(confirm, "onRequestPost", formPost(link),
             baseEnv({ RESEND_AREA_TOPICS: "{not json" }));
  assert.deepEqual(JSON.parse(calls2[0].body).topics.map((t) => t.id), ["top_main"],
                   "a malformed map must never strand a confirmed subscriber");
});

test("a tampered POST makes no Resend call", async () => {
  const link = await validLink();
  const calls = stubFetch(() => undefined);
  const r = await call(confirm, "onRequestPost",
    formPost(link, { e: "attacker@evil.example" }), baseEnv());
  assert.equal(r.status, 400);
  assert.equal(calls.length, 0);
});

test("end to end: the link index.js signs is the link confirm.js accepts", async () => {
  // The two Functions share only SUBSCRIBE_SECRET and a message format. This is the test that
  // would catch one of them changing that format without the other.
  const link = await validLink("round@trip.co", ["coverage", "damages"]);
  const calls = stubFetch(() => jsonResponse({ id: "c" }, 201));
  const r = await call(confirm, "onRequestPost", formPost(link), baseEnv());
  assert.equal(r.status, 200);
  assert.equal(JSON.parse(calls[0].body).email, "round@trip.co");
});
