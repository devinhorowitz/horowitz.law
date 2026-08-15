// functions/api/subscribe/confirm.js
// GET  /api/subscribe/confirm?e=<email>&t=<ts>&a=<areas>&s=<hmac>   -> verify the link, show a Confirm button
// POST /api/subscribe/confirm                              -> perform the subscription
//
// Two steps on purpose. Mail-security link scanners (Outlook SafeLinks and friends)
// follow GET links in inbound mail; a GET that subscribed on sight let a scanner
// confirm an address whose owner never clicked. The GET now only verifies the signed
// link and renders a button; the state change happens on the POST the button submits
// (a plain form, so no JavaScript is required). Links from earlier emails still work;
// they just show the button instead of confirming instantly.
//
// Verifies the signed link from the confirmation email, then creates the contact in
// Resend's global contacts: subscribed (unsubscribed: false), opted into the Topic,
// and added to the Segment. Links are valid for 48 hours and cannot be forged without
// SUBSCRIBE_SECRET. Resend's model: Contacts are global; a Segment is for internal
// targeting (broadcasts require a segment_id); a Topic carries the user-facing
// unsubscribe preference.
//
// Required Cloudflare Pages environment variables:
//   RESEND_API_KEY     Resend API key with contacts access
//   SUBSCRIBE_SECRET   the same secret used by functions/api/subscribe/index.js
// Recommended (set both so phase-two broadcasts can target + scope correctly):
//   RESEND_SEGMENT_ID  the Segment ID confirmed subscribers are added to
//   RESEND_TOPIC_ID    the Topic ID confirmed subscribers are opted into
// Optional:
//   RESEND_LEGISLATION_SEGMENT_ID  Segment for the Legislative & Regulatory Watch
//   RESEND_LEGISLATION_TOPIC_ID    Topic scoping that email's unsubscribe
//   SITE_URL           site origin, only used for links on the result page

const RESEND = "https://api.resend.com";
const UA = "horowitz.law-subscribe/1.0 (+https://horowitz.law)";
const CONFIRM_TTL_MS = 48 * 60 * 60 * 1000; // 48 hours -- ample for opt-in, and it bounds how
                                            // long a (stateless, non-single-use) confirm link
                                            // stays replayable if it leaks or is forwarded.

