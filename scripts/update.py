#!/usr/bin/env python3
"""Georgia Appellate Watch updater (three-tier funnel).

Daily pipeline, standard library only. Three model tiers, cheapest first, so the
expensive model only ever touches confirmed keepers:

  Tier 1  SCREEN   (Haiku)  reads the case name and opening excerpt only and drops
                            the categorically unrelated (criminal, family, juvenile,
                            probate, tax, bar, election, dispossessory) and one-line
                            application or clerk orders. Permissive: anything civil or
                            ambiguous passes, so nothing relevant is dropped on a glance.
  Tier 2  TRIAGE   (Sonnet) reads the FULL opinion and decides, against a narrow bar,
                            whether it genuinely decides or clarifies something relevant,
                            catching holdings that are not visible from the opening.
  Tier 3  SUMMARIZE(Opus)   reads the FULL opinion plus the triage note and writes the
                            public-facing card in the house style. Final backstop: it can
                            still decline. Runs at effort=high by default (its accuracy
                            lever); Opus 4.8 does not take an extended-thinking budget.

Keepers are appended to opinions.json, opinions_state.json is updated, opinions.html
and opinions.xml are re-rendered, and scripts/pr_body.md is written for the pull request.

Run from the repo root: `python scripts/update.py`. No third-party packages.

Environment:
  ANTHROPIC_API_KEY        required
  COURTLISTENER_TOKEN      optional (raises CourtListener rate limits)
  OPINIONS_MODEL           Tier 3 summarizer (default claude-opus-4-8)
  OPINIONS_TRIAGE_MODEL    Tier 2 full-read gate (default claude-sonnet-4-6). "" disables it.
  OPINIONS_SCREEN_MODEL    Tier 1 excerpt screen (default claude-haiku-4-5-20251001). "" disables it.
  OPINIONS_COURTS          CourtListener court ids (default "ga,gactapp")
  OPINIONS_LOOKBACK        fallback look-back window in days when state is empty (default 21)
  OPINIONS_MAX             max opinions evaluated per run (default 25)
  OPINIONS_MAXCHARS        opinion characters sent to triage and summarizer (default 60000)
  OPINIONS_MAX_TOKENS      summarizer output token cap (default 4096)
  DRY_RUN                  if set to 1, evaluate and print but write nothing
  OPINIONS_DEBUG           if set to 1, log every model call and full API error bodies
  OPINIONS_EFFORT          Opus reasoning effort high|medium|low (default medium); "" uses the API default
  OPINIONS_BUDGET_SEC      wall-clock cap on the candidate loop in seconds (default 480)
  OPINIONS_BREAKER         stop after this many consecutive API failures (default 4)
  OPINIONS_SEARCH_BUDGET_SEC  wall-clock cap on the CourtListener search phase (default 120)
"""
import os, re, sys, json, time, html, datetime
import urllib.request, urllib.parse, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import render  # single source of truth renderer

JSON_PATH  = os.path.join(REPO, "opinions.json")
STATE_PATH = os.path.join(REPO, "opinions_state.json")
PR_PATH    = os.path.join(REPO, "scripts", "pr_body.md")

KEY          = os.environ.get("ANTHROPIC_API_KEY", "")
CL_TOKEN     = os.environ.get("COURTLISTENER_TOKEN", "")
MODEL        = os.environ.get("OPINIONS_MODEL", "claude-opus-4-8")
TRIAGE_MODEL = os.environ.get("OPINIONS_TRIAGE_MODEL", "claude-sonnet-4-6")
SCREEN_MODEL = os.environ.get("OPINIONS_SCREEN_MODEL", "claude-haiku-4-5-20251001")
VERSION      = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
COURTS       = [c.strip() for c in os.environ.get("OPINIONS_COURTS", "ga,gactapp").split(",") if c.strip()]
LOOKBACK     = int(os.environ.get("OPINIONS_LOOKBACK", "21"))
MAX_RUN      = int(os.environ.get("OPINIONS_MAX", "25"))
MAXCHARS     = int(os.environ.get("OPINIONS_MAXCHARS", "60000"))
OUT_TOKENS   = int(os.environ.get("OPINIONS_MAX_TOKENS", "4096"))
DRY_RUN      = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
DEBUG        = os.environ.get("OPINIONS_DEBUG", "") in ("1", "true", "True", "yes")
EFFORT       = os.environ.get("OPINIONS_EFFORT", "medium").strip()
BUDGET_SEC   = int(os.environ.get("OPINIONS_BUDGET_SEC", "480"))
BREAKER      = int(os.environ.get("OPINIONS_BREAKER", "4"))
SEARCH_BUDGET= int(os.environ.get("OPINIONS_SEARCH_BUDGET_SEC", "120"))

