// functions/api/subscribe/confirm.js — TEMPORARY DIAGNOSTIC BUILD (replace after diagnosis)
//
// Verifies the signed link exactly like production, then makes the real Resend
// POST /contacts call with full instrumentation and prints the outcome to the page.
// It prints only whether the secrets exist (booleans), never their values.
// Once we read the output, swap this back for the real confirm.js.

const RESEND = "https://api.resend.com";
const UA = "horowitz.law-subscribe/1.0 (+https://horowitz.law)";
const CONFIRM_TTL_MS = 7 * 24 * 60 * 60 * 1000;

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
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function out(lines, status = 200) {
  const body =
    `<!doctype html><meta charset="utf-8"><title>confirm diagnostic</title>` +
    `<pre style="font:13px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;padding:24px;max-width:900px;margin:0 auto;">` +
    esc(lines.join("\n")) +
    `</pre>`;
  return new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const log = [];
  try {
    const url = new URL(request.url);
    const email = (url.searchParams.get("e") || "").trim().toLowerCase();
    const ts = url.searchParams.get("t") || "";
    const sig = url.searchParams.get("s") || "";

    log.push("=== DIAGNOSTIC confirm.js ===");
    log.push("RESEND_API_KEY present:   " + !!env.RESEND_API_KEY);
    log.push("SUBSCRIBE_SECRET present: " + !!env.SUBSCRIBE_SECRET);
    log.push("RESEND_SEGMENT_ID: " + (env.RESEND_SEGMENT_ID || "(UNSET)"));
    log.push("RESEND_TOPIC_ID:   " + (env.RESEND_TOPIC_ID || "(UNSET)"));
    log.push("");

    if (!env.RESEND_API_KEY || !env.SUBSCRIBE_SECRET)
      return out(log.concat("STOP: a required secret is missing at runtime."));
    if (!email || !ts || !sig)
      return out(log.concat("STOP: missing e/t/s params. Open the link from the confirmation email."));

    const expected = await hmacHex(env.SUBSCRIBE_SECRET, `${email}.${ts}`);
    if (!timingSafeEqual(expected, sig)) return out(log.concat("STOP: signature did not verify."));
    if (!/^\d+$/.test(ts) || Date.now() - Number(ts) > CONFIRM_TTL_MS)
      return out(log.concat("STOP: link expired."));

    log.push("verify: OK   email=" + email);

    const seg = env.RESEND_SEGMENT_ID;
    const top = env.RESEND_TOPIC_ID;
    const createBody = { email, unsubscribed: false };
    if (seg) createBody.segments = [{ id: seg }];
    if (top) createBody.topics = [{ id: top, subscription: "opt_in" }];
    log.push("request: POST " + RESEND + "/contacts");
    log.push("body:    " + JSON.stringify(createBody));
    log.push("");
    log.push("STEP A: calling fetch() ...");

    let r;
    try {
      r = await fetch(RESEND + "/contacts", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.RESEND_API_KEY}`,
          "Content-Type": "application/json",
          Accept: "application/json",
          "User-Agent": UA,
        },
        body: JSON.stringify(createBody),
      });
    } catch (e) {
      return out(
        log.concat([
          "STEP A RESULT: fetch() REJECTED (threw).",
          "  name:    " + (e && e.name),
          "  message: " + (e && e.message),
          "  stack:   " + (e && e.stack),
        ])
      );
    }

    log.push("STEP A RESULT: fetch() returned a response.");
    log.push("  status: " + r.status + "   ok: " + r.ok);
    log.push("STEP B: response headers:");
    try {
      r.headers.forEach((v, k) => {
        log.push("  " + k + ": " + (v.length > 300 ? v.slice(0, 300) + " ...(" + v.length + " chars)" : v));
      });
    } catch (e) {
      log.push("  (could not read headers: " + (e && e.message) + ")");
    }

    let text = "";
    try {
      text = await r.text();
    } catch (e) {
      return out(log.concat(["STEP C: reading body THREW: " + (e && e.message)]));
    }
    log.push("STEP C: body (" + text.length + " chars):");
    log.push(text.slice(0, 1500));
    log.push("");
    log.push("=== DONE: the Worker survived. If you previously saw a 502, the crash was NOT this fetch. ===");
    return out(log);
  } catch (e) {
    return out(
      log.concat([
        "",
        "OUTER CATCH (something above threw):",
        "  name:    " + (e && e.name),
        "  message: " + (e && e.message),
        "  stack:   " + (e && e.stack),
      ])
    );
  }
}
