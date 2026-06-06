#!/usr/bin/env python3
"""Georgia Appellate Watch updater.

Daily pipeline, standard library only:
  1. Ask CourtListener for new published Georgia appellate opinions since the last run.
  2. For each one not seen before, fetch its text and ask Claude whether it is
     relevant to a Georgia insurance-defense / civil-litigation audience and, if so,
     to write a short synopsis in the house style.
  3. Append the keepers to opinions.json, update opinions_state.json, and re-render
     opinions.html and opinions.xml via scripts/render.py.
  4. Write scripts/pr_body.md summarizing the run for the pull request.

Run from the repo root: `python scripts/update.py`.
No third-party packages. Network: CourtListener REST v4 + Anthropic Messages API.

Environment:
  ANTHROPIC_API_KEY     required
  COURTLISTENER_TOKEN   optional (raises CourtListener rate limits)
  OPINIONS_MODEL        Claude model id (default below; confirm current id in API docs)
  OPINIONS_COURTS       CourtListener court ids (default "ga,gactapp")
  OPINIONS_LOOKBACK     fallback look-back window in days when state is empty (default 21)
  OPINIONS_MAX          max opinions evaluated per run (default 25)
  OPINIONS_MAXCHARS     opinion text characters sent to the model (default 14000)
  DRY_RUN               if set to 1, evaluate and print but write nothing
"""
import os, re, sys, json, time, html, datetime
import urllib.request, urllib.parse, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import render  # single source of truth renderer

JSON_PATH  = os.path.join(REPO, "opinions.json")
STATE_PATH = os.path.join(REPO, "opinions_state.json")
PR_PATH    = os.path.join(REPO, "scripts", "pr_body.md")

KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
CL_TOKEN = os.environ.get("COURTLISTENER_TOKEN", "")
MODEL    = os.environ.get("OPINIONS_MODEL", "claude-sonnet-4-6")
VERSION  = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
COURTS   = [c.strip() for c in os.environ.get("OPINIONS_COURTS", "ga,gactapp").split(",") if c.strip()]
LOOKBACK = int(os.environ.get("OPINIONS_LOOKBACK", "21"))
MAX_RUN  = int(os.environ.get("OPINIONS_MAX", "25"))
MAXCHARS = int(os.environ.get("OPINIONS_MAXCHARS", "14000"))
DRY_RUN  = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")

COURT_MAP   = {"ga": "scotga", "gactapp": "ctapp"}
VALID_AREAS = set(render.AREA_LABELS)
CITE_RE = re.compile(r"\b\d+\s+(?:Ga\.?\s*App\.?|Ga\.?|S\.?\s*E\.?\s*2d|S\.?\s*E\.?|U\.?\s*S\.?|S\.?\s*Ct\.?|F\.?(?:2d|3d|4th)?|F\.?\s*Supp\.?)\s+\d+", re.I)

SYSTEM = (
    "You are a legal editor maintaining a curated feed of new Georgia appellate opinions "
    "for an insurance-defense audience. Given one opinion, decide whether it is relevant, "
    "and if so write a short neutral digest.\n\n"
    "RELEVANT means a Georgia civil case touching any of: auto and UM or UIM, premises "
    "liability, negligent security, insurance coverage, insurer bad faith, apportionment of "
    "fault, tort reform (SB 68), expert or Daubert issues, or civil damages. Also relevant: a "
    "civil procedure ruling likely to matter to defense litigators (service, dismissal, "
    "default, summary judgment, discovery sanctions).\n"
    "NOT RELEVANT (set relevant=false): criminal, habeas, family or domestic, juvenile, "
    "probate or wills, tax, workers' compensation administrative appeals, attorney discipline "
    "or bar admission, election, and zoning or governmental matters with no tort or insurance "
    "angle.\n\n"
    "If relevant, write the digest in this house style:\n"
    "- A 2 to 4 sentence synopsis, then a separate one-sentence reason it matters.\n"
    "- Neutral reporter voice. Lowercase party roles (plaintiff, defendant, insurer).\n"
    "- State the disposition (affirmed; reversed; vacated and remanded; affirmed in part, "
    "reversed in part; appeal dismissed; and so on).\n"
    "- Be conservative. Describe only what the opinion holds. Do not overstate.\n"
    "- Do NOT invent or include any case citations or reporter cites. Refer to the case by "
    "party name only. A statutory cite in the form O.C.G.A. section X is fine only if the "
    "opinion itself uses it.\n"
    "- No em dashes. Use commas and periods. Use the Oxford comma. Write 'about' not "
    "'approximately'.\n\n"
    "Field rules:\n"
    "- court: 'scotga' for the Supreme Court of Georgia, 'ctapp' for the Court of Appeals of Georgia.\n"
    "- division: the Court of Appeals division if the opinion states one (for example "
    "'First Division'), otherwise null.\n"
    "- dockets: a list of docket numbers as strings.\n"
    "- disposition: a short lowercase phrase.\n"
    "- areas: one or more codes from EXACTLY this set, using only codes that genuinely fit: "
    "coverage, badfaith, auto, premises, negsec, expert, procedure, damages.\n"
    "- name: the case name in the form 'Party v. Party'.\n"
    "- confidence: 'high', 'medium', or 'low'.\n\n"
    "Output ONLY a JSON object, no markdown and no commentary, with these keys: relevant, "
    "court, division, dockets, disposition, areas, name, synopsis, why, confidence. "
    "If relevant is false, the remaining fields may be empty."
)