COURT_MAP   = {"ga": "scotga", "gactapp": "ctapp"}
VALID_AREAS = set(render.AREA_LABELS)
CITE_RE = re.compile(r"\b\d+\s+(?:Ga\.?\s*App\.?|Ga\.?|S\.?\s*E\.?\s*2d|S\.?\s*E\.?|U\.?\s*S\.?|S\.?\s*Ct\.?|F\.?(?:2d|3d|4th)?|F\.?\s*Supp\.?)\s+\d+", re.I)

SCREEN_SYSTEM = (
    "You are a fast first-pass screener for a curated feed of Georgia appellate decisions "
    "for an insurance-defense and civil-litigation audience. You see only a case name and a "
    "short opening excerpt. Be permissive: your only job is to discard cases that cannot "
    "possibly belong, not to judge relevance.\n\n"
    "FAIL only if the case is clearly one of these: criminal (often captioned 'v. The State'), "
    "habeas, family or domestic, juvenile or dependency ('In the Interest of'), probate or "
    "wills, tax, workers' compensation, attorney discipline or bar admission, election, or "
    "landlord-tenant or dispossessory; or it is a one-line order that merely grants or denies "
    "an application or dismisses for failure to file, with no merits.\n\n"
    "PASS everything else, including any general civil case and anything you are not sure "
    "about. A later step reads the full opinion, so when in doubt, PASS. "
    "Output ONLY a JSON object: {\"pass\": true or false, \"reason\": \"a few words\"}."
)

TRIAGE_SYSTEM = (
    "You are the second-stage reviewer for a CURATED, NARROW feed of Georgia appellate "
    "decisions for an insurance-defense and civil-litigation audience. A cheap first pass has "
    "already removed the obviously unrelated cases. You are given the FULL text of one "
    "opinion. Catch genuine relevance that a glance at the opening would miss, while keeping "
    "the feed narrow.\n\n"
    "Mark relevant=true only if the opinion DECIDES or CLARIFIES a point in one of these "
    "areas, even if that point is not apparent from the caption or opening and even if it is a "
    "secondary holding: auto or UM/UIM, premises liability, negligent security, insurance "
    "coverage or insurer bad faith, trucking or commercial motor carriers, apportionment of "
    "fault, tort damages or medical causation, wrongful death, products liability, dram shop, "
    "spoliation, Georgia tort reform (SB 68), expert or Daubert issues, or a civil procedure "
    "or evidence rule of broad practical importance to civil defense litigators.\n\n"
    "Mark relevant=false if the opinion only MENTIONS such a topic in passing without "
    "deciding anything about it, if it is a routine and fact-bound application of a settled "
    "rule, or if it is otherwise out of scope (ordinary commercial or contract disputes, "
    "landlord-tenant, family, criminal, and the like). Default to false on a close call.\n\n"
    "Output ONLY a JSON object with keys: relevant (true or false), significance ('high', "
    "'medium', or 'low'), areas (a list of codes from: coverage, badfaith, auto, premises, "
    "negsec, expert, procedure, damages), note (one or two sentences telling the next reviewer "
    "exactly what in the opinion is relevant and worth summarizing, especially if it is "
    "buried), reason (a few words). If relevant is false, areas and note may be empty."
)

