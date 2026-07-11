// functions/api/subscribe/index.js
// POST /api/subscribe -- double opt-in signup for the Georgia Appellate Watch digest.
//
// This endpoint does NOT create a contact. It validates the address and emails an
// HMAC-signed confirmation link. The contact is created in Resend only after the
// link is clicked (see functions/api/subscribe/confirm.js), so unconfirmed addresses
// never enter the contact list. The Resend key never reaches the browser; it lives
// only in the Pages environment.
//
// Required Cloudflare Pages environment variables (Settings > Environment variables):
//   RESEND_API_KEY        Resend API key with contacts + sending access
//   SUBSCRIBE_SECRET      a long random string, e.g. `openssl rand -hex 32`
//   TURNSTILE_SECRET_KEY  Cloudflare Turnstile secret key (server-side only; pairs with the
//                         public site key embedded in subscribe.html)
// Optional:
//   DIGEST_FROM           From header (default below; must be a Resend-verified sender)
//   SITE_URL              site origin for the confirm link (default https://horowitz.law)
//   SUBSCRIBE_RATELIMIT   optional Workers rate-limit binding (see wrangler.toml); enforced
//                         only when present, so it never blocks a correct deploy

const RESEND = "https://api.resend.com";
const UA = "horowitz.law-subscribe/1.0 (+https://horowitz.law)";

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
    },
  });
}

function validEmail(e) {
  return typeof e === "string" && e.length <= 254 && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e);
}

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

function resendHeaders(env) {
  return {
    Authorization: `Bearer ${env.RESEND_API_KEY}`,
    "Content-Type": "application/json",
    Accept: "application/json",
    "User-Agent": UA, // a real UA: Cloudflare in front of api.resend.com blocks default library agents (error 1010)
  };
}

