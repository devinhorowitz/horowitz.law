// functions/mcp/index.js -- a public, read-only MCP server over the Georgia Appellate Watch content.
//
// POST /mcp   JSON-RPC 2.0 over HTTP (streamable HTTP, stateless JSON). No auth, no cookies, no
// stored state: the content is already public at /opinions.xml and /changes.xml, and this is the
// same material addressed to a model instead of an inbox or a feed reader.
//
// WHY THIS EXISTS. The email digest pushes on the sender's schedule and lands as prose a person has
// to re-key into a chat. A routine polling this pulls on ITS schedule, filtered to the practice
// areas it cares about, and gets structured deltas it can act on -- new law, and, more importantly,
// authority it already relies on that has since been disturbed.
//
// THE CURSOR IS THE CALLER'S. The server stores nothing per client; `since` is passed in. That one
// decision removes auth, per-user state, and the whole subscription surface. A routine already
// remembers its own watermark.
//
// SILENCE IS NOT SUCCESS -- the rule this file exists to enforce. A canary that goes quiet because
// nothing happened looks exactly like one that goes quiet because the pipeline stalled, and the
// second is the more dangerous by far, because a canary's whole value is being trusted when it says
// nothing. So no tool ever returns a bare empty list. Every response carries a `feed` block built
// from TWO sources with different update rules:
//
//   /status.json   scanned_at        written on EVERY scan, found anything or not  -> liveness
//                  content_updated_at written only when content actually changed   -> quiet vs busy
//   /api/feed.json generated          written when content is rendered              -> feed age
//
// Fresh scan + old content means Georgia was quiet, and that is a real answer. A stale scan means
// this feed cannot be trusted to be quiet, and the response says so in `health` and in
// `trust_silence`. This repo has already had an 82-hour content gap sitting behind a healthy scan;
// a consumer reading only content would have concluded, wrongly, that nothing had happened.
//
// Optional Pages environment variables:
//   SITE_URL          origin to read the feed from (default: this request's own origin)
//   MCP_STALE_HOURS   scan age at which health flips to "stale" (default 36)

const PROTOCOL_VERSION = "2025-06-18";
const SERVER = { name: "horowitz-law-mcp-server", version: "1.0.0" };
const DEFAULT_STALE_HOURS = 36;
const MAX_LIMIT = 100;
const DEFAULT_LIMIT = 25;

// ---------------------------------------------------------------------------- feed access

async function readJson(origin, path, request) {
  const url = new URL(path, origin).toString();
  const r = await fetch(url, { headers: { accept: "application/json" }, cf: { cacheTtl: 60 } });
  if (!r.ok) throw new Error(`${path} returned ${r.status}`);
  return r.json();
}

function hoursSince(iso, now) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (now - t) / 3600000;
}

/**
 * The health block attached to every tool result.
 *
 * `trust_silence` is the field that matters and the reason the two timestamps are read separately:
 * it answers "if this tool told me nothing changed, should I believe it?" A routine should refuse
 * to digest, and refuse to conclude nothing happened, when it is false.
 */
function feedHealth(status, feed, now, staleHours) {
  const scanAge = hoursSince(status && status.scanned_at, now);
  const contentAge = hoursSince(status && status.content_updated_at, now);
  const stale = scanAge === null || scanAge > staleHours;
  return {
    health: stale ? "stale" : "ok",
    trust_silence: !stale,
    scanned_at: (status && status.scanned_at) || null,
    content_updated_at: (status && status.content_updated_at) || null,
    generated: (feed && feed.generated) || null,
    scan_age_hours: scanAge === null ? null : Math.round(scanAge * 10) / 10,
    content_age_hours: contentAge === null ? null : Math.round(contentAge * 10) / 10,
    stale_after_hours: staleHours,
    schema: (feed && feed.schema) || null,
    note: stale
      ? "The scan is overdue, so an empty result CANNOT be read as 'nothing happened' -- it may mean "
        + "the pipeline stopped. Do not digest this as a clean bill of health; re-check later or "
        + "inspect the site."
      : "The scan is current, so an empty result genuinely means nothing changed in the window.",
  };
}

// ---------------------------------------------------------------------------- helpers