SYSTEM = (
    "You are the editor of a CURATED, NARROW feed of new Georgia appellate decisions for an "
    "insurance-defense and civil-litigation audience. Relevance and significance matter far "
    "more than coverage. You are given the full text of one opinion, and a triage note from a "
    "prior reviewer pointing to what is relevant. Decide whether it earns a place in the feed, "
    "and if so write a short neutral digest.\n\n"
    "INCLUDE (relevant=true) only if BOTH are true:\n"
    "  (1) Nexus. It involves one or more of: auto or UM/UIM, premises liability, negligent "
    "security, insurance coverage or insurer bad faith, trucking or commercial motor carriers, "
    "apportionment of fault, tort damages or medical causation, wrongful death, products "
    "liability, dram shop, spoliation, Georgia tort reform (SB 68), or expert or Daubert "
    "issues; OR it is a civil procedure or evidence decision that announces or clarifies a "
    "rule of broad, practical importance to civil defense litigators.\n"
    "  (2) Significance. It actually decides or clarifies something a practitioner would want "
    "to know. A routine, fact-bound application of a settled rule does not qualify.\n\n"
    "EXCLUDE (relevant=false): criminal, habeas, family or domestic, juvenile or dependency, "
    "probate or wills, tax, workers' compensation, attorney discipline or bar admission, "
    "election, zoning, and governmental matters with no tort or insurance angle; landlord-"
    "tenant and dispossessory cases; ordinary commercial, contract, business-tort, or debt-"
    "collection disputes with no insurance or personal-injury nexus, unless the holding "
    "establishes an evidentiary or procedural rule of broad importance to civil defense "
    "practice; routine jurisdictional dispositions that merely apply a settled appellate rule "
    "to the facts (late notice of appeal, non-final order without a Rule 54(b) certificate, "
    "wrong appeal route, failure to file a brief, dismissal for want of prosecution), unless "
    "the opinion announces or clarifies a rule of broader significance; and one-line orders "
    "that merely grant or deny an application. Default to EXCLUSION on close calls.\n\n"
    "If you INCLUDE it, write the digest in this house style:\n"
    "  - A 2 to 4 sentence synopsis, then a separate one-sentence reason it matters.\n"
    "  - Neutral reporter voice. Lowercase party roles (plaintiff, defendant, insurer).\n"
    "  - State the disposition (affirmed; reversed; vacated and remanded; affirmed in part, "
    "reversed in part; appeal dismissed; and so on).\n"
    "  - Be conservative. Describe only what the opinion holds. Do not overstate.\n"
    "  - Do NOT invent or include any case citations or reporter cites. Refer to the case by "
    "party name only. A statutory cite in the form O.C.G.A. section X is fine only if the "
    "opinion itself uses it.\n"
    "  - No em dashes. Use commas and periods. Use the Oxford comma. Write 'about' not "
    "'approximately'.\n\n"
    "Field rules:\n"
    "  - court: 'scotga' for the Supreme Court of Georgia, 'ctapp' for the Court of Appeals of Georgia.\n"
    "  - division: the Court of Appeals division if the opinion states one (for example "
    "'First Division'), otherwise null.\n"
    "  - dockets: a list of docket numbers as strings.\n"
    "  - disposition: a short lowercase phrase.\n"
    "  - areas: one or more codes from EXACTLY this set, using only codes that genuinely fit: "
    "coverage, badfaith, auto, premises, negsec, expert, procedure, damages.\n"
    "  - significance: 'high', 'medium', or 'low'. If you would rate it 'low', set relevant=false instead.\n"
    "  - confidence: 'high', 'medium', or 'low'.\n\n"
    "Output ONLY a JSON object, no markdown and no commentary, with these keys: relevant, "
    "court, division, dockets, disposition, areas, name, synopsis, why, significance, "
    "confidence. If relevant is false, the remaining fields may be empty."
)


def cl_headers():
    h = {"User-Agent": "horowitz.law Georgia Appellate Watch"}
    if CL_TOKEN:
        h["Authorization"] = "Token " + CL_TOKEN
    return h


CL_RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}


def cl_get(path, deadline=None):
    url = path if path.startswith("http") else "https://www.courtlistener.com" + path
    last = None
    for attempt in range(4):
        if deadline and time.time() > deadline:
            raise TimeoutError("courtlistener deadline exceeded")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cl_headers()), timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in CL_RETRY_STATUS and attempt < 3:
                wait = _retry_after(e) or min(4 * (attempt + 1), 20)
                if deadline and time.time() + wait > deadline:
                    raise
                _dbg("courtlistener HTTP %s, retrying in %ss" % (e.code, wait))
                time.sleep(wait); continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < 3:
                wait = min(4 * (attempt + 1), 20)
                if deadline and time.time() + wait > deadline:
                    raise
                _dbg("courtlistener network error (%s), retrying in %ss" % (getattr(e, "reason", e), wait))
                time.sleep(wait); continue
            raise
    if last:
        raise last


