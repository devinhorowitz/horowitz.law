// functions/subscribe/index.js
// POST /subscribe -- double opt-in signup for the Georgia Appellate Watch digest.
//
// Adds the address to the Resend audience as PENDING (unsubscribed: true) and emails
// an HMAC-signed confirmation link. The address only begins receiving once it is
// confirmed at /subscribe/confirm, which flips unsubscribed to false. The Resend key
// never reaches the browser; it lives only in the Pages environment.
//
// Required Cloudflare Pages environment variables (Settings > Environment variables):
//   RESEND_API_KEY       Resend API key with contacts + sending access
//   RESEND_AUDIENCE_ID   the audience's ID
//   SUBSCRIBE_SECRET     a long random string, e.g. `openssl rand -hex 32`
// Optional:
//   DIGEST_FROM          From header (default below; must be a Resend-verified sender)
//   SITE_URL             site origin for the confirm link (default https://horowitz.law)

const RESEND = "https://api.resend.com";
const UA = "horowitz.law-subscribe/1.0 (+https://horowitz.law)";

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
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
    "User-Agent": UA, // a real UA: Cloudflare in front of api.resend.com blocks default library agents
  };
}

// Returns the contact object if it exists, or null on 404.
// NOTE: assumes Resend returns 404 for a missing contact looked up by email.
async function getContact(env, email) {
  const r = await fetch(
    `${RESEND}/audiences/${env.RESEND_AUDIENCE_ID}/contacts/${encodeURIComponent(email)}`,
    { headers: resendHeaders(env) }
  );
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`get contact: ${r.status}`);
  const d = await r.json();
  return d && d.data ? d.data : d;
}

// Ensure the contact exists and is pending (unsubscribed: true), without
// downgrading anyone already confirmed (caller checks that first).
async function upsertPending(env, email) {
  const create = await fetch(`${RESEND}/audiences/${env.RESEND_AUDIENCE_ID}/contacts`, {
    method: "POST",
    headers: resendHeaders(env),
    body: JSON.stringify({ email, unsubscribed: true }),
  });
  if (create.ok) return;
  // Already exists (or similar): make sure it is marked pending.
  await fetch(
    `${RESEND}/audiences/${env.RESEND_AUDIENCE_ID}/contacts/${encodeURIComponent(email)}`,
    {
      method: "PATCH",
      headers: resendHeaders(env),
      body: JSON.stringify({ unsubscribed: true }),
    }
  );
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
    `<p style="font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#6a6560;margin:0;">If you did not request this, ignore this email and you will not be subscribed. This link expires in 7 days.</p>` +
    `</div></body></html>`;
  const text =
    `Confirm your subscription to the Georgia Appellate Watch weekly digest:\n\n${link}\n\n` +
    `If you did not request this, ignore this email and you will not be subscribed. This link expires in 7 days.`;
  const r = await fetch(`${RESEND}/emails`, {
    method: "POST",
    headers: resendHeaders(env),
    body: JSON.stringify({
      from,
      to: [email],
      subject: "Confirm your Georgia Appellate Watch subscription",
      html,
      text,
    }),
  });
  if (!r.ok) throw new Error(`send confirm: ${r.status}`);
}

export async function onRequestPost(context) {
  const { request, env } = context;
  if (!env.RESEND_API_KEY || !env.RESEND_AUDIENCE_ID || !env.SUBSCRIBE_SECRET) {
    return json({ ok: false, message: "Subscriptions are not configured yet." }, 500);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, message: "Bad request." }, 400);
  }

  const email = body && typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const honeypot = body && typeof body.company === "string" ? body.company : "";

  // Bot trap: the hidden field should always be empty. If filled, look successful but do nothing.
  if (honeypot) {
    return json({ ok: true, message: "Almost there. Check your inbox for a confirmation link." });
  }

  if (!validEmail(email)) {
    return json({ ok: false, message: "Please enter a valid email address." }, 422);
  }

  try {
    const existing = await getContact(env, email);
    if (existing && existing.unsubscribed === false) {
      return json({ ok: true, message: "You are already subscribed." });
    }

    await upsertPending(env, email);

    const ts = Date.now();
    const sig = await hmacHex(env.SUBSCRIBE_SECRET, `${email}.${ts}`);
    const site = (env.SITE_URL || "https://horowitz.law").replace(/\/+$/, "");
    const link = `${site}/subscribe/confirm?e=${encodeURIComponent(email)}&t=${ts}&s=${sig}`;

    await sendConfirmEmail(env, email, link);
    return json({
      ok: true,
      message: "Almost there. Check your inbox for a confirmation link to finish subscribing.",
    });
  } catch (e) {
    return json(
      { ok: false, message: "Something went wrong on our end. Please try again in a moment." },
      502
    );
  }
}

export async function onRequestGet() {
  return json({ ok: false, message: "Use the subscribe form." }, 405);
}