function normalize(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function clampLimit(n) {
  const v = Number.isFinite(n) ? Math.floor(n) : DEFAULT_LIMIT;
  return Math.max(1, Math.min(MAX_LIMIT, v));
}

function paginate(items, offset, limit) {
  const off = Math.max(0, Number.isFinite(offset) ? Math.floor(offset) : 0);
  const lim = clampLimit(limit);
  const page = items.slice(off, off + lim);
  return {
    total: items.length,
    count: page.length,
    offset: off,
    has_more: off + page.length < items.length,
    next_offset: off + page.length < items.length ? off + page.length : null,
    items: page,
  };
}

/** Trim a card to what a digesting routine needs, so a page of results stays readable. */
function slim(card, verbose) {
  const keep = ["cluster_id", "name", "court", "date", "dockets", "disposition", "areas",
                "precedential", "url", "official_url", "why", "changed", "change",
                "treatment", "treatment_date", "treatment_note"];
  const out = {};
  for (const k of keep) if (card[k] !== undefined) out[k] = card[k];
  if (verbose) {
    for (const k of ["synopsis", "additional_holdings", "law_applied", "jurisdiction",
                     "first_impression", "tort_reform", "editor_note", "treated_by"]) {
      if (card[k] !== undefined) out[k] = card[k];
    }
  }
  return out;
}

function toMarkdown(payload) {
  const f = payload.feed || {};
  const lines = [`**Feed:** ${f.health} — scanned ${f.scanned_at || "?"}`
    + (f.content_age_hours !== null && f.content_age_hours !== undefined
       ? `, content last changed ${f.content_age_hours}h ago` : "")
    + `. Trust an empty result: ${f.trust_silence ? "yes" : "NO"}.`, ""];
  const items = (payload.results && payload.results.items) || payload.authorities || [];
  if (!items.length) {
    lines.push(payload.results
      ? `_No matching items._ ${f.note}` : `_Nothing to report._ ${f.note}`);
    return lines.join("\n");
  }
  for (const it of items) {
    if (it.query !== undefined) {
      lines.push(`- **${it.query}** — ${it.status}${it.card ? `: ${it.card.name}` : ""}`
        + (it.treatment ? ` (**${it.treatment}**${it.treatment_date ? `, ${it.treatment_date}` : ""})` : ""));
      if (it.note) lines.push(`  - ${it.note}`);
      continue;
    }
    lines.push(`- **${it.name}** (${it.court || "?"} ${it.date || "?"})`
      + (it.areas ? ` — _${it.areas.join(", ")}_` : "")
      + (it.change === "treatment" ? "  ⚠️ **treatment change**" : ""));
    if (it.why) lines.push(`  - ${it.why}`);
    if (it.url) lines.push(`  - ${it.url}`);
  }
  if (payload.results && payload.results.has_more) {
    lines.push("", `_${payload.results.total} total; next_offset=${payload.results.next_offset}._`);
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------------------- tools

const TOOLS = [
  {
    name: "hlaw_whats_new",
    title: "What changed since",
    description:
      "Georgia civil-litigation and insurance appellate developments that have changed since a date "
      + "you supply. Returns BOTH newly carded opinions and previously published opinions that have "
      + "since been flagged with adverse treatment (change='treatment'), which is the signal a "
      + "practitioner most needs and which no publication-date feed can surface. Covers the Georgia, "
      + "Florida and Alabama appellate courts, the Eleventh Circuit and the U.S. Supreme Court. "
      + "Every response reports feed health: if trust_silence is false, an empty result may mean the "
      + "pipeline stalled rather than that nothing happened.",
    inputSchema: {
      type: "object",
      properties: {
        since: { type: "string", description: "ISO date or datetime (e.g. '2026-07-01'). Items whose `changed` is strictly after this are returned. Omit for the most recent items." },
        areas: { type: "array", items: { type: "string" }, description: "Practice areas to filter to, e.g. ['premises','negsec']. Known areas: coverage, badfaith, auto, premises, negsec, damages, expert, procedure. Omit for all." },
        kinds: { type: "array", items: { type: "string", enum: ["new", "treatment", "legislation", "courtrule"] }, description: "Restrict to certain kinds of change. Omit for opinions only (new + treatment); pass explicitly to include legislation and court rules." },
        verbose: { type: "boolean", description: "Include synopsis, additional holdings and treatment detail. Default false." },
        limit: { type: "integer", description: `Max items (1-${MAX_LIMIT}, default ${DEFAULT_LIMIT}).` },
        offset: { type: "integer", description: "Items to skip, for paging." },
        response_format: { type: "string", enum: ["json", "markdown"], description: "Default json." },
      },
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "hlaw_check_authorities",
    title: "Check authorities for adverse treatment",
    description:
      "Given cases you are relying on -- CourtListener cluster ids and/or case names -- report "
      + "whether any carry an adverse-treatment flag in this feed. Use before filing, before "
      + "publishing, or when refreshing material that cites Georgia authority. "
      + "IMPORTANT: 'not flagged' is NOT a clearance. This feed tracks only its own ~113 curated "
      + "cards and its treatment sweep is incomplete, so an unflagged or unknown case has simply "
      + "not been flagged HERE. Verify on a citator before relying on any answer.",
    inputSchema: {
      type: "object",
      properties: {
        cluster_ids: { type: "array", items: { type: "integer" }, description: "CourtListener cluster ids to check." },
        names: { type: "array", items: { type: "string" }, description: "Case names or fragments, e.g. 'Aspen American'. Matched case-insensitively against the caption." },
        response_format: { type: "string", enum: ["json", "markdown"], description: "Default json." },
      },
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "hlaw_feed_status",
    title: "Feed health and coverage",
    description:
      "Whether the pipeline is running, when it last scanned, when content last changed, and how "
      + "many cards exist per practice area. Call this before trusting an empty result from any "
      + "other tool, and to see the DENOMINATOR before relying on a thin area -- some areas hold "
      + "only a handful of cards.",
    inputSchema: {
      type: "object",
      properties: {
        response_format: { type: "string", enum: ["json", "markdown"], description: "Default json." },
      },
    },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
];

function runWhatsNew(args, feed, health) {
  const since = (args.since || "").trim();
  const kinds = Array.isArray(args.kinds) && args.kinds.length ? args.kinds : ["new", "treatment"];
  const areas = Array.isArray(args.areas) && args.areas.length
    ? args.areas.map((a) => String(a).toLowerCase()) : null;

  let items = [];
  if (kinds.includes("new") || kinds.includes("treatment")) {
    items = items.concat((feed.cards || []).filter((c) => kinds.includes(c.change)));
  }
  for (const w of feed.watches || []) if (kinds.includes(w.change)) items.push(w);

  if (since) items = items.filter((c) => (c.changed || "") > since);
  if (areas) items = items.filter((c) => (c.areas || []).some((a) => areas.includes(String(a).toLowerCase())));
  items.sort((a, b) => String(b.changed || "").localeCompare(String(a.changed || "")));

  const page = paginate(items.map((c) => slim(c, !!args.verbose)), args.offset, args.limit);
  return {
    feed: health,
    query: { since: since || null, areas: areas, kinds },
    coverage: (feed.counts && feed.counts.by_area) || {},
    results: page,
  };
}

function runCheckAuthorities(args, feed, health) {
  const ids = Array.isArray(args.cluster_ids) ? args.cluster_ids : [];
  const names = Array.isArray(args.names) ? args.names : [];
  const cards = feed.cards || [];
  const out = [];

  const record = (query, card) => {
    if (!card) {
      out.push({
        query, status: "not_in_feed", treatment: null,
        note: "Not among this feed's curated cards. That is not a clearance -- it means this feed "
            + "does not track the case. Check a citator.",
      });
      return;
    }
    const treated = !!card.treatment;
    out.push({
      query,
      status: treated ? "flagged" : "no_flag",
      treatment: card.treatment || null,
      treatment_date: card.treatment_date || null,
      treatment_note: card.treatment_note || null,
      treated_by: card.treated_by || null,
      card: slim(card, false),
      note: treated
        ? "Adverse treatment recorded. Read the flagged card and the later opinion before relying "
          + "on this case."
        : "No adverse-treatment flag in this feed. NOT a clearance: the treatment sweep is "
          + "incomplete, so this means only that nothing has been flagged here.",
    });
  };

  for (const id of ids) {
    record(id, cards.find((c) => String(c.cluster_id) === String(id)) || null);
  }
  for (const raw of names) {
    const q = normalize(raw);
    record(raw, q ? cards.find((c) => normalize(c.name).includes(q)) || null : null);
  }
  if (!ids.length && !names.length) {
    return {
      feed: health, authorities: [],
      note: "No authorities supplied. Pass cluster_ids and/or names to check.",
    };
  }
  return {
    feed: health,
    authorities: out,
    checked: out.length,
    flagged: out.filter((a) => a.status === "flagged").length,
    disclaimer: "This feed carries adverse-treatment flags for its own curated cards only, and its "
      + "sweep is incomplete. Absence of a flag is not a statement that a case remains good law.",
  };
}

function runFeedStatus(feed, health) {
  return {
    feed: health,
    counts: feed.counts || {},
    coverage_note: "by_area is the DENOMINATOR for each practice area. Thin areas answer thin: "
      + "state the count alongside any conclusion drawn from them.",
    source: "https://horowitz.law",
    content_disclaimer: "Cards are editorial summaries of published opinions, not primary law. "
      + "Each carries `url` (and often `official_url`) to the opinion itself; verify there before "
      + "relying on any summary.",
  };
}

async function callTool(name, args, ctx) {
  const { origin, request, now, staleHours } = ctx;
  let feed = { cards: [], watches: [], counts: {} };
  let status = null;
  let loadError = null;
  try {
    feed = await readJson(origin, "/api/feed.json", request);
  } catch (e) {
    loadError = e.message;
  }
  try {
    status = await readJson(origin, "/status.json", request);
  } catch (e) {
    status = null;
  }
  const health = feedHealth(status, feed, now, staleHours);
  if (loadError) {
    health.health = "unavailable";
    health.trust_silence = false;
    health.note = `The content feed could not be read (${loadError}). No conclusion about what has `
      + "or has not changed can be drawn from this response.";
  }

  if (name === "hlaw_whats_new") return runWhatsNew(args || {}, feed, health);
  if (name === "hlaw_check_authorities") return runCheckAuthorities(args || {}, feed, health);
  if (name === "hlaw_feed_status") return runFeedStatus(feed, health);
  throw new Error(`Unknown tool '${name}'. Available: ${TOOLS.map((t) => t.name).join(", ")}.`);
}

// ---------------------------------------------------------------------------- JSON-RPC

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function handleRpc(msg, ctx) {
  const { id, method, params } = msg || {};
  if (method === "initialize") {
    return rpcResult(id, {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: SERVER,
      instructions:
        "Public, read-only feed of Georgia civil-litigation and insurance appellate developments. "
        + "Poll hlaw_whats_new with a `since` watermark you keep yourself. ALWAYS read the `feed` "
        + "block: when trust_silence is false, an empty result may mean the pipeline stalled rather "
        + "than that nothing changed. Cards are editorial summaries, not primary law -- verify at "
        + "the linked opinion before relying on one.",
    });
  }
  if (method === "ping") return rpcResult(id, {});
  if (method === "tools/list") return rpcResult(id, { tools: TOOLS });
  if (method === "tools/call") {
    const name = params && params.name;
    const args = (params && params.arguments) || {};
    try {
      const payload = await callTool(name, args, ctx);
      const wantMd = String(args.response_format || "json").toLowerCase() === "markdown";
      return rpcResult(id, {
        content: [{ type: "text", text: wantMd ? toMarkdown(payload) : JSON.stringify(payload, null, 2) }],
        structuredContent: payload,
      });
    } catch (e) {
      // Tool failures are reported inside the result, not as protocol errors, so the model can read
      // and act on them rather than seeing an opaque transport failure.
      return rpcResult(id, { isError: true, content: [{ type: "text", text: `Error: ${e.message}` }] });
    }
  }
  if (typeof method === "string" && method.startsWith("notifications/")) return null;
  return rpcError(id, -32601, `Method not found: ${method}`);
}

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-allow-headers": "content-type, mcp-protocol-version, accept",
  "access-control-max-age": "86400",
};

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS });
}

export async function onRequestGet() {
  // A browser or crawler hitting /mcp should learn what this is rather than see an error.
  return new Response(JSON.stringify({
    server: SERVER, protocolVersion: PROTOCOL_VERSION,
    transport: "streamable-http (stateless JSON); POST JSON-RPC 2.0 to this URL",
    tools: TOOLS.map((t) => ({ name: t.name, description: t.description })),
    docs: "https://horowitz.law/colophon",
  }, null, 2), { status: 200, headers: { "content-type": "application/json", ...CORS } });
}

export async function onRequestPost({ request, env }) {
  const staleHours = Number(env && env.MCP_STALE_HOURS) > 0
    ? Number(env.MCP_STALE_HOURS) : DEFAULT_STALE_HOURS;
  const origin = (env && env.SITE_URL) || new URL(request.url).origin;
  const ctx = { origin, request, now: Date.now(), staleHours };

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify(rpcError(null, -32700, "Parse error: body must be JSON")),
      { status: 400, headers: { "content-type": "application/json", ...CORS } });
  }

  const batch = Array.isArray(body);
  const msgs = batch ? body : [body];
  const out = [];
  for (const m of msgs) {
    const r = await handleRpc(m, ctx);
    if (r) out.push(r);
  }
  if (!out.length) return new Response(null, { status: 202, headers: CORS });   // notifications only
  return new Response(JSON.stringify(batch ? out : out[0]),
    { status: 200, headers: { "content-type": "application/json", ...CORS } });
}