def search_court(court, since, deadline=None):
    params = {"type": "o", "court": court, "filed_after": since,
              "stat_Published": "on", "order_by": "dateFiled desc", "page_size": "50"}
    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params)
    out, pages = [], 0
    while url and pages < 6:
        if deadline and time.time() > deadline:
            break
        data = cl_get(url, deadline)
        out += data.get("results", [])
        url = data.get("next")
        pages += 1
        time.sleep(1)
    return out


def cluster_id_of(r):
    if r.get("cluster_id"):
        return int(r["cluster_id"])
    m = re.search(r"/opinion/(\d+)/", r.get("absolute_url", "") or "")
    return int(m.group(1)) if m else None


def snippet_of(r):
    ops = r.get("opinions") or []
    if ops and isinstance(ops[0], dict):
        return ops[0].get("snippet") or ""
    return ""


def opinion_id_of(r):
    ops = r.get("opinions") or []
    if ops and isinstance(ops[0], dict) and ops[0].get("id"):
        return ops[0]["id"]
    sib = r.get("sibling_ids") or []
    if sib:
        return sib[0]
    cid = cluster_id_of(r)
    if cid:
        cl = cl_get("/api/rest/v4/clusters/%d/" % cid)
        for s in (cl.get("sub_opinions") or []):
            m = re.search(r"/opinions/(\d+)/", s) if isinstance(s, str) else None
            if m:
                return int(m.group(1))
    return None


def opinion_text(oid):
    o = cl_get("/api/rest/v4/opinions/%s/" % oid)
    for f in ("plain_text", "html_with_citations", "html", "xml_harvard", "html_lawbox", "html_columbia"):
        v = o.get(f)
        if v:
            if f != "plain_text":
                v = re.sub(r"<[^>]+>", " ", v)
                v = html.unescape(v)
            return re.sub(r"[ \t]+", " ", v).strip()
    return ""


def parse_json(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _dbg(msg):
    if DEBUG:
        print("  . " + msg)


def _retry_after(e):
    try:
        v = e.headers.get("retry-after")
        return int(float(v)) if v else 0
    except Exception:
        return 0


RETRY_STATUS = {429, 500, 502, 503, 529}


def anthropic_json(body, label="call"):
    """POST to the Messages API. Retries 429 and 5xx with backoff, and on a final
    failure raises with the API's own error body so the cause names itself."""
    model = body.get("model", "?")
    last = None
    for attempt in range(5):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json", "x-api-key": KEY,
                         "anthropic-version": VERSION},
                method="POST")
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.loads(r.read().decode("utf-8"))
            _dbg("%s %s ok in %.1fs (attempt %d)" % (label, model, time.time() - t0, attempt + 1))
            txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            try:
                return parse_json(txt)
            except Exception as pe:
                raise RuntimeError("%s %s returned unparseable JSON: %s | head=%r"
                                   % (label, model, pe, txt[:200]))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last = "%s %s -> HTTP %s: %s" % (label, model, e.code, (detail[:600] or e.reason))
            if e.code in RETRY_STATUS and attempt < 4:
                wait = _retry_after(e) or min(2 ** attempt * 2, 30)
                _dbg("%s HTTP %s, retrying in %ss" % (label, e.code, wait))
                time.sleep(wait); continue
            raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError) as e:
            last = "%s %s -> network error: %s" % (label, model, getattr(e, "reason", e))
            if attempt < 4:
                wait = min(2 ** attempt * 2, 30)
                _dbg("%s network error, retrying in %ss" % (label, wait))
                time.sleep(wait); continue
            raise RuntimeError(last)
    raise RuntimeError(last or (label + " failed"))


