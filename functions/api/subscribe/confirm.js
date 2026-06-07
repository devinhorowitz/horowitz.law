// functions/api/subscribe/confirm.js
// GET /api/subscribe/confirm?e=<email>&t=<ts>&s=<hmac>
//
// Verifies the signed link from the confirmation email, then creates the contact in
// Resend's global contacts: subscribed (unsubscribed: false), opted into the Topic,
// and added to the Segment. Links are valid for 7 days and cannot be forged without
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
//   SITE_URL           site origin, only used for links on the result page

const RESEND = "https://api.resend.com";
const UA = "horowitz.law-subscribe/1.0 (+https://horowitz.law)";
const CONFIRM_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

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

function resendHeaders(env) {
  return {
    Authorization: `Bearer ${env.RESEND_API_KEY}`,
    "Content-Type": "application/json",
    Accept: "application/json",
    "User-Agent": UA, // Cloudflare in front of api.resend.com blocks default library agents (error 1010)
  };
}

// Thin fetch wrapper: JSON-encodes the body when one is given.
function rfetch(env, method, path, body) {
  const init = { method, headers: resendHeaders(env) };
  if (body !== null && body !== undefined) init.body = JSON.stringify(body);
  return fetch(RESEND + path, init);
}

// Create the global contact subscribed, opted into the Topic, and in the Segment.
// New contact: a single POST applies segment + topic inline. Existing contact: the
// POST returns non-2xx (or upserts), so we follow with idempotent updates.
async function subscribeConfirmed(env, email) {
  const seg = env.RESEND_SEGMENT_ID;
  const top = env.RESEND_TOPIC_ID;

  const createBody = { email, unsubscribed: false };
  if (seg) createBody.segments = [{ id: seg }];
  if (top) createBody.topics = [{ id: top, subscription: "opt_in" }];

  const created = await rfetch(env, "POST", "/contacts", createBody);
  if (created.ok) return; // new contact: segment + topic already applied inline

  // Contact already exists (or create was rejected). Ensure the end state another way.
  const path = `/contacts/${encodeURIComponent(email)}`;

  const patched = await rfetch(env, "PATCH", path, { unsubscribed: false });
  if (!patched.ok) {
    throw new Error(`confirm: create ${created.status}, patch ${patched.status}`);
  }

  if (top) {
    // Raw endpoint takes a bare array body.
    const t = await rfetch(env, "PATCH", `${path}/topics`, [{ id: top, subscription: "opt_in" }]);
    if (!t.ok) throw new Error(`confirm: topics ${t.status}`);
  }

  if (seg) {
    const s = await rfetch(env, "POST", `${path}/segments/${seg}`, null);
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
    `@media (prefers-color-scheme: dark){a{color:#ff9e5e;}.muted{color:#807a72;}}` +
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
    },
  });
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const email = (url.searchParams.get("e") || "").trim().toLowerCase();
  const ts = url.searchParams.get("t") || "";
  const sig = url.searchParams.get("s") || "";

  if (!env.RESEND_API_KEY || !env.SUBSCRIBE_SECRET) {
    return html(
      "Not configured",
      `<h1>Not configured</h1><p class="muted">Subscription confirmation is not set up yet.</p>`,
      500
    );
  }
  if (!email || !ts || !sig || email.length > 254 || !/^[0-9a-f]{64}$/.test(sig)) {
    return html(
      "Invalid link",
      `<h1>Invalid link</h1><p class="muted">This confirmation link is missing or malformed. <a href="/subscribe">Try subscribing again</a>.</p>`,
      400
    );
  }

  const expected = await hmacHex(env.SUBSCRIBE_SECRET, `${email}.${ts}`);
  if (!timingSafeEqual(expected, sig)) {
    return html(
      "Invalid link",
      `<h1>Invalid link</h1><p class="muted">This confirmation link could not be verified. <a href="/subscribe">Try subscribing again</a>.</p>`,
      400
    );
  }
  if (!/^\d+$/.test(ts) || Date.now() - Number(ts) > CONFIRM_TTL_MS) {
    return html(
      "Link expired",
      `<h1>Link expired</h1><p class="muted">This confirmation link has expired. <a href="/subscribe">Subscribe again</a> to get a fresh one.</p>`,
      410
    );
  }

  try {
    await subscribeConfirmed(env, email);
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