async function hmacHex(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// HTML-escape the one piece of user input these pages reflect (the email address).
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function resendHeaders(env) {
  return {
    Authorization: `Bearer ${env.RESEND_API_KEY}`,
    "Content-Type": "application/json",
    Accept: "application/json",
    "User-Agent": UA, // Cloudflare in front of api.resend.com blocks default library agents (error 1010)
  };
}

// Thin fetch wrapper: JSON-encodes the body when one is given, and bounds every
// Resend call with a timeout so a stalled api.resend.com cannot hang the Worker
// mid-mutation (the abort rejects, which the callers surface as a 500 page).
function rfetch(env, method, path, body) {
  const init = { method, headers: resendHeaders(env) };
  if (body !== null && body !== undefined) init.body = JSON.stringify(body);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  init.signal = ctrl.signal;
  return fetch(RESEND + path, init).finally(() => clearTimeout(timer));
}

// Practice-area choices ride the confirm link as a sanitized CSV (covered by the
// HMAC). Re-sanitize on receipt anyway: shape, token charset, count.
function parseAreas(csv) {
  if (typeof csv !== "string" || !csv || csv.length > 140) return [];
  const parts = csv.split(",").filter((a) => /^[a-z]{2,12}$/.test(a) && a !== "all");
  return [...new Set(parts)].sort().slice(0, 10);
}

// RESEND_AREA_TOPICS maps area code -> Resend Topic id, e.g.
//   {"coverage":"top_...","premises":"top_..."}
// Set it as a Cloudflare Pages environment variable. Absent, malformed, or
// missing a code: those choices fall back to the main Topic below, so a
// subscriber always lands on something rather than silently on nothing.
function areaTopicMap(env) {
  try {
    const m = JSON.parse(env.RESEND_AREA_TOPICS || "{}");
    return m && typeof m === "object" && !Array.isArray(m) ? m : {};
  } catch {
    return {};
  }
}

// The "legislation & regulations" checkbox rides the same `area` field as the practice
// areas (public/subscribe.html), but it is NOT one of them: the Legislative & Regulatory
// Watch is a separate weekly broadcast with its own Segment and Topic, so it is handled
// apart from areaTopicMap. Two things follow, and both were wrong in the version that
// only mapped it as another area topic:
//
//   1. A broadcast is addressed to a SEGMENT. digest.py sends the legislation email to
//      RESEND_LEGISLATION_SEGMENT_ID, so opting a subscriber into the legislation Topic
//      while leaving them out of that Segment delivers nothing -- while looking wired.
//      Ticking the box has to put them in the Segment.
//   2. Legislation is ADDITIVE, never narrowing. Treated as an area topic it would
//      consume the narrowing branch below, so someone who ticked only this box would be
//      opted into the legislation Topic and OUT of the opinions one -- breaking the
//      "a choice can narrow what arrives but never strand a confirmed subscriber" rule.
const LEGISLATION_AREA = "legislation";

// Create the global contact subscribed, opted into the Topic, and in the Segment.
// New contact: a single POST applies segments + topics inline. Existing contact: the
// POST returns non-2xx (or upserts), so we follow with idempotent updates.
async function subscribeConfirmed(env, email, areas) {
  const seg = env.RESEND_SEGMENT_ID;
  const top = env.RESEND_TOPIC_ID;
  const legSeg = env.RESEND_LEGISLATION_SEGMENT_ID;
  const legTop = env.RESEND_LEGISLATION_TOPIC_ID;

  const chose = (areas || []).includes(LEGISLATION_AREA);
  const wantsLegislation = chose && Boolean(legSeg);   // unconfigured: behave as before

  // The topic set this confirmation opts into: the chosen areas' topics where
  // mapped, the main topic when nothing was chosen or nothing mapped. A choice
  // can narrow what arrives but never strand a confirmed subscriber.
  const map = areaTopicMap(env);
  const practice = (areas || []).filter((a) => a !== LEGISLATION_AREA);
  const areaTopics = practice.map((a) => map[a]).filter(Boolean);
  const topicIds = areaTopics.length ? [...new Set(areaTopics)] : (top ? [top] : []);
  if (wantsLegislation && legTop && !topicIds.includes(legTop)) topicIds.push(legTop);

  const segmentIds = [...new Set([seg, wantsLegislation ? legSeg : null].filter(Boolean))];

  const createBody = { email, unsubscribed: false };
  if (segmentIds.length) createBody.segments = segmentIds.map((id) => ({ id }));
  if (topicIds.length) createBody.topics = topicIds.map((id) => ({ id, subscription: "opt_in" }));

  const created = await rfetch(env, "POST", "/contacts", createBody);
  if (created.ok) return; // new contact: segment + topic already applied inline

  // Contact already exists (or create was rejected). Ensure the end state another way.
  const path = `/contacts/${encodeURIComponent(email)}`;

  const patched = await rfetch(env, "PATCH", path, { unsubscribed: false });
  if (!patched.ok) {
    throw new Error(`confirm: create ${created.status}, patch ${patched.status}`);
  }

  if (topicIds.length) {
    // Raw endpoint takes a bare array body.
    const t = await rfetch(env, "PATCH", `${path}/topics`,
      topicIds.map((id) => ({ id, subscription: "opt_in" })));
    if (!t.ok) throw new Error(`confirm: topics ${t.status}`);
  }

  for (const id of segmentIds) {
    const s = await rfetch(env, "POST", `${path}/segments/${id}`, null);
    // Adding a contact already in the segment may return a 4xx; only treat 5xx as fatal.
    if (!s.ok && s.status >= 500) throw new Error(`confirm: segment ${s.status}`);
  }
}

function page(title, bodyHtml) {
  return (
    `<!doctype html><html lang="en"><head><meta charset="utf-8">` +
    `<meta name="viewport" content="width=device-width,initial-scale=1">` +
    `<title>${title} · Georgia Appellate Watch</title>` +
    `<style>` +
    `body{font-family:'JetBrains Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;background:#f5ede0;color:#1a1a1a;margin:0;padding:72px 24px;line-height:1.7;}` +
    `@media (prefers-color-scheme: dark){body{background:#0d0e10;color:#e8e3d8;}}` +
    `.card{max-width:540px;margin:0 auto;}` +
    `h1{font-size:25px;font-weight:500;letter-spacing:-0.02em;line-height:1.2;margin:0 0 14px;}` +
    `p{margin:0 0 12px;font-size:15px;}` +
    `.muted{color:#6a6560;font-size:13.5px;}` +
    `a{color:#a4471a;text-decoration:none;border-bottom:1px dotted currentColor;}` +
    `button{display:inline-block;background:#a4471a;color:#f5ede0;border:0;border-radius:8px;` +
    `padding:13px 24px;font:600 14px ui-monospace,Menlo,Consolas,monospace;cursor:pointer;}` +
    `@media (prefers-color-scheme: dark){a{color:#ff9e5e;}.muted{color:#807a72;}` +
    `button{background:#ff9e5e;color:#0d0e10;}}` +
    `</style></head><body><div class="card">${bodyHtml}</div></body></html>`
  );
}

function html(title, bodyHtml, status = 200) {
  return new Response(page(title, bodyHtml), {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      // The confirm URL carries the signed token; keep it out of caches and out of any Referer.
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
      // Functions responses bypass the static _headers file, so set a page CSP here.
      // These pages run no scripts; the only inline material is the style block above
      // and the confirm form, which must be able to POST back to this origin.
      "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; " +
        "base-uri 'none'; frame-ancestors 'none'",
      "X-Frame-Options": "DENY",
    },
  });
}