def screen(name, docket, snippet):
    user = "Case name: %s\nDocket: %s\nOpening excerpt:\n%s" % (name, docket, (snippet or "")[:1500])
    return anthropic_json({"model": SCREEN_MODEL, "max_tokens": 256, "system": SCREEN_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "screen")


def triage(name, docket, text):
    user = "Case name: %s\nDocket: %s\n\nFULL OPINION:\n%s" % (name, docket, text[:MAXCHARS])
    return anthropic_json({"model": TRIAGE_MODEL, "max_tokens": 1024, "system": TRIAGE_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "triage")


def summarize(court_id, name, docket, date_filed, text, note):
    user = ("Court (CourtListener id): %s\nCase name: %s\nDocket: %s\nDate filed: %s\n\n"
            "Triage note (what a prior reviewer flagged as relevant): %s\n\n"
            "OPINION TEXT (may be truncated):\n%s"
            % (court_id, name, docket, date_filed, note or "(none)", text[:MAXCHARS]))
    # Opus reasoning effort (high|medium|low) trades accuracy against latency and cost; medium is
    # ample for a short digest. Sent only to the summarizer, and only when EFFORT is non-empty.
    body = {"model": MODEL, "max_tokens": OUT_TOKENS, "system": SYSTEM,
            "messages": [{"role": "user", "content": user}]}
    if EFFORT:
        body["effort"] = EFFORT
    return anthropic_json(body, "summarize")


def main():
    if not KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)

    # The PR step reads PR_PATH as its body. Guarantee the file exists on every exit
    # path, including the no-candidates early return, so it never fails on a missing file.
    # It is gitignored and not in the PR add-paths, so a no-op run writes it and opens no PR.
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No update this run.\n")

    entries = json.load(open(JSON_PATH, encoding="utf-8")) if os.path.exists(JSON_PATH) else []
    have = {int(e["cluster_id"]) for e in entries if e.get("cluster_id")}

    state = {}
    if os.path.exists(STATE_PATH):
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    seen = set(int(x) for x in state.get("seen_clusters", []))
    last = state.get("last_filed")
    if last:
        since = (datetime.date.fromisoformat(last) - datetime.timedelta(days=2)).isoformat()
    else:
        since = (datetime.date.today() - datetime.timedelta(days=LOOKBACK)).isoformat()

    run_start = time.time()
    search_deadline = run_start + SEARCH_BUDGET
    results = []
    for court in COURTS:
        if time.time() > search_deadline:
            print("  ! search budget reached (%ds); skipping remaining courts" % SEARCH_BUDGET)
            break
        try:
            results += search_court(court, since, search_deadline)
        except Exception as e:
            print("  ! courtlistener search failed for %s: %s" % (court, e))
    if not results:
        print("no candidates returned from courtlistener "
              "(search timed out or rate-limited); nothing written this run.")
        return
    cand, ids = [], set()
    for r in results:
        cid = cluster_id_of(r)
        if not cid or cid in have or cid in seen or cid in ids:
            continue
        ids.add(cid)
        cand.append(r)
    cand.sort(key=lambda r: (r.get("dateFiled") or "", cluster_id_of(r)), reverse=True)
    cand = cand[:MAX_RUN]
    print("since %s | candidates: %d | tiers: screen=%s triage=%s summarize=%s"
          % (since, len(cand), SCREEN_MODEL or "off", TRIAGE_MODEL or "off", MODEL))

    added, flagged, skipped = [], [], []
    evaluated, n_screen, n_triage, n_opus = set(), 0, 0, 0
    consec = 0
    for r in cand:
        if time.time() - run_start > BUDGET_SEC:
            print("  ! time budget reached (%ds) after %d evaluated; finalizing with what is collected"
                  % (BUDGET_SEC, len(evaluated)))
            break
        cid = cluster_id_of(r)
        name = r.get("caseName") or r.get("caseNameFull") or ""
        court_id = r.get("court_id") or (COURTS[0])
        docket = r.get("docketNumber") or ""
        date_filed = (r.get("dateFiled") or "")[:10]
        url = "https://www.courtlistener.com" + (r.get("absolute_url") or "")
        try:
            # Tier 1: cheap excerpt screen
            if SCREEN_MODEL:
                n_screen += 1
                s = screen(name, docket, snippet_of(r))
                if not s.get("pass"):
                    skipped.append((name, "screen: %s" % (s.get("reason") or "not a fit")))
                    consec = 0; evaluated.add(cid); continue
                time.sleep(0.4)
            # full text, fetched once and reused by tiers 2 and 3
            oid = opinion_id_of(r)
            text = opinion_text(oid) if oid else ""
            if not text:
                skipped.append((name, "no opinion text available")); consec = 0; continue
            time.sleep(0.4)
            # Tier 2: full-read relevance gate
            note = ""
            if TRIAGE_MODEL:
                n_triage += 1
                t = triage(name, docket, text)
                if not t.get("relevant") or (t.get("significance") or "").lower() == "low":
                    skipped.append((name, "triage: %s" % (t.get("reason") or "not relevant")))
                    consec = 0; evaluated.add(cid); continue
                note = t.get("note") or ""
                time.sleep(0.4)
            # Tier 3: high-effort public summary
            n_opus += 1
            v = summarize(court_id, name, docket, date_filed, text, note)
            consec = 0
            evaluated.add(cid)
        except Exception as e:
            print("  ! error on cluster %s (%s): %s" % (cid, name, e))
            consec += 1
            if consec >= BREAKER:
                print("  ! %d consecutive failures; stopping early (API likely rate-limited). "
                      "Unevaluated candidates roll to the next run." % consec)
                break
            continue  # leave unseen so it is retried next run

        if not v.get("relevant"):
            skipped.append((name, "summarizer: not relevant")); continue
        if (v.get("significance") or "").lower() == "low":
            skipped.append((name, "summarizer: low significance")); continue

        areas = [a for a in (v.get("areas") or []) if a in VALID_AREAS]
        if not areas:
            skipped.append((name, "no recognized practice area")); continue
        court = COURT_MAP.get(court_id) or (v.get("court") if v.get("court") in ("scotga", "ctapp") else None)
        if not court:
            skipped.append((name, "unrecognized court id %s" % court_id)); continue
        dockets = [str(d).strip() for d in (v.get("dockets") or []) if str(d).strip()] or ([docket] if docket else [""])
        disp = (v.get("disposition") or "").strip().lower()
        synopsis = (v.get("synopsis") or "").strip()
        why = (v.get("why") or "").strip()

        entry = {"cluster_id": cid, "name": (v.get("name") or name).strip(), "court": court,
                 "division": (v.get("division") or None), "date": date_filed, "dockets": dockets,
                 "disposition": disp, "areas": areas, "url": url, "synopsis": synopsis, "why": why}

        reasons = []
        if (v.get("confidence") or "").lower() == "low":
            reasons.append("low confidence")
        if CITE_RE.search(synopsis) or CITE_RE.search(why):
            reasons.append("contains a reporter-style citation")
        if not disp:
            reasons.append("no disposition")
        if not synopsis or not why:
            reasons.append("empty synopsis or reason")
        if reasons:
            flagged.append((entry["name"], reasons))

        added.append(entry)
        print("  + %s [%s] %s (sig=%s)" % (entry["name"], ",".join(areas), disp, v.get("significance")))

    lines = ["## Georgia Appellate Watch: %d new opinion(s)" % len(added), ""]
    for e in added:
        cl = render.COURT_LABELS[e["court"]]
        lines.append("- **%s** (%s, %s): %s. areas: %s. Read: %s"
                     % (e["name"], cl, e["date"], e["disposition"] or "(none)", ", ".join(e["areas"]), e["url"]))
        fr = dict(flagged).get(e["name"])
        if fr:
            lines.append("  - review: %s" % "; ".join(fr))
    if skipped:
        lines += ["", "Screened or dropped this run (not added):"]
        lines += ["- %s: %s" % (n, why) for n, why in skipped]
    if not added:
        lines += ["", "No new relevant opinions this run."]
    pr_body = "\n".join(lines) + "\n"
    funnel = "screened %d, triaged %d, summarized %d" % (n_screen, n_triage, n_opus)

    if DRY_RUN:
        print("\n--- DRY RUN, nothing written (%s) ---\n%s" % (funnel, pr_body)); return

    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write(pr_body)

    if not added:
        print("no new opinions; files unchanged (%s, dropped %d)" % (funnel, len(skipped))); return

    entries += added
    json.dump(entries, open(JSON_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    state["last_filed"] = max(e["date"] for e in entries)
    state["seen_clusters"] = sorted(seen | evaluated | have | {e["cluster_id"] for e in added})
    state["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = render.render(entries)
    print("rendered %d entries; added %d, flagged %d (%s, dropped %d)"
          % (n, len(added), len(flagged), funnel, len(skipped)))


if __name__ == "__main__":
    main()
