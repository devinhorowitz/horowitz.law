// functions/subscribe/confirm.js
// GET /subscribe/confirm?e=<email>&t=<ts>&s=<hmac>
//
// Verifies the signed link from the confirmation email and flips the Resend contact
// from pending (unsubscribed: true) to subscribed (unsubscribed: false). Links are
// valid for 7 days and cannot be forged without SUBSCRIBE_SECRET.
//
// Uses the same environment variables as functions/subscribe/index.js.

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
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

async function markSubscribed(env, email) {
  const r = await fetch(
    `${RESEND}/audiences/${env.RESEND_AUDIENCE_ID}/contacts/${encodeURIComponent(email)}`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${env.RESEND_API_KEY}`,
        "Content-Type": "application/json",
        Accept: "application/json",
        "User-Agent": UA,
      },
      body: JSON.stringify({ unsubscribed: false }),
    }
  );
  if (!r.ok) throw new Error(`patch: ${r.status}`);
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const email = (url.searchParams.get("e") || "").trim().toLowerCase();
  const ts = url.searchParams.get("t") || "";
  const sig = url.searchParams.get("s") || "";

  if (!env.RESEND_API_KEY || !env.RESEND_AUDIENCE_ID || !env.SUBSCRIBE_SECRET) {
    return html(
      "Not configured",
      `<h1>Not configured</h1><p class="muted">Subscription confirmation is not set up yet.</p>`,
      500
    );
  }
  if (!email || !ts || !sig) {
    return html(
      "Invalid link",
      `<h1>Invalid link</h1><p class="muted">This confirmation link is missing information. <a href="/subscribe">Try subscribing again</a>.</p>`,
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
    await markSubscribed(env, email);
  } catch (e) {
    return html(
      "Something went wrong",
      `<h1>Something went wrong</h1><p class="muted">We could not confirm your subscription just now. Please <a href="/subscribe">try again</a>.</p>`,
      502
    );
  }

  return html(
    "Subscribed",
    `<h1>You are subscribed.</h1>` +
      `<p>You will receive the Georgia Appellate Watch digest in weeks with new decisions.</p>` +
      `<p class="muted"><a href="/opinions">Browse the latest decisions</a></p>`
  );
}