// Shared validation for GET and POST: config, shape, signature, expiry.
// Returns { email } on success, or { error: Response } to return as-is.
async function validateLink(env, email, ts, sig, areasCsv) {
  if (!env.RESEND_API_KEY || !env.SUBSCRIBE_SECRET) {
    return { error: html(
      "Not configured",
      `<h1>Not configured</h1><p class="muted">Subscription confirmation is not set up yet.</p>`,
      500
    ) };
  }
  if (!email || !ts || !sig || email.length > 254 || !/^[0-9a-f]{64}$/.test(sig)) {
    return { error: html(
      "Invalid link",
      `<h1>Invalid link</h1><p class="muted">This confirmation link is missing or malformed. <a href="/subscribe">Try subscribing again</a>.</p>`,
      400
    ) };
  }
  // Pre-areas links signed `email.ts`; current links with choices sign
  // `email.ts.areas`. Verify whichever shape this link carries.
  const msg = areasCsv ? `${email}.${ts}.${areasCsv}` : `${email}.${ts}`;
  const expected = await hmacHex(env.SUBSCRIBE_SECRET, msg);
  if (!timingSafeEqual(expected, sig)) {
    return { error: html(
      "Invalid link",
      `<h1>Invalid link</h1><p class="muted">This confirmation link could not be verified. <a href="/subscribe">Try subscribing again</a>.</p>`,
      400
    ) };
  }
  if (!/^\d+$/.test(ts) || Date.now() - Number(ts) > CONFIRM_TTL_MS) {
    return { error: html(
      "Link expired",
      `<h1>Link expired</h1><p class="muted">This confirmation link has expired. <a href="/subscribe">Subscribe again</a> to get a fresh one.</p>`,
      410
    ) };
  }
  return { email };
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const email = (url.searchParams.get("e") || "").trim().toLowerCase();
  const ts = url.searchParams.get("t") || "";
  const sig = url.searchParams.get("s") || "";
  const areas = parseAreas(url.searchParams.get("a") || "");
  const areasCsv = areas.join(",");

  const v = await validateLink(env, email, ts, sig, areasCsv);
  if (v.error) return v.error;

  // Valid link: render the confirm step. No state changes on GET, so a link
  // scanner that prefetches this URL subscribes nobody.
  return html(
    "Confirm subscription",
    `<h1>One click to finish.</h1>` +
      `<p>Confirm the Georgia Appellate Watch weekly digest for <strong>${escapeHtml(email)}</strong>.</p>` +
      (areasCsv
        ? `<p class="muted">Areas chosen: ${escapeHtml(areas.join(", "))}.</p>`
        : "") +
      `<form method="post" action="/api/subscribe/confirm">` +
      `<input type="hidden" name="e" value="${escapeHtml(email)}">` +
      `<input type="hidden" name="t" value="${escapeHtml(ts)}">` +
      `<input type="hidden" name="a" value="${escapeHtml(areasCsv)}">` +
      `<input type="hidden" name="s" value="${escapeHtml(sig)}">` +
      `<p style="margin:18px 0 10px;"><button type="submit">Confirm subscription</button></p>` +
      `</form>` +
      `<p class="muted">Not you, or changed your mind? Just close this page; nothing is saved.</p>`
  );
}

export async function onRequestPost(context) {
  const { request, env } = context;
  let form;
  try {
    form = await request.formData();
  } catch {
    return html(
      "Invalid request",
      `<h1>Invalid request</h1><p class="muted">Use the button on the confirmation page. <a href="/subscribe">Try subscribing again</a>.</p>`,
      400
    );
  }
  const email = String(form.get("e") || "").trim().toLowerCase();
  const ts = String(form.get("t") || "");
  const sig = String(form.get("s") || "");
  const areas = parseAreas(String(form.get("a") || ""));
  const areasCsv = areas.join(",");

  const v = await validateLink(env, email, ts, sig, areasCsv);
  if (v.error) return v.error;

  try {
    await subscribeConfirmed(env, email, areas);
  } catch (e) {
    return html(
      "Something went wrong",
      `<h1>Something went wrong</h1><p class="muted">We could not confirm your subscription just now. Please <a href="/subscribe">try again</a>.</p>`,
      500
    );
  }

  return html(
    "Subscribed",
    `<h1>You are subscribed.</h1>` +
      `<p>You will receive the Georgia Appellate Watch digest in weeks with new decisions.</p>` +
      `<p class="muted"><a href="/opinions">Browse the latest decisions</a></p>`
  );
}