def cl_headers():
    h = {"User-Agent": "horowitz.law Georgia Appellate Watch"}
    if CL_TOKEN:
        h["Authorization"] = "Token " + CL_TOKEN
    return h


def cl_get(path):
    url = path if path.startswith("http") else "https://www.courtlistener.com" + path
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cl_headers()), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1)); continue
            raise


def search_court(court, since):
    params = {"type": "o", "court": court, "filed_after": since,
              "stat_Published": "on", "order_by": "dateFiled desc", "page_size": "50"}
    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params)
    out, pages = [], 0
    while url and pages < 6:
        data = cl_get(url)
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


def summarize(court_id, case_name, docket, date_filed, text):
    user = ("Court (CourtListener id): %s\nCase name: %s\nDocket: %s\nDate filed: %s\n\n"
            "OPINION TEXT (may be truncated):\n%s" % (court_id, case_name, docket, date_filed, text[:MAXCHARS]))
    body = {"model": MODEL, "max_tokens": 1024, "system": SYSTEM,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "x-api-key": KEY, "anthropic-version": VERSION},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return parse_json(txt)


def main():
    if not KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)

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

    # gather candidates across courts, newest first, de-duplicated
    results = []
    for court in COURTS:
        results += search_court(court, since)
    cand, ids = [], set()
    for r in results:
        cid = cluster_id_of(r)
        if not cid or cid in have or cid in seen or cid in ids:
            continue
        ids.add(cid)
        cand.append(r)
    cand.sort(key=lambda r: (r.get("dateFiled") or "", cluster_id_of(r)), reverse=True)
    cand = cand[:MAX_RUN]
    print("since %s | candidates: %d" % (since, len(cand)))

    added, flagged, skipped, evaluated = [], [], [], set()
    for r in cand:
        cid = cluster_id_of(r)
        name = r.get("caseName") or r.get("caseNameFull") or ""
        court_id = r.get("court_id") or (COURTS[0])
        docket = r.get("docketNumber") or ""
        date_filed = (r.get("dateFiled") or "")[:10]
        url = "https://www.courtlistener.com" + (r.get("absolute_url") or "")
        try:
            oid = opinion_id_of(r)
            text = opinion_text(oid) if oid else ""
            if not text:
                skipped.append((name, "no opinion text available")); continue
            time.sleep(1)
            v = summarize(court_id, name, docket, date_filed, text)
            evaluated.add(cid)
        except Exception as e:
            print("  ! error on cluster %s (%s): %s" % (cid, name, e))
            continue  # leave unseen so it is retried next run

        if not v.get("relevant"):
            skipped.append((name, "model marked not relevant")); continue

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
        print("  + %s [%s] %s" % (entry["name"], ",".join(areas), disp))

    # PR body summary
    lines = ["## Georgia Appellate Watch: %d new opinion(s)" % len(added), ""]
    for e in added:
        cl = render.COURT_LABELS[e["court"]]
        lines.append("- **%s** (%s, %s): %s. areas: %s. Read: %s"
                     % (e["name"], cl, e["date"], e["disposition"] or "(none)", ", ".join(e["areas"]), e["url"]))
        fr = dict(flagged).get(e["name"])
        if fr:
            lines.append("  - review: %s" % "; ".join(fr))
    if skipped:
        lines += ["", "Skipped (logged, not added):"]
        lines += ["- %s: %s" % (n, why) for n, why in skipped]
    if not added:
        lines += ["", "No new relevant opinions this run."]
    pr_body = "\n".join(lines) + "\n"

    if DRY_RUN:
        print("\n--- DRY RUN, nothing written ---\n" + pr_body); return

    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write(pr_body)

    if not added:
        print("no new opinions; leaving files unchanged"); return

    entries += added
    json.dump(entries, open(JSON_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    state["last_filed"] = max(e["date"] for e in entries)
    state["seen_clusters"] = sorted(seen | evaluated | have | {e["cluster_id"] for e in added})
    state["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = render.render(entries)
    print("rendered %d entries; added %d, flagged %d, skipped %d" % (n, len(added), len(flagged), len(skipped)))


if __name__ == "__main__":
    main()