async function sendConfirmEmail(env, email, link) {
  const from = env.DIGEST_FROM || "Georgia Appellate Watch <digest@horowitz.law>";
  const safeLink = link.replace(/"/g, "&quot;");
  const html =
    `<!doctype html><html><body style="margin:0;padding:24px;background:#f5ede0;font-family:Georgia,'Times New Roman',serif;color:#1a1a1a;">` +
    `<div style="max-width:520px;margin:0 auto;background:#fffaf2;border:1px solid #d4cab8;border-radius:10px;padding:24px 28px;">` +
    `<div style="font:700 14px ui-monospace,Menlo,Consolas,monospace;color:#1a1a1a;">horowitz.law</div>` +
    `<p style="margin:16px 0 8px;font-size:15px;line-height:1.6;">Confirm your subscription to the <strong>Georgia Appellate Watch</strong> weekly digest.</p>` +
    `<p style="margin:0 0 20px;"><a href="${safeLink}" style="display:inline-block;background:#a4471a;color:#f5ede0;text-decoration:none;padding:12px 22px;border-radius:8px;font:600 14px -apple-system,Segoe UI,Roboto,sans-serif;">Confirm subscription</a></p>` +
    `<p style="font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#6a6560;margin:0;">If you did not request this, ignore this email and you will not be subscribed. This link expires in 48 hours.</p>` +
    `</div></body></html>`;
  const text =
    `Confirm your subscription to the Georgia Appellate Watch weekly digest:\n\n${link}\n\n` +
    `If you did not request this, ignore this email and you will not be subscribed. This link expires in 48 hours.`;
  // Bound the send with a timeout so a stalled api.resend.com cannot hang the
  // Worker; the abort rejects and onRequestPost turns it into a 500 for the form.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  let r;
  try {
    r = await fetch(`${RESEND}/emails`, {
      method: "POST",
      headers: resendHeaders(env),
      body: JSON.stringify({
        from,
        to: [email],
        subject: "Confirm your Georgia Appellate Watch subscription",
        html,
        text,
      }),
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) throw new Error(`send confirm: ${r.status}`);
}

// Verify a Turnstile token with Cloudflare's Siteverify API. Fails CLOSED: any non-success
// verdict, a hostname or action mismatch, or a network error returns false, so an email is
// never sent on an unverified request. The secret stays server-side; the browser sends only
// the token. Tokens are single-use, so a replayed token is rejected here as a duplicate.
async function verifyTurnstile(env, token, request) {
  const form = new URLSearchParams();
  form.set("secret", env.TURNSTILE_SECRET_KEY);
  form.set("response", token);
  const ip = request.headers.get("CF-Connecting-IP");
  if (ip) form.set("remoteip", ip);

  let data;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    data = await r.json();
  } catch {
    return false; // network error or timeout -> fail closed
  }
  if (!data || data.success !== true) return false;

  // Bind the token to THIS site and THIS form: reject one solved on another host or for
  // another action. A genuine Turnstile verdict always carries hostname and action (the
  // widget sets data-action="subscribe"), so a MISSING field is treated as a failure, not a
  // pass -- the binding is enforced unconditionally. Compare against hostname (no port),
  // which is the form Turnstile returns.
  let expectedHost;
  try {
    expectedHost = new URL(request.url).hostname;
  } catch {
    return false;
  }
  if (data.hostname !== expectedHost) return false;
  if (data.action !== "subscribe") return false;
  return true;
}

export async function onRequestPost(context) {
  const { request, env } = context;
  if (!env.RESEND_API_KEY || !env.SUBSCRIBE_SECRET) {
    return json({ ok: false, message: "Subscriptions are not configured yet." }, 500);
  }
  if (!env.TURNSTILE_SECRET_KEY) {
    return json({ ok: false, message: "Verification is not configured yet." }, 500);
  }

  // Same-origin only. A cross-site POST carries an Origin whose host will not match this
  // endpoint's host; reject it. Same-origin requests always match, which also covers the
  // apex, www, and the *.pages.dev preview without a hardcoded allowlist.
  const origin = request.headers.get("Origin");
  if (origin) {
    let bad = true;
    try { bad = new URL(origin).host !== new URL(request.url).host; } catch {}
    if (bad) return json({ ok: false, message: "Request blocked." }, 403);
  }

  // Require a JSON body. This also forces a CORS preflight for any cross-origin caller,
  // which the browser then blocks because no CORS headers are returned.
  const ctype = (request.headers.get("Content-Type") || "").toLowerCase();
  if (!ctype.includes("application/json")) {
    return json({ ok: false, message: "Bad request." }, 415);
  }
  // Read the body once, under a hard size ceiling enforced on the bytes ACTUALLY read, not
  // the Content-Length header (which a chunked or lying request can omit or understate). A
  // Turnstile token is up to ~2 KB; 8 KB leaves ample room for a legitimate request.
  let raw;
  try {
    raw = await request.text();
  } catch {
    return json({ ok: false, message: "Bad request." }, 400);
  }
  if (raw.length > 8192) {
    return json({ ok: false, message: "Request too large." }, 413);
  }

  // Optional edge rate limit (defense in depth). Enforced only if the SUBSCRIBE_RATELIMIT
  // binding is configured in wrangler.toml; skipped entirely otherwise, so it never blocks a
  // correct deploy. The WAF rate-limiting rule is the primary limiter (see deploy notes).
  if (env.SUBSCRIBE_RATELIMIT && typeof env.SUBSCRIBE_RATELIMIT.limit === "function") {
    const ipKey = request.headers.get("CF-Connecting-IP") || "anon";
    try {
      const { success } = await env.SUBSCRIBE_RATELIMIT.limit({ key: ipKey });
      if (!success) {
        return json({ ok: false, message: "Too many attempts. Please wait a minute and try again." }, 429);
      }
    } catch {
      /* never block legitimate users if the limiter itself errors */
    }
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ ok: false, message: "Bad request." }, 400);
  }

  const email = body && typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const honeypot = body && typeof body.company === "string" ? body.company : "";
  const token = body && typeof body.turnstileToken === "string" ? body.turnstileToken : "";

  // Optional practice-area choices. Sanitized to short lowercase tokens, deduped,
  // sorted, capped: the canonical CSV is covered by the HMAC below, so the confirm
  // step receives exactly what was requested here or nothing. Unknown codes are
  // harmless -- confirm.js maps them through RESEND_AREA_TOPICS and skips the rest --
  // so this list never needs to chase the taxonomy. Empty means the full digest.
  const areas = Array.isArray(body && body.areas)
    ? [...new Set(
        body.areas
          .filter((a) => typeof a === "string" && /^[a-z]{2,12}$/.test(a) && a !== "all")
      )].sort().slice(0, 10)
    : [];
  const areasCsv = areas.join(",");

  // Bot trap: the hidden field should always be empty. If filled, look successful but do
  // nothing, and spend no verification or email call on it.
  if (honeypot) {
    return json({ ok: true, message: "Almost there. Check your inbox for a confirmation link." });
  }

  if (!validEmail(email)) {
    return json({ ok: false, message: "Please enter a valid email address." }, 422);
  }

  // Second rate-limit dimension, keyed on the DESTINATION email (the IP-keyed limit above
  // does not stop a rotating IP pool from bombing one victim address with confirmation
  // emails). Same binding, a different key. Inert unless SUBSCRIBE_RATELIMIT is configured --
  // see the deploy notes: an always-on limiter (this binding or a WAF rule) is required, since
  // Turnstile alone does not bound how many emails a solved-challenge farm can send.
  if (env.SUBSCRIBE_RATELIMIT && typeof env.SUBSCRIBE_RATELIMIT.limit === "function") {
    try {
      const { success } = await env.SUBSCRIBE_RATELIMIT.limit({ key: "email:" + email });
      if (!success) {
        return json({ ok: false, message: "Too many attempts. Please wait a minute and try again." }, 429);
      }
    } catch { /* never block legitimate users if the limiter itself errors */ }
  }

  // Human-verification gate: confirm the Turnstile token with Cloudflare BEFORE sending any
  // email, so a forged, missing, or replayed token can never trigger a message from our domain.
  if (!token) {
    return json({ ok: false, message: "Please complete the verification and try again." }, 400);
  }
  if (!(await verifyTurnstile(env, token, request))) {
    return json({ ok: false, message: "Verification failed. Please reload the page and try again." }, 403);
  }

  try {
    const ts = Date.now();
    // Back-compat HMAC: the area list joins the signed message only when present,
    // so links from emails sent before this field existed still verify.
    const msg = areasCsv ? `${email}.${ts}.${areasCsv}` : `${email}.${ts}`;
    const sig = await hmacHex(env.SUBSCRIBE_SECRET, msg);
    const site = (env.SITE_URL || "https://horowitz.law").replace(/\/+$/, "");
    const aPart = areasCsv ? `&a=${encodeURIComponent(areasCsv)}` : "";
    const link = `${site}/api/subscribe/confirm?e=${encodeURIComponent(email)}&t=${ts}${aPart}&s=${sig}`;

    await sendConfirmEmail(env, email, link);
    return json({
      ok: true,
      message: "Almost there. Check your inbox for a confirmation link to finish subscribing.",
    });
  } catch (e) {
    // 500, not 502: Cloudflare intercepts a 502 from a Function and serves its own error page;
    // 500 passes through, so the browser receives this JSON and the form shows a real message.
    return json(
      { ok: false, message: "Something went wrong on our end. Please try again in a moment." },
      500
    );
  }
}

export async function onRequestGet() {
  return json({ ok: false, message: "Use the subscribe form." }, 405);
}
