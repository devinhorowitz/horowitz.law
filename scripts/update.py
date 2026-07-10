#!/usr/bin/env python3
"""Georgia Appellate Watch updater (staged funnel).

Daily pipeline, standard library only. Four model tiers, cheapest first, so the
expensive model only ever touches confirmed keepers:

  Tier 1  SCREEN   (Haiku)  reads the case name and opening excerpt only and drops
                            the categorically unrelated (criminal, family, juvenile,
                            probate, tax, bar, election, dispossessory) and one-line
                            application or clerk orders. Permissive: anything civil or
                            ambiguous passes, so nothing relevant is dropped on a glance.
  Tier 1.5 PRETRIAGE (Haiku) reads the FULL opinion at the same permissive bar and drops
                            only what the full text now shows cannot belong, so the costly
                            Sonnet read lands only on plausible keepers. High-recall:
                            anything in scope or ambiguous still passes through to triage.
  Tier 2  TRIAGE   (Sonnet) reads the FULL opinion and decides, against a narrow bar,
                            whether it genuinely decides or clarifies something relevant,
                            catching holdings that are not visible from the opening.
  Tier 3  SUMMARIZE(Opus)   reads the FULL opinion plus the triage note and writes the
                            public-facing card in the house style. Final backstop: it can
                            still decline. Opus 4.8 takes no extended-thinking budget and
                            no "effort" parameter; the summarizer runs at the model default.

Keepers are appended to opinions.json, opinions_state.json is updated, opinions.html
and opinions.xml are re-rendered, and scripts/pr_body.md is written for the pull request.

Run from the repo root: `python scripts/update.py`. No third-party packages.

Environment:
  ANTHROPIC_API_KEY        required
  COURTLISTENER_TOKEN      optional (raises CourtListener rate limits)
  OPINIONS_MODEL           Tier 3 summarizer (default claude-opus-4-8)
  OPINIONS_TRIAGE_MODEL    Tier 2 full-read gate (default claude-sonnet-5). "" disables it.
  OPINIONS_SCREEN_MODEL    Tier 1 excerpt screen (default claude-haiku-4-5-20251001). "" disables it.
  OPINIONS_PRETRIAGE_MODEL Tier 1.5 full-read screen (default claude-haiku-4-5-20251001); a cheap full read before triage. "" disables it.
  OPINIONS_CROSSCHECK_MODEL  fidelity check on each drafted card (default = the triage model). "" disables it.
  OPINIONS_COMPLETENESS_MODEL  completeness check on each drafted card (default = the triage model). "" disables it.
  OPINIONS_AUDIT_MODEL     confirms a flagged adverse-treatment event (default = the summarizer model, OPINIONS_MODEL).
  OPINIONS_COURTS          CourtListener court ids (default "ga,gactapp,ca11,scotus")
  OPINIONS_JURISDICTION    active jurisdiction key from jurisdictions.py (default "ga")
  OPINIONS_LOOKBACK        fallback look-back window in days when state is empty (default 21)
  OPINIONS_MAX             max opinions evaluated per run (code default 25; the daily workflow raises it to 80 for heavy filing days)
  OPINIONS_SEEN_CAP        max cluster ids kept in opinions_state.json seen list (default 5000; bounds state-file growth)
  OPINIONS_MAXCHARS        opinion characters sent to triage and summarizer (default 60000)
  OPINIONS_MAX_TOKENS      summarizer output token cap (default 4096)
  DRY_RUN                  if set to 1, evaluate and print but write nothing
  OPINIONS_DEBUG           if set to 1, log every model call and full API error bodies
  OPINIONS_BUDGET_SEC      wall-clock cap on the candidate loop in seconds (default 480)
  OPINIONS_BREAKER         stop after this many consecutive API failures (default 4)
  OPINIONS_SEARCH_BUDGET_SEC  wall-clock cap on the CourtListener search phase (default 120)
  ANTHROPIC_STATUS         status-page preflight: on (log + skip on a confirmed API outage),
                           warn (log only), or off (no check). Default on. Fail-open on any error.
  ANTHROPIC_STATUS_URL     status summary endpoint (default https://status.claude.com/api/v2/summary.json)
  CL_PER_MINUTE / CL_PER_HOUR / CL_PER_DAY / CL_RATE_MARGIN  CourtListener REST budget (see cl_rate.py)
"""
import os, re, sys, json, time, html, datetime, io, copy
import cl_rate           # shared CourtListener REST budget (limits, pacing, defer)
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
import siteconfig        # shared practice-area taxonomy and site config

AREA_CODES_STR = ", ".join(siteconfig.AREA_CODES)  # prompt-injected; single source of truth

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import render          # single source of truth renderer
import treatment_core  # shared treatment-flag model (the forward escalation writes it)
import skill_alert     # alert-out: extend the treats watch-list to skill-relied authorities
import safeio          # crash-safe atomic writes
import jurisdictions   # per-jurisdiction court config (court map, labels, patterns)
import official_ga     # resolves a scotga card's official gasupreme.us opinion PDF (fail-open)
import review_store     # two-lane routing: stages held cases, tracks the pending-review ledger


class ConfigError(RuntimeError):
    """A non-transient, operator-fixable failure: a bad/expired API key, a
    depleted credit balance, or a retired model id. Distinct from a transient
    error so the run can stop fast and exit non-zero (surfacing it by email)
    instead of silently deferring like it does for an outage or a rate limit."""

JSON_PATH  = os.path.join(REPO, "opinions.json")
STATE_PATH = os.path.join(REPO, "opinions_state.json")
LOG_PATH   = os.path.join(REPO, "opinions_pipeline_log.jsonl")  # append-only per-run health log (observability)
REJECT_PATH = os.path.join(REPO, "opinions_rejections.jsonl")  # append-only log of candidates the screen or triage dropped, for periodic recall review
SA_MANIFEST_PATH = os.path.join(REPO, "skill-authorities.json")   # skill-authority manifest (alert-out join key; absent = watch inactive)
SA_STATE_PATH    = os.path.join(REPO, "skill_alert_state.json")   # per-authority adverse-treatment record (state; rides the PR / straight to main like opinions_state.json)
REJECT_CAP  = int(os.environ.get("OPINIONS_REJECT_CAP", "5000"))  # keep only the most recent N rejection records so the committed log stays bounded
PR_PATH    = os.path.join(REPO, "scripts", "pr_body.md")            # combined run body (DRY_RUN log)
AUTO_PR_PATH   = os.path.join(REPO, "scripts", "pr_body_auto.md")   # auto-lane PR body (additive, auto-merged)
REVIEW_PR_PATH = os.path.join(REPO, "scripts", "pr_body_review.md") # review-lane PR body (held cases, per-case /veto)

KEY          = os.environ.get("ANTHROPIC_API_KEY", "")
CL_TOKEN     = os.environ.get("COURTLISTENER_TOKEN", "")
MODEL        = os.environ.get("OPINIONS_MODEL", "claude-opus-4-8")
AUDIT_MODEL  = os.environ.get("OPINIONS_AUDIT_MODEL", MODEL)  # escalated treatment audit; Opus by default
TRIAGE_MODEL = os.environ.get("OPINIONS_TRIAGE_MODEL", "claude-sonnet-5")
SCREEN_MODEL = os.environ.get("OPINIONS_SCREEN_MODEL", "claude-haiku-4-5-20251001")
PRETRIAGE_MODEL = os.environ.get("OPINIONS_PRETRIAGE_MODEL", "claude-haiku-4-5-20251001")  # tier 1.5: cheap full-read screen before the Sonnet triage; "" disables
CROSSCHECK_MODEL = os.environ.get("OPINIONS_CROSSCHECK_MODEL", TRIAGE_MODEL)  # fidelity check on each card; a different model than the Opus summarizer so it is not grading its own work; "" disables
CROSSCHECK_TRIES = int(os.environ.get("OPINIONS_CROSSCHECK_TRIES", "3"))  # on a substantiated flag, re-ask up to this many times; a flag stands only on a majority, damping one-roll noise at temperature 1. 1 keeps grounding but disables consensus
COMPLETENESS_MODEL = os.environ.get("OPINIONS_COMPLETENESS_MODEL", TRIAGE_MODEL)  # completeness check on each card: flags a material holding in a covered area the card omits; a different model than the Opus summarizer; "" disables
COMPLETENESS_TRIES = int(os.environ.get("OPINIONS_COMPLETENESS_TRIES", "3"))  # like OPINIONS_CROSSCHECK_TRIES, for the completeness check: on a substantiated flag, re-ask up to this many times; a flag stands only on a majority. 1 keeps grounding but disables consensus
VERSION      = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
COURTS       = jurisdictions.COURTS         # CL ids the feed iterates (OPINIONS_COURTS narrows it)
LOOKBACK     = int(os.environ.get("OPINIONS_LOOKBACK", "21"))
MAX_RUN      = int(os.environ.get("OPINIONS_MAX", "25"))
SEEN_CAP     = int(os.environ.get("OPINIONS_SEEN_CAP", "5000"))  # cap on seen_clusters kept in state (bounds growth)
MAXCHARS     = int(os.environ.get("OPINIONS_MAXCHARS", "60000"))
PDF_MIN_CHARS= int(os.environ.get("OPINIONS_PDF_MIN_CHARS", "500"))  # below this, fall back from PDF to REST
OUT_TOKENS   = int(os.environ.get("OPINIONS_MAX_TOKENS", "4096"))
DRY_RUN      = os.environ.get("DRY_RUN", "") in ("1", "true", "True", "yes")
DEBUG        = os.environ.get("OPINIONS_DEBUG", "") in ("1", "true", "True", "yes")
BUDGET_SEC   = int(os.environ.get("OPINIONS_BUDGET_SEC", "480"))
BREAKER      = int(os.environ.get("OPINIONS_BREAKER", "4"))
SEARCH_BUDGET= int(os.environ.get("OPINIONS_SEARCH_BUDGET_SEC", "120"))
STATUS_URL   = os.environ.get("ANTHROPIC_STATUS_URL", "https://status.claude.com/api/v2/summary.json")
STATUS_MODE  = (os.environ.get("ANTHROPIC_STATUS", "on") or "on").strip().lower()  # on | warn | off

COURT_MAP   = jurisdictions.COURT_MAP       # CL court id -> our internal key
COURTS_ALL  = jurisdictions.COURTS_ALL      # full CL id set, ignoring the OPINIONS_COURTS override
VALID_KEYS  = jurisdictions.VALID_KEYS      # internal court keys (fallback validation)
VALID_AREAS = set(render.AREA_LABELS)
CITE_RE = jurisdictions.CITE_RE

SCREEN_SYSTEM = (
    "You are a fast first-pass screener for a curated feed of court decisions for a "
    "civil-litigation and insurance audience focused on Georgia. The feed covers the Georgia, Florida, and "
    "Alabama appellate courts, the U.S. Court of Appeals for the Eleventh Circuit, and the U.S. "
    "Supreme Court. Florida and Alabama are supplementary, so screen such a case on the same out-of-area "
    "grounds as any other, not for being from those states. "
    "You see only a case name and a short opening excerpt. Be permissive: your only job is to "
    "discard cases that cannot possibly belong, not to judge relevance.\n\n"
    "FAIL only if the case is clearly one of these: criminal (often captioned 'v. The State', 'v. State of Florida', 'v. State of Alabama', or "
    "'United States v.'), habeas or post-conviction (including 28 U.S.C. 2254 or 2255), "
    "immigration, prisoner civil rights, Social Security or veterans' benefits, family or "
    "domestic, juvenile or dependency ('In the Interest of'), probate or wills, tax, workers' "
    "compensation, attorney discipline or bar admission, election, or landlord-tenant or "
    "dispossessory; or it is a one-line order that merely grants or denies an application or "
    "dismisses for failure to file, with no merits.\n\n"
    "PASS everything else, including any general civil case and anything you are not sure "
    "about. A later step reads the full opinion, so when in doubt, PASS. "
    "Output ONLY a JSON object: {\"pass\": true or false, \"reason\": \"a few words\"}."
)

PRETRIAGE_SYSTEM = (
    "You are a cheap full-read screener for a curated feed of court decisions for a "
    "civil-litigation and insurance audience focused on Georgia. The feed covers the Georgia, Florida, and "
    "Alabama appellate courts, the U.S. Court of Appeals for the Eleventh Circuit, and the U.S. "
    "Supreme Court (Florida and Alabama are supplementary, dropped only on the same out-of-area grounds). A first pass has "
    "already discarded the obviously unrelated cases from the caption and opening; you now see the "
    "FULL opinion, and a stricter reviewer reads everything you pass, so your only job is to drop "
    "what the full text now makes clearly impossible, not to judge relevance. "
    "FAIL only if the full opinion is clearly one of these: criminal, habeas or post-conviction "
    "(28 U.S.C. 2254 or 2255), immigration, prisoner civil rights, Social Security or veterans' "
    "benefits, family or domestic, juvenile or dependency, probate or wills, tax, workers' "
    "compensation, attorney discipline or bar admission, election, landlord-tenant or "
    "dispossessory, bankruptcy with no coverage or tort nexus, patent or other intellectual "
    "property, or employment discrimination that announces no broad evidentiary or procedural "
    "rule; or it is a one-line or purely procedural order that decides nothing on the merits. "
    "PASS everything else, including any case that touches, even secondarily, auto or UM/UIM, "
    "premises liability, negligent security, insurance coverage or bad faith, trucking or motor "
    "carriers, apportionment, tort damages or medical causation, wrongful death, products "
    "liability, dram shop, spoliation, Georgia tort reform, expert or Daubert issues, arbitration, "
    "or a civil procedure or evidence question; and PASS anything you are not sure about. The next "
    "reviewer applies the narrow bar, so when in doubt, PASS. "
    'Output ONLY a JSON object: {"pass": true or false, "reason": "a few words"}.'
)

TRIAGE_SYSTEM = (
    "You are the second-stage reviewer for a CURATED, NARROW feed of court decisions for a "
    "civil-litigation and insurance audience focused on Georgia. The feed covers the Georgia, Florida, and "
    "Alabama appellate courts, the U.S. Court of Appeals for the Eleventh Circuit, and the U.S. "
    "Supreme Court. Florida and Alabama are supplementary: keep such a decision when it decides a point in "
    "the practice areas below, on the same terms as a Georgia one. A cheap first pass has already "
    "removed the obviously unrelated cases. You are given "
    "the FULL text of one opinion. Catch genuine relevance that a glance at the opening would "
    "miss, while keeping the feed narrow.\n\n"
    "Mark relevant=true only if the opinion DECIDES or CLARIFIES a point in one of these "
    "areas, even if that point is not apparent from the caption or opening and even if it is a "
    "secondary holding: auto or UM/UIM, premises liability, negligent security, insurance "
    "coverage or insurer bad faith, trucking or commercial motor carriers (including FAAAA "
    "preemption and broker liability), apportionment of fault, tort damages or medical "
    "causation, wrongful death, products liability, dram shop, spoliation, Georgia tort reform "
    "(SB 68), expert or Daubert issues, arbitration under the FAA, or a civil procedure or "
    "evidence rule of broad practical importance to civil litigators (including removal "
    "and diversity jurisdiction, class actions, and punitive-damages due process).\n\n"
    "For an Eleventh Circuit or U.S. Supreme Court opinion, apply this bar even more strictly: "
    "include it only if it decides or clarifies a point a Georgia civil-litigation or insurance practitioner would need to know in the areas above. "
    "Exclude the large federal docket that does not bear on this practice, including federal "
    "criminal, immigration, habeas and section 2255, prisoner civil rights, Social Security, "
    "patent and other intellectual property, bankruptcy with no coverage or tort nexus, "
    "employment discrimination (unless it announces a broad evidentiary or procedural rule), "
    "and administrative or regulatory matters.\n\n"
    "Mark relevant=false if the opinion only MENTIONS such a topic in passing without "
    "deciding anything about it, if it is a routine and fact-bound application of a settled "
    "rule, or if it is otherwise out of scope (ordinary commercial or contract disputes, "
    "landlord-tenant, family, criminal, and the like). In particular, a short order that just "
    "dismisses an appeal as untimely or for want of prosecution, or that grants or denies an "
    "application, is OUT even when the underlying case is an in-scope auto, premises, or tort "
    "matter, unless the order announces or clarifies a rule. Default to false on a close "
    "call.\n\n"
    "ADVERSE TREATMENT OF THE FEED. You may also be given a list of CASES ALREADY IN THE FEED "
    "(each as 'id: name'). Independently of relevance, check whether THIS opinion treats any of "
    "those listed cases NEGATIVELY: overrules, reverses, abrogates, holds it superseded by "
    "statute, limits or narrows the rule of, disapproves, or criticizes it as wrongly decided. "
    "Merely citing, following, or distinguishing a listed case on its facts is NOT negative. Use "
    "a LOW threshold: if you have any genuine doubt that the treatment might be negative, include "
    "it, because a later step confirms. List each as {id (the listed integer id), kind, note (a "
    "few words)}.\n\n"
    "Output ONLY a JSON object with keys: relevant (true or false), significance ('high', "
    "'medium', or 'low'), areas (a list of codes from: " + AREA_CODES_STR + "), note (one or two sentences telling the next reviewer "
    "exactly what in the opinion is relevant and worth summarizing, especially if it is buried, "
    "and flagging when the opinion decides more than one distinct salient holding), treats (a "
    "list of negatively-treated feed cases as described above, or an empty "
    "list), reason (a few words). If relevant is false, areas and note may be empty, but still "
    "fill treats when the opinion negatively treats a listed case."
)

SYSTEM = (
    "You are the editor of a CURATED, NARROW feed of new court decisions for a "
    "civil-litigation and insurance audience focused on Georgia. The feed covers the Georgia, Florida, and "
    "Alabama appellate courts, the U.S. Court of Appeals for the Eleventh Circuit, and the U.S. "
    "Supreme Court. Florida and Alabama are supplementary: a Florida or Alabama decision in the areas below belongs on the "
    "same terms as a Georgia one. "
    "Relevance and significance matter far more than coverage. You are given the full text of "
    "one opinion, and a triage note from a prior reviewer pointing to what is relevant. Decide "
    "whether it earns a place in the feed, and if so write a short neutral digest.\n\n"
    "INCLUDE (relevant=true) only if BOTH are true:\n"
    "  (1) Nexus. It involves one or more of: auto or UM/UIM, premises liability, negligent "
    "security, insurance coverage or insurer bad faith, trucking or commercial motor carriers "
    "(including FAAAA preemption and broker liability), apportionment of fault, tort damages or "
    "medical causation, wrongful death, products liability, dram shop, spoliation, Georgia tort "
    "reform (SB 68), expert or Daubert issues, or arbitration under the FAA; OR it is a civil "
    "procedure or evidence decision that announces or clarifies a rule of broad, practical "
    "importance to civil litigators (including removal and diversity jurisdiction, "
    "class actions, and punitive-damages due process).\n"
    "  (2) Significance. It actually decides or clarifies something a practitioner would want "
    "to know, regardless of which party prevailed; a holding that goes against an insurer, a defendant, a plaintiff, or an insured is exactly as eligible as one that favors them. A routine, fact-bound application of a settled rule does not qualify.\n\n"
    "For an Eleventh Circuit or U.S. Supreme Court opinion, apply the nexus even more strictly: "
    "include it only if it decides or clarifies a point a Georgia civil-litigation or insurance practitioner would need to know in the areas above.\n\n"
    "EXCLUDE (relevant=false): criminal, habeas and section 2255, immigration, prisoner civil "
    "rights, Social Security and veterans' benefits, patent and other intellectual property, "
    "family or domestic, juvenile or dependency, probate or wills, tax, workers' compensation, "
    "attorney discipline or bar admission, election, zoning, and governmental or regulatory "
    "matters with no tort or insurance angle; landlord-tenant and dispossessory cases; ordinary "
    "commercial, contract, business-tort, or debt-collection disputes with no insurance or "
    "personal-injury nexus, unless the holding establishes an evidentiary or procedural rule of "
    "broad importance to civil litigation practice; and routine procedural dispositions that "
    "merely apply a settled appellate rule to the facts (a short order dismissing an appeal as "
    "untimely or for want of prosecution, an appeal from a non-final order without a Rule 54(b) "
    "certificate, the wrong appeal route, or failure to file a brief), and one-line orders that "
    "merely grant or deny an application. These procedural dispositions are OUT regardless of "
    "what the underlying case is about: a one-page order dismissing an untimely appeal does not "
    "qualify merely because the case involves an auto collision, a premises injury, or another "
    "in-scope subject. Only a rule the order itself announces or clarifies can make it qualify, "
    "and a routine application of O.C.G.A. section 5-6-38 announces none. Default to EXCLUSION "
    "on close calls.\n\n"
    "If you INCLUDE it, write the digest in this house style:\n"
    "  - A 2 to 4 sentence synopsis, then a separate one-sentence reason it matters, written neutrally. State the decision's practical significance to anyone practicing in the area: the rule it establishes, clarifies, or changes, and the consequence that follows. Write it from no party's side. Do not frame the decision as helping or hurting plaintiffs, defendants, insurers, or insureds, and do not use words like win, loss, victory, blow, caution for carriers, defense-friendly, or plaintiff-friendly. Say what the decision does, not who it helps.\n"
    "  - Capture every material holding the opinion reaches, not only the most prominent one. First identify each issue the court independently resolves. A holding is separate and material when the court decides it as its own question, with its own reasoning and its own outcome, so that omitting it would misstate what the court decided. Two holdings count as separate whether or not they fall under different area codes: a duty-to-defend holding and a separate duty-to-indemnify holding, or two independent evidentiary rulings, are each their own holding even when both code to a single area. Put the first holding in 'synopsis', 'why', and 'areas', and put each further material holding in 'additional_holdings' as its own object, written to the same standard and neither subordinated nor dropped. A split disposition (for example affirmed in part and reversed in part, or vacated in part) almost always signals more than one resolved issue, so account for each. Do not split the successive steps of a single holding into several, do not treat a standard of review or a subsidiary sub-issue as its own holding, and tag each holding only with the areas it actually decides under, not every area the opinion mentions. Many opinions still decide only one material point, and those leave 'additional_holdings' empty.\n"
    "  - Neutral reporter voice. Lowercase party roles (plaintiff, defendant, insurer).\n"
    "  - State the disposition (affirmed; reversed; vacated and remanded; affirmed in part, "
    "reversed in part; appeal dismissed; and so on).\n"
    "  - Be conservative. Describe only what the opinion holds. Do not overstate.\n"
    "  - The text may contain a majority or per curiam opinion followed by separate "
    "concurrences and dissents. Summarize ONLY the opinion of the court (the majority "
    "or per curiam holding). Never state a concurrence's or dissent's position as the "
    "court's holding; note a significant dissent only as a dissent if it matters.\n"
    "  - Do NOT invent or include any case citations or reporter cites. Refer to the case by "
    "party name only. A statutory cite is fine only if the opinion itself uses it (for example "
    "O.C.G.A. section X, or a federal cite such as 28 U.S.C. section X).\n"
    "  - No em dashes. Use commas and periods. Use the Oxford comma. Write 'about' not "
    "'approximately'.\n\n"
    "Field rules:\n"
    "  - court: 'scotga' for the Supreme Court of Georgia, 'ctapp' for the Court of Appeals of "
    "Georgia, 'scotfl' for the Supreme Court of Florida, 'dcafl' for a Florida District Court of "
    "Appeal, 'scotal' for the Supreme Court of Alabama, 'civappal' for the Alabama Court of Civil "
    "Appeals, 'ca11' for the U.S. Court of Appeals for the Eleventh Circuit, 'scotus' for the "
    "U.S. Supreme Court.\n"
    "  - division: the Court of Appeals of Georgia division if the opinion states one (for "
    "example 'First Division'), otherwise null. Florida, Alabama, and federal opinions have no division.\n"
    "  - dockets: a list of docket numbers as strings.\n"
    "  - disposition: a short lowercase phrase.\n"
    "  - areas: one or more codes from EXACTLY this set, using only codes that genuinely fit: " + AREA_CODES_STR + ".\n"
    "  - additional_holdings: an empty list when the opinion decides only one material holding; otherwise a list "
    "of objects, one per salient holding beyond the first, each with 'areas' (codes from the same set), "
    "'synopsis' (2 to 4 sentences), and 'why' (one sentence), all under the same rules as above.\n"
    "  - significance: 'high', 'medium', or 'low'. If you would rate it 'low', set relevant=false instead.\n"
    "  - precedential: 'published' for a published, citable precedential decision; 'unpublished' if "
    "the court marked it not for publication or non-precedential (for example a 'DO NOT PUBLISH' or "
    "'NOT FOR PUBLICATION' designation, or an Eleventh Circuit unpublished opinion); 'physical precedent' "
    "for a Court of Appeals of Georgia opinion marked physical precedent only under Court of Appeals Rule "
    "33.2 (less than full concurrence in the division); 'unknown' only if the text gives no indication. "
    "Decide from the opinion's own designation; the publication-status metadata above is a hint, not "
    "controlling.\n"
    "  - first_impression: true ONLY when the opinion itself states that it resolves a question "
    "of first impression, or expressly states that no controlling precedent of the issuing court "
    "or of the state whose law governs decides the question. Never infer it from novelty alone. Default false.\n"
    "  - tort_reform: a TRACKER tag for Georgia's tort-reform storyline. true when EITHER (1) a holding construes or applies one of the reform acts of Georgia's recent tort-reform "
    "legislation: the 2025 SB 68 omnibus (for example O.C.G.A. sections 51-3-50 through 51-3-57 "
    "on negligent security, section 9-10-184 on anchoring, section 51-12-1.1 on medical damages, "
    "or its seatbelt, dismissal-timing, or bifurcation provisions), the 2025 SB 69 "
    "litigation-funding act, the 2024 SB 426 motor-carrier direct-action restriction, or the "
    "2022 HB 961 amendment to O.C.G.A. section 51-12-33, including a holding that decides whether or how one of those acts applies to a case or incident; OR (2) the decision is one that a reform act superseded, abrogated, or was enacted in direct response to. A passing mention is not enough, and a routine application of the PRE-reform version of a statute or framework that a reform act later displaced (construing O.C.G.A. section 51-12-33 before the 2022 amendment, or pre-SB 68 premises law, without more) is not by itself the storyline. This tag tracks Georgia only; it is always false on a Florida, Alabama, or federal opinion, even one that applies that state's own tort-reform statutes.\n"
    "  - law_applied: for a federal opinion only (ca11 or scotus), the body of substantive law "
    "the first holding turns on: 'federal' for a federal-question holding, or 'ga', 'fl', or "
    "'al' when the holding applies that state's substantive law, for example under Erie in "
    "diversity. null for any state-court opinion (Georgia, Florida, or Alabama).\n"
    "  - confidence: 'high', 'medium', or 'low'.\n\n"
    "Output ONLY a JSON object, no markdown and no commentary, with these keys: relevant, "
    "court, division, dockets, disposition, areas, name, synopsis, why, additional_holdings, "
    "significance, confidence, precedential, first_impression, tort_reform, law_applied. "
    "If relevant is false, the remaining fields may be empty."
)


UA = "horowitz.law Georgia Appellate Watch (contact: via horowitz.law)"


def cl_headers():
    h = {"User-Agent": UA}
    if CL_TOKEN:
        h["Authorization"] = "Token " + CL_TOKEN
    return h


CL_RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}


def _read_detail(e):
    """Pull the 'detail' string out of a CourtListener error body (the throttle
    message names the period and seconds to wait). The body can be read once."""
    try:
        return (json.loads(e.read().decode("utf-8")) or {}).get("detail", "") or ""
    except Exception:
        return ""


def cl_get(path, deadline=None):
    url = path if path.startswith("http") else "https://www.courtlistener.com" + path
    cl_rate.PACER.acquire(deadline)        # per-run budget + per-minute spacing; may defer
    last = None
    for attempt in range(4):
        if deadline and time.time() > deadline:
            raise TimeoutError("courtlistener deadline exceeded")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cl_headers()), timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                detail = _read_detail(e)
                kind, wait = cl_rate.PACER.penalize(detail, _retry_after(e))
                # A per-minute burst is worth a brief wait then retry; an hourly or
                # daily ceiling is not. Defer instead of sleeping on a long window.
                if kind == "short" and attempt < 3:
                    w = wait or min(4 * (attempt + 1), 15)
                    if deadline and time.time() + w > deadline:
                        raise cl_rate.RateBudgetExceeded("courtlistener throttled: %s" % (detail or "429"))
                    _dbg("courtlistener 429 (%s); waiting %ss then retry" % ((detail or "")[:60], w))
                    time.sleep(w); continue
                raise cl_rate.RateBudgetExceeded("courtlistener throttled: %s" % (detail or "429"))
            if e.code in CL_RETRY_STATUS and attempt < 3:
                wait = _retry_after(e) or min(4 * (attempt + 1), 20)
                if deadline and time.time() + wait > deadline:
                    raise
                _dbg("courtlistener HTTP %s, retrying in %ss" % (e.code, wait))
                time.sleep(wait); continue
            if e.code in (401, 403):
                print("  ! CourtListener AUTHENTICATION failed (HTTP %s). Check the COURTLISTENER_TOKEN secret." % e.code)
                raise ConfigError("courtlistener auth failed: HTTP %s" % e.code)
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


# CourtListener stores opinion PDFs as static files here; fetching one costs no REST
# quota and needs no token, the same Phase-2 path the daily pipeline reads PDFs from.
STORAGE = "https://storage.courtlistener.com/"


def search_window(court, after, before, deadline=None, max_pages=400):
    """Discover published opinions for one court in the [after, before] filing-date
    window via the CourtListener v4 search API. Unlike feed_court, which reaches only
    the recent feed, this reaches back arbitrarily, so it is the backfill's discovery
    path. Returns candidate dicts in the same shape feed_court yields (cluster_id,
    caseName, court_id, absolute_url, dateFiled, docketNumber, snippet, pdf_url) plus
    download_url (the court's own opinion PDF, for official_url) and precedential_status.

    Each search result already carries the lead opinion's local_path (a free
    storage.courtlistener.com PDF), its download_url, and an opening-text snippet, so a
    swept candidate needs no per-cluster cluster/opinion/docket lookup: the only REST
    cost is the search pages (v4 returns ~20 per cursor page). Goes through cl_get, so
    it shares the per-run budget and pacing and defers cleanly on a 429. max_pages caps
    a runaway; a one-year single-court window is well under it."""
    params = {"type": "o", "court": court, "filed_after": after, "filed_before": before,
              "stat_Published": "on", "order_by": "dateFiled desc", "page_size": "20"}
    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params)
    out, pages = [], 0
    while url and pages < max_pages:
        if deadline and time.time() > deadline:
            break
        data = cl_get(url, deadline)
        for r in data.get("results", []):
            cid = r.get("cluster_id")
            if not cid:
                continue
            ops = r.get("opinions") or []
            op0 = ops[0] if (ops and isinstance(ops[0], dict)) else {}
            local_path = (op0.get("local_path") or "").strip()
            download_url = (op0.get("download_url") or "").strip()
            pdf_url = (STORAGE + local_path) if local_path else (
                download_url if download_url.lower().startswith("http") else "")
            snippet = re.sub(r"\s+", " ", (op0.get("snippet") or "")).strip()
            out.append({
                "cluster_id": int(cid),
                "caseName": (r.get("caseName") or r.get("caseNameFull") or "").strip(),
                "court_id": (r.get("court_id") or court),
                "absolute_url": r.get("absolute_url") or ("/opinion/%d/" % int(cid)),
                "dateFiled": (r.get("dateFiled") or "")[:10],
                "docketNumber": (r.get("docketNumber") or "").strip(),
                "snippet": snippet[:1500],
                "pdf_url": pdf_url,
                "download_url": download_url,
                "precedential_status": (r.get("status") or "").strip(),
                "opinions": [{"id": o["id"]} for o in ops if isinstance(o, dict) and o.get("id")],
            })
        url = data.get("next")
        pages += 1
        time.sleep(1)
    return out


def cluster_id_of(r):
    if r.get("cluster_id"):
        return int(r["cluster_id"])
    m = re.search(r"/opinion/(\d+)/", r.get("absolute_url", "") or "")
    return int(m.group(1)) if m else None


ATOM = "{http://www.w3.org/2005/Atom}"
# Docket-number fallback (the summarizer normally supplies dockets); the pattern
# is jurisdiction-specific and lives in the registry.
DOCKET_RE = jurisdictions.DOCKET_RE


def feed_get(url, deadline=None):
    """Fetch a public CourtListener court feed. This is the /feed/ path, not /api/rest/,
    so it does not draw on the REST API daily rate limit. No token needed."""
    last = None
    for attempt in range(3):
        if deadline and time.time() > deadline:
            raise TimeoutError("feed deadline exceeded")
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < 2:
                wait = 4 * (attempt + 1)
                if deadline and time.time() + wait > deadline:
                    raise
                _dbg("feed error (%s), retrying in %ss" % (getattr(e, "reason", e), wait))
                time.sleep(wait); continue
            raise
    if last:
        raise last


def feed_court(court, deadline=None):
    """Discover recent opinions from the court's Atom feed. Returns candidate dicts with the
    same keys the pipeline expects (cluster_id, caseName, court_id, absolute_url, dateFiled,
    docketNumber, snippet), plus pdf_url. The feed snippet is the opinion's opening text,
    which is enough for the screen tier; full text is fetched later only for survivors."""
    raw = feed_get("https://www.courtlistener.com/feed/court/%s/" % court, deadline)
    root = ET.fromstring(raw)
    out = []
    for e in root.findall(ATOM + "entry"):
        t = e.find(ATOM + "title")
        name = (t.text or "").strip() if t is not None else ""
        href, pdf = "", ""
        for ln in e.findall(ATOM + "link"):
            rel, h = ln.get("rel"), (ln.get("href") or "")
            if rel == "alternate" and "/opinion/" in h:
                href = h
            elif rel == "enclosure" and (ln.get("type") == "application/pdf" or h.endswith(".pdf")):
                pdf = h
        m = re.search(r"/opinion/(\d+)/", href)
        if not m:
            continue
        pub = e.find(ATOM + "published")
        if pub is None:
            pub = e.find(ATOM + "updated")
        date_filed = ((pub.text or "")[:10]) if pub is not None else ""
        sm = e.find(ATOM + "summary")
        snippet = re.sub(r"<[^>]+>", " ", html.unescape(sm.text or "")) if sm is not None else ""
        snippet = re.sub(r"\s+", " ", snippet).strip()
        dm = DOCKET_RE.search(snippet)
        # site-relative path for absolute_url; check the parsed host, not a substring,
        # so a look-alike host cannot be mistaken for CourtListener. CL feeds are
        # always fully qualified, so no scheme-normalization is needed here.
        parsed_href = urllib.parse.urlparse(href)
        host = (parsed_href.hostname or "").lower()
        if host == "courtlistener.com" or host.endswith(".courtlistener.com"):
            path = parsed_href.path or "/"
            if parsed_href.query:
                path += "?" + parsed_href.query
            if parsed_href.fragment:
                path += "#" + parsed_href.fragment
        else:
            path = href
        out.append({"cluster_id": int(m.group(1)), "caseName": name, "court_id": court,
                    "absolute_url": path, "dateFiled": date_filed,
                    "docketNumber": dm.group(0) if dm else "", "snippet": snippet[:1500],
                    "pdf_url": pdf})
    return out


def snippet_of(r):
    return r.get("snippet") or ""


def opinion_ids_of(r, deadline=None):
    """Every sub-opinion id for a result/cluster (lead, concurrences, dissents), so
    the REST fallback can read the whole decision rather than only the first writing."""
    ops = r.get("opinions") or []
    ids = [o["id"] for o in ops if isinstance(o, dict) and o.get("id")]
    if ids:
        return ids
    sib = [s for s in (r.get("sibling_ids") or []) if s]
    if sib:
        return list(sib)
    cid = cluster_id_of(r)
    if not cid:
        return []
    cl = cl_get("/api/rest/v4/clusters/%d/" % cid, deadline)
    out = []
    for s in (cl.get("sub_opinions") or []):
        if isinstance(s, int):
            out.append(s)
        else:
            m = re.search(r"/opinions/(\d+)/", s) if isinstance(s, str) else None
            if m:
                out.append(int(m.group(1)))
    return out


def opinion_text_full(r, deadline=None):
    """Concatenated text of every sub-opinion in the cluster, mirroring the full
    slip-opinion PDF. The REST fallback uses this so a cluster whose lead opinion is
    not listed first never yields only a dissent; the summarizer is separately
    instructed to summarize only the court's holding."""
    parts = []
    for oid in opinion_ids_of(r, deadline):
        t = opinion_text(oid, deadline)
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def cluster_precedential_status(r, deadline=None):
    """Best-effort CourtListener precedential_status for a candidate's cluster, passed
    to the summarizer as a publication-status hint. Empty string on any failure so it
    never blocks the run (the summarizer still reads the opinion's own designation); a
    ConfigError still propagates, since that is an operator-fixable auth problem."""
    cid = cluster_id_of(r)
    if not cid:
        return ""
    try:
        cl = cl_get("/api/rest/v4/clusters/%d/" % cid, deadline)
        return (cl.get("precedential_status") or "").strip()
    except ConfigError:
        raise
    except Exception:
        return ""


def official_download_url(r, deadline=None):
    """The court's own opinion-PDF URL for a federal cluster: CourtListener's
    download_url for the lead sub-opinion (media.ca11.uscourts.gov for the
    Eleventh Circuit, www.supremecourt.gov for SCOTUS), normalized to https. Read
    through cl_get so it shares the per-run CourtListener budget and pacing rather
    than fetching the court site directly, which a server-side run cannot always
    reach. Empty string on any miss so it never blocks a run; a ConfigError (auth,
    credit, or model) still propagates, like cluster_precedential_status."""
    try:
        oids = opinion_ids_of(r, deadline)
        if not oids:
            return ""
        o = cl_get("/api/rest/v4/opinions/%s/" % oids[0], deadline)
        u = (o.get("download_url") or "").strip()
        if u.lower().startswith("http://"):
            u = "https://" + u[len("http://"):]
        return u if u.lower().startswith("https://") else ""
    except ConfigError:
        raise
    except Exception:
        return ""


_enrich = {}  # CL court id -> {cluster_id: {"status", "download_url"}}; one search per court per run


def enrich_map(court, since, deadline=None):
    """One CourtListener search call per court, returning
    {cluster_id: {"status", "download_url"}} for opinions filed since the window.
    A single search result already carries each opinion's publication status and
    its download_url (the court's own PDF), so per-card lookups read from this bulk
    result instead of fetching each cluster and each opinion one at a time. Cached
    per process, so a court is searched at most once a run. Goes through cl_get, so
    it shares the REST budget and pacing. Empty on any failure (a ConfigError still
    propagates), so callers fall back to the per-card fetch and lose no fidelity."""
    if court in _enrich:
        return _enrich[court]
    m = {}
    try:
        url = ("https://www.courtlistener.com/api/rest/v4/search/?"
               + urllib.parse.urlencode({"type": "o", "court": court, "filed_after": since,
                                         "order_by": "dateFiled desc", "page_size": "50"}))
        pages = 0
        while url and pages < 4:
            data = cl_get(url, deadline)
            for res in data.get("results", []):
                cid = res.get("cluster_id")
                if not cid:
                    continue
                du = ""
                for o in (res.get("opinions") or []):
                    du = (o.get("download_url") or "").strip()
                    if du:
                        break
                if du.lower().startswith("http://"):
                    du = "https://" + du[len("http://"):]
                m[int(cid)] = {"status": (res.get("status") or "").strip(),
                               "download_url": du if du.lower().startswith("https://") else ""}
            url = data.get("next")
            pages += 1
        _dbg("search enrich: %s -> %d opinions" % (court, len(m)))
    except ConfigError:
        raise
    except Exception as e:
        _dbg("search enrich failed for %s (%s)" % (court, e))
        m = {}
    _enrich[court] = m
    return m


def enriched(r, since, deadline=None):
    """Bulk-search fields for one candidate (see enrich_map): {"status",
    "download_url"} or {} if the candidate is not in the search window."""
    return enrich_map(r.get("court_id") or "", since, deadline).get(cluster_id_of(r) or -1, {})


def opinion_text(oid, deadline=None):
    o = cl_get("/api/rest/v4/opinions/%s/" % oid, deadline)
    for f in ("plain_text", "html_with_citations", "html", "xml_harvard", "html_lawbox", "html_columbia"):
        v = o.get(f)
        if v:
            if f != "plain_text":
                v = re.sub(r"<[^>]+>", " ", v)
                v = html.unescape(v)
            return re.sub(r"[ \t]+", " ", v).strip()
    return ""


def _pdf_ok(text):
    """Quality gate: is extracted PDF text good enough to use without falling back to REST?
    A failed or image-only extraction yields little or no text; a real opinion yields plenty.
    Survivors of the Tier 1 screen are substantive, so genuine opinions clear this easily, and
    only true extraction failures (image-only scans, download errors) fall through to REST."""
    return bool(text) and len(text) >= PDF_MIN_CHARS and sum(c.isalpha() for c in text) >= 100


def pdf_text(pdf_url, deadline=None):
    """Extract opinion text from the PDF enclosure on storage.courtlistener.com. The enclosure
    is a static file, so it needs no token and does not draw on the REST API daily rate limit.
    Returns cleaned text, or "" on any failure (missing or non-http url, download error,
    image-only PDF, or pypdf unavailable) so the caller can fall back to the REST API."""
    if not pdf_url or not pdf_url.lower().startswith(("http://", "https://")):
        return ""
    try:
        import pypdf
    except Exception as e:
        _dbg("pypdf unavailable (%s); using REST fallback" % e)
        return ""
    raw = None
    for attempt in range(2):
        if deadline and time.time() > deadline:
            return ""
        try:
            req = urllib.request.Request(pdf_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt < 1:
                if deadline and time.time() + 3 > deadline:
                    return ""
                _dbg("pdf download error (%s), retrying" % (getattr(e, "reason", e)))
                time.sleep(3); continue
            _dbg("pdf download failed (%s); using REST fallback" % (getattr(e, "reason", e)))
            return ""
    if not raw:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        _dbg("pdf parse failed (%s); using REST fallback" % e)
        return ""
    return re.sub(r"[ \t]+", " ", text).strip()


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
    # Cache the static system prompt so repeated same-model calls in one 5-minute
    # window can bill it at the cache-read rate. Behavior-neutral: the model sees
    # identical content, and a system below the model's minimum cacheable prefix is
    # a silent no-op (no cache write, no cost). Kept because it is free and self-
    # activates if a prompt ever grows past the floor -- but note that today every
    # tier's system sits UNDER its floor (Haiku 4.5 and Opus 4.8 = 4096 tokens,
    # Sonnet 5 ~ 2048; the summarize SYSTEM is only ~2.6k), so nothing actually
    # caches right now. Caching is a weak lever here regardless: the large per-
    # opinion text is unique to each call and lives after the breakpoint, so it is
    # never cacheable, and the tiers run on different models (separate caches).
    if isinstance(body.get("system"), str):
        body["system"] = [{"type": "text", "text": body["system"],
                           "cache_control": {"type": "ephemeral"}}]
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
            u = data.get("usage", {}) or {}
            _dbg("%s %s usage in=%s out=%s cache_write=%s cache_read=%s"
                 % (label, model, u.get("input_tokens"), u.get("output_tokens"),
                    u.get("cache_creation_input_tokens", 0), u.get("cache_read_input_tokens", 0)))
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
                wait = min(_retry_after(e) or min(2 ** attempt * 2, 30), 60)
                msg = "%s HTTP %s, retrying in %ss (attempt %d/5)" % (label, e.code, wait, attempt + 1)
                if e.code == 429:
                    print("  ! Anthropic rate/credit limit: " + msg)   # surfaced, not debug-only
                else:
                    _dbg(msg)
                time.sleep(wait); continue
            lo = detail.lower()
            if e.code in (401, 403):
                print("  ! Anthropic AUTHENTICATION failed (HTTP %s) on %s. Check the ANTHROPIC_API_KEY secret." % (e.code, model))
                raise ConfigError(last)
            if e.code == 404 or ("model" in lo and any(s in lo for s in ("not found", "not_found", "does not exist", "deprecated", "retired"))):
                print("  ! Anthropic MODEL problem (HTTP %s) for %r. It may be retired or misspelled; update the model id "
                      "(repo Variable OPINIONS_SCREEN_MODEL / OPINIONS_PRETRIAGE_MODEL / OPINIONS_TRIAGE_MODEL / OPINIONS_MODEL / OPINIONS_AUDIT_MODEL)." % (e.code, model))
                raise ConfigError(last)
            if e.code == 400 and any(s in lo for s in ("credit", "billing", "balance")):
                print("  ! Anthropic CREDIT/billing problem (HTTP 400): %s. Check the account balance and limits." % (detail[:200].strip() or "see body"))
                raise ConfigError(last)
            raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError) as e:
            last = "%s %s -> network error: %s" % (label, model, getattr(e, "reason", e))
            if attempt < 4:
                wait = min(2 ** attempt * 2, 30)
                _dbg("%s network error, retrying in %ss" % (label, wait))
                time.sleep(wait); continue
            raise RuntimeError(last)
    raise RuntimeError(last or (label + " failed"))


def anthropic_status():
    """Best-effort read of Anthropic's public status page (Statuspage v2 JSON).

    Returns (level, description). level is one of:
      operational  every signal nominal
      degraded     a minor/major incident or the API component is degraded/partial
      outage       the Claude API component is in a major outage, or the page's
                   blended indicator is 'critical' (a strong, rarely-false signal)
      unknown      the check is disabled or the page could not be read/parsed

    Fail-open by design: any network or parse error returns ('unknown', ...),
    and the caller proceeds with the run. This check can skip a run on a
    confirmed outage; it can never block one because the status page is down.
    """
    if STATUS_MODE == "off" or not STATUS_URL:
        return ("unknown", "status check disabled")
    try:
        req = urllib.request.Request(STATUS_URL, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return ("unknown", "status check unavailable (%s)" % (getattr(e, "reason", None) or e))
    status    = data.get("status") or {}
    indicator = (status.get("indicator") or "none").strip().lower()
    desc      = (status.get("description") or "").strip() or indicator
    # The blended indicator covers the whole page; find the Claude API component
    # specifically, since that is the only thing this pipeline depends on.
    api_state = ""
    for c in (data.get("components") or []):
        nm = (c.get("name") or "").lower()
        if "api" in nm and ("anthropic" in nm or "claude" in nm):
            api_state = (c.get("status") or "").strip().lower()
            break
    detail = "%s (Claude API: %s)" % (desc, api_state or indicator)
    if api_state == "major_outage" or indicator == "critical":
        return ("outage", detail)
    if indicator in ("minor", "major") or api_state in ("degraded_performance", "partial_outage"):
        return ("degraded", detail)
    return ("operational", desc)


def clip(text, limit=None):
    """Trim opinion text to `limit` characters for the model while keeping the head
    AND the tail. An opinion's disposition ('Judgment affirmed.') and any dissents
    sit at the very end, so a head-only cut can hide the disposition or make a
    dissent the last thing read. Keeping both ends preserves the issue, the holding,
    the disposition, and the ending, dropping only the long middle, with a marker so
    the reader knows text was omitted."""
    limit = limit or MAXCHARS
    if not text:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n\n[... middle of the opinion omitted for length ...]\n\n"
    body = max(limit - len(marker), 1000)
    head = int(body * 0.72)
    tail = body - head
    return text[:head] + marker + text[-tail:]


def screen(name, docket, snippet):
    user = "Case name: %s\nDocket: %s\nOpening excerpt:\n%s" % (name, docket, (snippet or "")[:1500])
    return anthropic_json({"model": SCREEN_MODEL, "max_tokens": 256, "system": SCREEN_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "screen")


def pretriage(name, docket, text):
    user = "Case name: %s\nDocket: %s\n\nFULL OPINION:\n%s" % (name, docket, clip(text))
    return anthropic_json({"model": PRETRIAGE_MODEL, "max_tokens": 256, "system": PRETRIAGE_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "pretriage")


def triage(name, docket, text, feed_index=""):
    user = "Case name: %s\nDocket: %s\n\nFULL OPINION:\n%s" % (name, docket, clip(text))
    if feed_index:
        user += ("\n\nCASES TO WATCH (id: name). If THIS opinion treats any of them "
                 "negatively, report them in `treats` (low threshold; a later step confirms):\n"
                 + feed_index)
    return anthropic_json({"model": TRIAGE_MODEL, "max_tokens": 1024, "system": TRIAGE_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "triage")


def summarize_request(court_id, name, docket, date_filed, text, note, cl_status=""):
    """The Messages body for the Tier-3 public summary. One source of truth for the prompt,
    shared by the synchronous summarize() and the batch path (which submits many of these as
    one 50%-priced job and parses each result with parse_json, exactly as anthropic_json does)."""
    user = ("Court (CourtListener id): %s\nCase name: %s\nDocket: %s\nDate filed: %s\n\n"
            "Publication status (CourtListener metadata, may be blank): %s\n\n"
            "Triage note (what a prior reviewer flagged as relevant): %s\n\n"
            "OPINION TEXT (the middle may be omitted for length):\n%s"
            % (court_id, name, docket, date_filed, cl_status or "(unknown)", note or "(none)", clip(text)))
    return {"model": MODEL, "max_tokens": OUT_TOKENS, "system": SYSTEM,
            "messages": [{"role": "user", "content": user}]}


def summarize(court_id, name, docket, date_filed, text, note, cl_status=""):
    return anthropic_json(summarize_request(court_id, name, docket, date_filed, text, note, cl_status),
                          "summarize")


AUDIT_SYSTEM = (
    "You are the senior editor auditing a HIGH-RISK citation event for a curated feed of court "
    "decisions. A later opinion appears to treat a case ALREADY IN THE FEED negatively. You are "
    "given (A) the FEED CARD: the cited case's name and the synopsis and 'why it matters' the "
    "feed currently publishes for it, and (B) the LATER OPINION that cites it. Do two things.\n\n"
    "1) TREATMENT. Decide how the later opinion treats the cited case AS TO THE PROPOSITION the "
    "card publishes. It is NEGATIVE only if the later opinion overrules, reverses, abrogates, "
    "holds it superseded by statute, limits or narrows its rule, disapproves, or criticizes it as "
    "wrongly decided. Distinguishing on the facts without narrowing the rule is NOT negative; "
    "following or citing in support is POSITIVE; a bare mention is NEUTRAL. Set affects_proposition "
    "true only if the treatment bears on the card's published proposition, not some other point. "
    "Be careful and conservative: a human confirms on a citator, but your read decides whether the "
    "card is flagged at all.\n\n"
    "2) CARD AUDIT. Separately, judge whether the card's published synopsis and 'why it matters' "
    "are still ACCURATE in light of the later opinion. If the later opinion shows the card "
    "overstates, misstates, or now needs qualification, set card_review true and say briefly what "
    "to fix. If the card still reads correctly, even if it should now be flagged, card_review is "
    "false.\n\n"
    "Output ONLY a JSON object with keys: treatment ('positive', 'neutral', or 'negative'); kind "
    "(one of overruled, reversed, abrogated, superseded by statute, limited, disapproved, "
    "criticized, distinguished-narrowing, or null); affects_proposition (true or false); note (one "
    "neutral sentence, no case citations, on the treatment); confidence ('high', 'medium', or "
    "'low'); card_review (true or false); card_review_note (a brief note on what to fix, or empty)."
)


def treatment_audit(new_name, new_text, card):
    prop = "%s\nSynopsis: %s\nWhy it matters: %s" % (
        card.get("name", ""), card.get("synopsis", ""), card.get("why", ""))
    user = ("FEED CARD (A):\n%s\n\nLATER OPINION THAT CITES IT (B) -- %s:\n%s"
            % (prop, new_name, clip(new_text)))
    return anthropic_json({"model": AUDIT_MODEL, "max_tokens": 700, "system": AUDIT_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "treatment-audit")


AUTHORITY_AUDIT_SYSTEM = (
    "You are the senior editor auditing a citation event for a litigation team. A later opinion "
    "appears to treat an AUTHORITY the team's practice materials rely on. You are given (A) the "
    "AUTHORITY: a case name the team treats as controlling, and (B) the LATER OPINION that cites it. "
    "Decide how the later opinion treats that authority. It is NEGATIVE only if the later opinion "
    "overrules, reverses, abrogates, holds it superseded by statute, limits or narrows its rule, "
    "disapproves, or criticizes it as wrongly decided. Distinguishing on the facts without narrowing "
    "the rule is NOT negative; following or citing in support is POSITIVE; a bare mention is NEUTRAL. "
    "Be careful and conservative: a human confirms on a citator, but your read decides whether the "
    "team is alerted at all. Output ONLY a JSON object with keys: treatment ('positive', 'neutral', "
    "or 'negative'); kind (one of overruled, reversed, abrogated, superseded by statute, limited, "
    "disapproved, criticized, distinguished-narrowing, or null); note (one neutral sentence, no case "
    "citations, on the treatment); confidence ('high', 'medium', or 'low')."
)


def authority_audit(new_name, new_text, authority_name):
    """General adverse-treatment audit for a relied-on authority that is not a feed card.
    Unlike treatment_audit there is no published proposition to test against, so this asks
    whether the later opinion treats the authority negatively at all. Reuses the Opus audit
    model and the same adverse-kinds vocabulary."""
    user = ("AUTHORITY (A): %s\n\nLATER OPINION THAT CITES IT (B) -- %s:\n%s"
            % (authority_name, new_name, clip(new_text)))
    return anthropic_json({"model": AUDIT_MODEL, "max_tokens": 500, "system": AUTHORITY_AUDIT_SYSTEM,
                           "messages": [{"role": "user", "content": user}]}, "authority-audit")


CROSSCHECK_SYSTEM = (
    "You audit a drafted summary of a court opinion for fidelity to the opinion itself, the last "
    "check before a human editor sees it. You are given the full opinion text and a drafted summary "
    "of what the court held, with its disposition. Decide ONLY whether the summary accurately states "
    "the court's actual holding and disposition. FLAG it if the summary misstates the holding, "
    "overstates how broadly the court ruled, attributes a holding or reasoning the court did not "
    "reach, gets the disposition or who prevailed backwards, or asserts a fact the opinion does not "
    "support. Do NOT flag a summary for omitting detail, for word choice, or for emphasis, so long as "
    "what it does say is correct. "
    "This check is only about statements that are PRESENT in the drafted summary and wrong. To flag, "
    "you MUST copy, verbatim, the exact span of the DRAFTED SUMMARY that is the misstatement into the "
    "\"quote\" field, character for character from the drafted-summary text shown to you. Quote from "
    "the DRAFTED SUMMARY, never from the opinion; do not paraphrase it and do not invent a sentence "
    "the summary does not contain. If you cannot point to a specific verbatim span of the drafted "
    "summary that is wrong or unsupported, return \"match\": there is nothing for this check to flag. "
    "If you are unsure about a specific statement that is in the drafted summary, flag it and quote "
    "that statement. "
    "Output ONLY a JSON object: {\"verdict\": \"match\" or \"flag\", \"quote\": \"for a flag, the exact "
    "verbatim text copied from the drafted summary that is wrong; empty string for a match\", "
    "\"reason\": \"one sentence; for a flag, name the specific discrepancy\"}."
)


def _normalize_for_match(s):
    """Lowercase, unwrap surrounding quotes/ellipses, and collapse whitespace, so a model's
    copied span matches the drafted summary despite trivial reformatting."""
    s = (s or "").strip().strip("'\"\u201c\u201d\u2018\u2019").strip()
    s = s.replace("\u2026", " ")
    s = re.sub(r"^\.\.\.|\.\.\.$", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _quote_substantiated(quote, source):
    """True only if the model's quoted span actually appears in the source text it was shown (the
    drafted card for a fidelity flag, the opinion for a completeness flag). A flag must point at a
    real span of that source; a quote that is absent (the premise was invented) or trivially short
    does not substantiate a flag."""
    q = _normalize_for_match(quote)
    if len(q) < 4:
        return False
    return q in _normalize_for_match(source)


def crosscheck(name, text, entry):
    """Independent fidelity check on a drafted card: a model other than the Opus summarizer reads the
    opinion against the drafted holding and flags a summary that misstates it. Flag-and-surface, so it
    never drops a card; the verdict rides the PR for the editor to judge. Fail-open: if no attempt
    returns a usable answer, the verdict is 'unavailable' so the card still surfaces for a manual look.
    Reuses the opinion text already in hand, so it costs no CourtListener calls.

    Two guardrails against a false flag, the dominant failure mode for a single temperature-1 read:
      1. Grounding. A flag must quote, verbatim, the span of the DRAFTED SUMMARY it claims is wrong. A
         flag whose quote is not actually in the drafted summary (an invented premise) is dismissed,
         not surfaced. The quote is folded into the reason so the editor sees exactly what was faulted.
      2. Consensus. On a substantiated flag, the check re-asks up to OPINIONS_CROSSCHECK_TRIES times and
         a flag stands only on a majority of the attempts made, so one noisy roll does not flag a sound
         card. Re-asking happens only after a flag, so a clean card still costs about one call.
    Set OPINIONS_CROSSCHECK_TRIES=1 to keep grounding but disable consensus."""
    if not CROSSCHECK_MODEL:
        return None
    body, drafted = guard_request("fidelity", name, text, entry)

    tries = max(1, CROSSCHECK_TRIES)
    flags, clears, made, last_error = [], 0, 0, None
    for attempt in range(tries):
        try:
            r = anthropic_json(body, "crosscheck")
        except Exception as e:
            last_error = str(e)[:160]
            print("  ! cross-check attempt %d unavailable for %s: %s" % (attempt + 1, name[:40], last_error))
            continue
        made += 1
        verdict = (r.get("verdict") or "").strip().lower()
        reason = (r.get("reason") or "").strip()
        quote = (r.get("quote") or "").strip()
        if verdict != "match" and _quote_substantiated(quote, drafted):
            flags.append((reason, quote))
        else:
            clears += 1
            if verdict != "match":
                print("  . cross-check flag DISMISSED (quoted span not found in the drafted summary) "
                      "for %s: reason=%r quote=%r" % (name[:40], reason[:160], quote[:120]))
        # Stop once a majority of the budget is locked either way; no need to keep paying.
        if len(flags) > tries // 2 or clears > tries // 2:
            break

    if made == 0:
        print("  ! cross-check unavailable for %s: %s" % (name[:40], last_error or "no response"))
        return {"verdict": "unavailable", "reason": last_error or "no response"}

    if len(flags) > made // 2:
        reason, quote = flags[-1]
        reason = ('%s (drafted text at issue: "%s")' % (reason, quote)) if reason else \
                 ('the drafted summary misstates the holding (drafted text at issue: "%s")' % quote)
        return {"verdict": "flag", "reason": reason, "quote": quote, "tries": made, "flag_count": len(flags)}

    if flags:
        print("  . cross-check flag NOT CONFIRMED for %s (%d of %d attempts flagged); clearing as noise"
              % (name[:40], len(flags), made))
    return {"verdict": "match", "reason": "holding matches the opinion", "tries": made, "flag_count": len(flags)}


COMPLETENESS_SYSTEM = (
    "You audit a drafted summary of a court opinion for COMPLETENESS, for a curated feed for a "
    "civil-litigation and insurance audience focused on Georgia. The feed covers only these practice areas: "
    "auto or UM/UIM, premises liability, negligent security, insurance coverage or insurer bad "
    "faith, expert-testimony admissibility, civil procedure, and damages. You are given the full "
    "opinion and a drafted summary that already captures one or more holdings. Decide ONLY whether "
    "the opinion squarely decides a SEPARATE, MATERIAL holding in one of those covered areas that "
    "the drafted summary leaves out, in its main holding or any additional holding. FLAG it only "
    "when the omitted point is a holding the court actually reached AND material enough to merit "
    "its own line in the feed, such as a second independent ground of decision, a dispositive "
    "ruling on a covered issue, or a distinct holding in a different covered area. Do NOT flag for: "
    "a point outside the covered areas; a standard of review, a subsidiary step, or procedural "
    "recitation that is not itself a holding; dicta; or mere detail, emphasis, or wording, so long "
    "as what the summary captures is itself a correct holding. When you are unsure the omitted "
    "point rises to a separate material holding, do NOT flag, because a false flag costs review time. "
    "To flag, you MUST copy, verbatim, the exact span of the FULL OPINION that states the omitted "
    "holding into the \"quote\" field, character for character from the opinion text shown to you. "
    "Quote from the FULL OPINION, never from the drafted summary; do not paraphrase it and do not "
    "invent a holding the opinion does not contain. If you cannot point to a specific verbatim span "
    "of the opinion that decides a separate material holding in a covered area the summary omits, "
    "return \"complete\". "
    "Output ONLY a JSON object: {\"verdict\": \"complete\" or \"flag\", \"quote\": \"for a flag, the exact "
    "verbatim opinion text that states the omitted holding; empty string for complete\", \"reason\": "
    "\"one sentence; for a flag, name the omitted holding and its covered area\"}."
)


def completeness_check(name, text, entry):
    """Independent completeness check on a drafted card: a model other than the Opus summarizer reads
    the opinion against the drafted holding(s) and flags a separate, material holding in a covered area
    that the card leaves out. Flag-and-surface, so it never drops a card; the verdict rides the PR for
    the editor to judge. Fail-open: if no attempt returns a usable answer, the verdict is 'unavailable'
    so the card still surfaces for a manual look. Reuses the opinion text already in hand, so it costs
    no CourtListener calls.

    The same two guardrails as crosscheck against a false flag, with the grounding source flipped:
    because a completeness flag asserts the OPINION decides a holding the card omits, a flag must quote
    the verbatim span of the OPINION that states that holding. A flag whose quote is not in the opinion
    (a hallucinated holding) is dismissed, not surfaced; the quote is folded into the reason. On a
    substantiated flag the check re-asks up to OPINIONS_COMPLETENESS_TRIES times and a flag stands only
    on a majority of the attempts made. Set OPINIONS_COMPLETENESS_TRIES=1 to keep grounding but disable
    consensus."""
    if not COMPLETENESS_MODEL:
        return None
    body, opinion = guard_request("completeness", name, text, entry)

    tries = max(1, COMPLETENESS_TRIES)
    flags, clears, made, last_error = [], 0, 0, None
    for attempt in range(tries):
        try:
            r = anthropic_json(body, "completeness")
        except Exception as e:
            last_error = str(e)[:160]
            print("  ! completeness attempt %d unavailable for %s: %s" % (attempt + 1, name[:40], last_error))
            continue
        made += 1
        verdict = (r.get("verdict") or "").strip().lower()
        reason = (r.get("reason") or "").strip()
        quote = (r.get("quote") or "").strip()
        if verdict != "complete" and _quote_substantiated(quote, opinion):
            flags.append((reason, quote))
        else:
            clears += 1
            if verdict != "complete":
                print("  . completeness flag DISMISSED (quoted holding not found in the opinion) "
                      "for %s: reason=%r quote=%r" % (name[:40], reason[:160], quote[:120]))
        # Stop once a majority of the budget is locked either way; no need to keep paying.
        if len(flags) > tries // 2 or clears > tries // 2:
            break

    if made == 0:
        print("  ! completeness check unavailable for %s: %s" % (name[:40], last_error or "no response"))
        return {"verdict": "unavailable", "reason": last_error or "no response"}

    if len(flags) > made // 2:
        reason, quote = flags[-1]
        reason = ('%s (opinion text omitted: "%s")' % (reason, quote)) if reason else \
                 ('the opinion decides a material holding the card omits (opinion text omitted: "%s")' % quote)
        return {"verdict": "flag", "reason": reason, "quote": quote, "tries": made, "flag_count": len(flags)}

    if flags:
        print("  . completeness flag NOT CONFIRMED for %s (%d of %d attempts flagged); clearing as noise"
              % (name[:40], len(flags), made))
    return {"verdict": "complete", "reason": "no material holding omitted", "tries": made, "flag_count": len(flags)}


# --- shared guard request/verdict, so the sync guards above and the batch path build the
#     one prompt and apply the one grounding rule. `kind` is "fidelity" (cross-check) or
#     "completeness". Splitting build from parse is what lets a batch submit many guard
#     requests as one 50%-priced job and interpret each result the same way. ---
def _guard_spec(kind):
    """(model, system, clear-verdict word, fold template, bare-flag reason, clear reason)
    for a guard kind. Reads the model/system globals live so a repo-Variable override or a
    test reassignment takes effect."""
    if kind == "fidelity":
        return (CROSSCHECK_MODEL, CROSSCHECK_SYSTEM, "match",
                'drafted text at issue: "%s"', "the drafted summary misstates the holding",
                "holding matches the opinion")
    if kind == "completeness":
        return (COMPLETENESS_MODEL, COMPLETENESS_SYSTEM, "complete",
                'opinion text omitted: "%s"', "the opinion decides a material holding the card omits",
                "no material holding omitted")
    raise ValueError("unknown guard kind %r" % kind)


def guard_request(kind, name, text, entry):
    """Build one guard's Messages body plus the text a flag must quote to be grounded (the
    DRAFTED summary for fidelity, the FULL OPINION for completeness). One source of truth for
    the prompt: crosscheck()/completeness_check() and the batch path all build it here."""
    model, system = _guard_spec(kind)[:2]
    holdings = [{"areas": entry["areas"], "synopsis": entry["synopsis"], "why": entry["why"]}]
    holdings += entry.get("additional_holdings", [])
    drafted = "\n\n".join(
        "Holding %d (areas: %s)\nSynopsis: %s\nWhy it matters: %s"
        % (i + 1, ", ".join(h["areas"]), h["synopsis"], h["why"])
        for i, h in enumerate(holdings))
    opinion = clip(text)
    user = ("Case name: %s\nDisposition as drafted: %s\n\nDRAFTED SUMMARY:\n%s\n\nFULL OPINION:\n%s"
            % (name, entry.get("disposition") or "(none stated)", drafted, opinion))
    body = {"model": model, "max_tokens": 400, "system": system,
            "messages": [{"role": "user", "content": user}]}
    return body, (drafted if kind == "fidelity" else opinion)


def guard_verdict(kind, r, ground):
    """Interpret ONE guard response into the same verdict shape the sync guards return.
    Applies the grounding guardrail (a flag must quote the grounding text verbatim, else it
    is cleared as an invented premise) but not the multi-attempt consensus: each batch line
    is a single attempt, so the batch path keeps the primary defense (grounding) and trades
    the secondary one (majority-of-N) for the 50% batch price -- and the sweep re-runs on its
    rotating schedule anyway. Equivalent to the sync guard run with TRIES=1."""
    _, _, clear, fold, bare, clear_reason = _guard_spec(kind)
    verdict = (r.get("verdict") or "").strip().lower()
    reason = (r.get("reason") or "").strip()
    quote = (r.get("quote") or "").strip()
    if verdict != clear and _quote_substantiated(quote, ground):
        folded = fold % quote
        reason = ("%s (%s)" % (reason, folded)) if reason else ("%s (%s)" % (bare, folded))
        return {"verdict": "flag", "reason": reason, "quote": quote, "tries": 1, "flag_count": 1}
    return {"verdict": clear, "reason": clear_reason, "tries": 1, "flag_count": 0}


# Party-name matching for the screen override in the candidate loop. A case can
# return to the feed at a higher court under the same caption (the Supreme Court
# reviewing a decision we carded from the Court of Appeals), and the caption can
# flip on appeal, so match on distinctive surname tokens, order-independent.
# Institutional and noise words are dropped so a shared insurer name alone cannot
# trigger a match, and the threshold is two shared tokens.
_NAME_STOP = {
    "the", "and", "for", "versus", "etal", "et", "al", "ex", "rel",
    "inc", "incorporated", "llc", "llp", "corp", "corporation", "company", "companies",
    "ltd", "limited", "group", "holdings", "partners", "partnership", "associates",
    "association", "services", "service", "systems", "enterprises", "trust", "estate",
    "bank", "fund", "foundation", "insurance", "insurers", "insurer", "indemnity",
    "casualty", "mutual", "auto", "automobile", "assurance", "underwriters", "national",
    "american", "general", "first", "united", "state", "states", "farm", "county",
    "city", "department", "board", "commission", "authority", "district", "georgia",
    "hospital", "health", "medical", "center", "transport", "transportation", "logistics",
    "trucking", "construction", "properties", "property", "management", "realty",
    "investments", "capital", "financial", "credit", "wrecker", "towing", "doe", "john",
    "jane", "unknown",
}


GOLDEN_PATH = os.path.join(REPO, "scripts", "golden_set.json")
GOLDEN_THIN = int(os.environ.get("OPINIONS_GOLDEN_THIN", "2"))  # an area with fewer positive anchors than this is "thin"


def golden_nominations(added, crosschecks, flagged_names):
    """Phase 4 golden-set NOMINATION -- never self-adoption. When a freshly carded
    opinion covers a practice area the golden set anchors thinly, propose it in the
    PR body as a paste-ready entry. The set's labels must stay independent of the
    system under test, so the pipeline only nominates; adoption is the editor's merge
    plus a paste, the same human ratification the original seed went through. Only
    clean cards qualify: anything the run itself flagged, or the cross-check disputed,
    would anchor the benchmark on a card whose own verdict is in doubt."""
    try:
        gset = json.load(open(GOLDEN_PATH, encoding="utf-8"))
    except Exception:
        return []
    gids = {c.get("cluster_id") for c in gset}
    cover = {}
    for c in gset:
        if c.get("expect_relevant", True):
            for ar in (c.get("expect_areas") or []):
                cover[ar] = cover.get(ar, 0) + 1
    out = []
    for e in added:
        if e["cluster_id"] in gids or e["name"] in flagged_names:
            continue
        cc = crosschecks.get(e["cluster_id"]) or {}
        if cc.get("verdict") == "flag":
            continue
        thin = sorted({ar for ar in render.all_areas(e) if cover.get(ar, 0) < GOLDEN_THIN})
        if not thin:
            continue
        out.append((thin, {
            "cluster_id": e["cluster_id"], "name": e["name"],
            "docket": (e.get("dockets") or [""])[0],
            "expect_relevant": True, "expect_areas": thin,
            "note": "thin-area anchor (%s): nominated by the %s run; adopted by editor paste + merge"
                    % (", ".join(thin), datetime.date.today().isoformat()),
            "text": ""}))
    return out


def party_tokens(name):
    """Distinctive party tokens from a case caption, lowercased and split on
    non-alphanumerics (so a hyphenated surname yields two tokens); drops digits,
    short tokens, and the institutional/noise stoplist."""
    toks = re.split(r"[^a-z0-9]+", (name or "").lower())
    return {t for t in toks if len(t) >= 4 and not t.isdigit() and t not in _NAME_STOP}


def party_match(name, card_token_sets):
    """True if `name` shares at least two distinctive party tokens with any carded
    case, identifying a likely repeat appearance regardless of caption order."""
    t = party_tokens(name)
    return any(len(t & cs) >= 2 for cs in card_token_sets)


# Docket-aware duplicate guard. CourtListener can issue two cluster ids for one
# consolidated appeal (twin clusters), or republish a corrected opinion under a new
# cluster id; the cluster-id dedup in the candidate pre-pass catches neither, so a
# second card would publish for one case. Two records are the same case when they
# share a court and either a docket number (unique within a court, so a shared docket
# also catches a revision refiled on a later date) or, filed the same day, at least
# two distinctive party tokens (the split-docket twin, whose dockets differ). A match
# must clear one of those bars; mere caption similarity is not enough. A repeat
# appearance at a higher court is not caught, because the court differs, so the
# screen-override treatment path still sees it.
_DOCKET_SPLIT = re.compile(r"[^A-Za-z0-9-]+")


def _docket_set(d):
    """Normalized docket tokens from a card's `dockets` list or a feed `docketNumber`
    string: uppercased, punctuation-trimmed, with noise words and short fragments dropped."""
    parts = []
    for piece in (d if isinstance(d, (list, tuple)) else [d]):
        parts += _DOCKET_SPLIT.split(str(piece or ""))
    out = set()
    for p in parts:
        p = p.upper().strip("-")
        if len(p) >= 4 and p not in ("CASE", "NOS", "AND"):
            out.add(p)
    return out


def _dup_sig(court_key, date, dockets, name):
    """A comparison signature for the duplicate guard: (court key, YYYY-MM-DD,
    docket-token set, party-token set)."""
    return (court_key or "", (date or "")[:10], _docket_set(dockets), party_tokens(name or ""))


def _same_case(a, b):
    """True if two `_dup_sig` signatures denote one case: the same court and either a
    shared docket token, or the same filing date with two or more shared party tokens."""
    if not a[0] or a[0] != b[0]:
        return False
    if a[2] & b[2]:
        return True
    return bool(a[1]) and a[1] == b[1] and len(a[3] & b[3]) >= 2


def _drop_counts(skipped):
    """Break the run's dropped candidates down by the tier that dropped them, read
    from the reason prefix. The screen and triage counts are the recall signal: how
    much each cheap tier discarded before a human ever saw it."""
    c = {"screen": 0, "pretriage": 0, "triage": 0, "summarizer": 0, "other": 0}
    for _name, reason in skipped:
        if reason.startswith("screen:"):
            c["screen"] += 1
        elif reason.startswith("pretriage:"):
            c["pretriage"] += 1
        elif reason.startswith("triage:"):
            c["triage"] += 1
        elif reason.startswith("summarizer:"):
            c["summarizer"] += 1
        else:
            c["other"] += 1
    return c


def _log_run(rec):
    """Append one JSON line of per-run stats to LOG_PATH for observability, and, when
    running under Actions, also write a readable summary to the run page. Best-effort:
    a logging failure must never fail the run."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception as e:
        print("  . run-log append skipped: %s" % e)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        d = rec.get("drops", {})
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(
                    "### Funnel run %s\n\n"
                    "- screened %d, triaged %d, summarized %d, audited %d\n"
                    "- carded %d, flagged %d, treatment %d\n"
                    "- dropped %d (screen %d, pretriage %d, triage %d, summarizer %d, other %d)\n"
                    "- CourtListener calls %d, cross-check flags %d, completeness flags %d\n"
                    % (rec.get("ts", ""), rec.get("screened", 0), rec.get("triaged", 0),
                       rec.get("summarized", 0), rec.get("audited", 0), rec.get("carded", 0),
                       rec.get("flagged", 0), rec.get("treatment", 0), rec.get("dropped", 0),
                       d.get("screen", 0), d.get("pretriage", 0), d.get("triage", 0), d.get("summarizer", 0), d.get("other", 0),
                       rec.get("cl_calls", 0), rec.get("crosscheck_flags", 0), rec.get("completeness_flags", 0)))
        except Exception as e:
            print("  . run summary write skipped: %s" % e)


def _log_rejections(records):
    """Append this run's screen and triage rejections to REJECT_PATH, one JSON line each, so the
    cases the funnel threw out can be reviewed for false negatives. Kept to the most recent
    REJECT_CAP lines so the committed log stays bounded, the same discipline seen_clusters uses.
    Best-effort: a logging failure must never fail the run. When running under Actions, also list
    them on the run page."""
    if not records:
        return
    try:
        old = []
        if os.path.exists(REJECT_PATH):
            with open(REJECT_PATH, "r", encoding="utf-8") as f:
                old = [ln for ln in f.read().splitlines() if ln.strip()]
        new = old + [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in records]
        # Atomic: this file is staged and committed by the workflow, so a truncating
        # write killed mid-flight would commit a corrupt log. Same discipline as
        # opinions.json and opinions_state.json.
        safeio.atomic_write_text(REJECT_PATH, "\n".join(new[-REJECT_CAP:]) + "\n")
    except Exception as e:
        print("  . rejection-log write skipped: %s" % e)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("\n### Screened out this run (%d)\n\n" % len(records))
                for r in records[:40]:
                    f.write("- [%s] %s (%s): %s\n" % (r.get("stage", ""), r.get("name") or "(unnamed)",
                                                       r.get("court") or "", r.get("reason") or ""))
                if len(records) > 40:
                    f.write("- ... and %d more (see opinions_rejections.jsonl)\n" % (len(records) - 40))
        except Exception as e:
            print("  . rejection summary write skipped: %s" % e)


def assemble_entry(v, cluster_id, name, court, areas, docket, date_filed, url, first_seen):
    """Build the opinions.json card dict from a summarize() result (v) plus the
    candidate's metadata. Shared by the daily pipeline (main) and the backfill
    (scripts/backfill.py) so both produce identically shaped cards.

    Pure: no network and no official_url enrichment -- the official-link source
    differs by pipeline, so the caller sets entry["official_url"] itself. The caller
    has already resolved and gated `areas` and `court` and (for the live feed)
    confirmed v is relevant; this only shapes the stored record. Phase-4 badges and
    the Erie field are stored only when set, matching the lean-JSON pattern the
    renderer keys on. `first_seen` is supplied by the caller (today for the daily
    run; the filing date for a backfilled card, so the digest never treats it as new)."""
    dockets = [str(d).strip() for d in (v.get("dockets") or []) if str(d).strip()] or ([docket] if docket else [""])
    disp = (v.get("disposition") or "").strip().lower()
    synopsis = (v.get("synopsis") or "").strip()
    why = (v.get("why") or "").strip()

    # Additional distinct holdings (rare). Each is validated like the primary
    # and dropped if malformed; stored only when present, so a single-holding
    # card's shape is unchanged.
    additional_holdings = []
    for h in (v.get("additional_holdings") or []):
        if not isinstance(h, dict):
            continue
        h_areas = [a for a in (h.get("areas") or []) if a in VALID_AREAS]
        h_syn = (h.get("synopsis") or "").strip()
        h_why = (h.get("why") or "").strip()
        if h_areas and h_syn and h_why:
            additional_holdings.append({"areas": h_areas, "synopsis": h_syn, "why": h_why})

    entry = {"cluster_id": cluster_id, "name": (v.get("name") or name).strip(), "court": court,
             "division": (v.get("division") or None), "date": date_filed, "dockets": dockets,
             "disposition": disp, "areas": areas, "url": url, "synopsis": synopsis, "why": why,
             "precedential": (v.get("precedential") or "unknown"),
             "first_seen": first_seen}
    # Phase 4 taxonomy. Badges are stored only when true (lean JSON, and render
    # keys on truthiness); the Erie field only for a federal court with a
    # recognized value. editor_note is human-only: the pipeline never writes it.
    if v.get("first_impression") is True:
        entry["first_impression"] = True
    if v.get("tort_reform") is True:
        entry["tort_reform"] = True
    _la = v.get("law_applied")
    _la = _la.strip().lower() if isinstance(_la, str) else ""
    if entry["court"] in ("ca11", "scotus") and _la in ({"federal"} | set(jurisdictions.JURISDICTIONS)):
        entry["law_applied"] = _la
    # A state court outside the active jurisdiction carries its state so the
    # renderer files the card under the right jurisdiction filter instead of the
    # active-jurisdiction fallback (a Supreme Court of Florida card is stamped
    # "fl"). Active-jurisdiction state cards stay unstamped, so their stored shape
    # is unchanged; federal cards derive bindingness from the court at render time.
    _jx = jurisdictions.COURT_JURISDICTION.get(entry["court"])
    if jurisdictions.COURT_SYSTEM.get(entry["court"]) == "state" and _jx and _jx != jurisdictions.JURISDICTION:
        entry["jurisdiction"] = _jx
    if additional_holdings:
        entry["additional_holdings"] = additional_holdings
    return entry


def _pr_card(e, i):
    """Full, phone-readable markdown for one pending card, so a reviewer can read
    and vet it from the PR's main page without opening the diff. Mirrors the site
    card's vocabulary (the synopsis, "Why it matters", the first-impression and
    tort-reform badges, the editor's note), so the PR reads like the card it will
    become. Returns markdown lines; the per-card review and check flags are
    appended by the caller."""
    prec = (e.get("precedential") or "").strip().lower()
    prec_note = {"unpublished": "unpublished, not binding precedent",
                 "physical precedent": "physical precedent only, not binding"}.get(prec, "")
    meta = "%s \u00b7 decided %s \u00b7 %s \u00b7 %s" % (
        render.COURT_LABELS[e["court"]], render._date_label(e["date"]),
        render._no_label(e["dockets"]), e["disposition"] or "(disposition not stated)")
    if prec_note:
        meta += " \u00b7 " + prec_note
    areas = ", ".join(render.AREA_LABELS[c] for c in render.all_areas(e))
    if e.get("first_impression"):
        areas += " \u00b7 first impression"
    if e.get("tort_reform"):
        areas += " \u00b7 tort reform"
    out = ["### %d. %s" % (i, e["name"]), "", meta, "", "areas: %s" % areas,
           "", "> %s" % e["synopsis"], "", "**Why it matters:** %s" % e["why"]]
    for h in (e.get("additional_holdings") or []):
        ha = ", ".join(render.AREA_LABELS[c] for c in (h.get("areas") or []))
        label = ("**Also (%s):** " % ha) if ha else "**Also:** "
        out += ["", "> %s%s" % (label, h.get("synopsis") or ""),
                "", "**Why it matters:** %s" % (h.get("why") or "")]
    la = (e.get("law_applied") or "").strip().lower()
    if la and la != "federal":
        jl = jurisdictions.JURISDICTIONS.get(la, {}).get("label")
        out += ["", "**Law applied:** %s law" % (jl or la)]
    note = (e.get("editor_note") or "").strip()
    if note:
        out += ["", "**Editor's note:** %s" % note]
    links = "CourtListener: %s" % e["url"]
    if e.get("official_url"):
        links += " \u00b7 Official PDF: %s" % e["official_url"]
    out += ["", links]
    return out


def route_and_publish(added, treat_events, clean_entries, flagged, crosschecks, completeness,
                      overruling_cids, pending_review, state, seen, evaluated, have, now_iso,
                      treat_flags):
    """Route this run's carded output into the two lanes and write each. Returns a counts dict
    {auto, held, treatments, wrote_auto, noop}.

      AUTO   -- a new card that is additive, unflagged, and touches no existing card. Written to
                opinions.json (from clean_entries, the pre-treatment snapshot) and rendered, for
                a straight-to-main publish.
      REVIEW -- a new card that a guard flagged or that overrules/modifies an existing card, and
                every adverse-treatment change. Staged under review/ and added to the pending
                ledger; held clusters are kept OUT of seen so a veto lets a later run redraft them.

    Pure of network. Isolated from main() so the routing is unit-tested (test_review.py)."""
    flagged_map = dict(flagged)
    auto_cards, held_items = [], []
    for e in added:
        reasons = review_store.hold_reasons(e, flagged_map, crosschecks, completeness, overruling_cids)
        (held_items if reasons else auto_cards).append((e, reasons))
    held_cids = ({int(e["cluster_id"]) for e, _ in held_items}
                 | {int(ev["citer"]["cluster_id"]) for ev in treat_events})

    if not added and not treat_events:
        seen_all = seen | evaluated | have
        if seen_all != seen:
            state["seen_clusters"] = sorted(seen_all)[-SEEN_CAP:]
            state["updated"] = now_iso
            safeio.atomic_write_json(STATE_PATH, state)
        return {"auto": 0, "held": 0, "treatments": 0, "wrote_auto": False, "noop": True}

    # AUTO lane: write + render only when there is additive content. Held treatment changes are
    # absent from clean_entries, so an auto write can never publish a held change.
    if auto_cards:
        auto_entries = clean_entries + [e for e, _ in auto_cards]
        safeio.atomic_write_json(JSON_PATH, auto_entries)
        state["last_filed"] = max(e["date"] for e in auto_entries if e.get("date"))
        render.render(auto_entries)
        ab = ["## Georgia Appellate Watch: %d new opinion(s) (auto-published)" % len(auto_cards), ""]
        for i, (e, _r) in enumerate(auto_cards, 1):
            ab += _pr_card(e, i) + [""]
        safeio.atomic_write_text(AUTO_PR_PATH, "\n".join(ab) + "\n")

    # REVIEW lane: stage each held card and each treatment change; extend the pending ledger and
    # write the review PR body, which tells the reviewer how to veto a single case.
    for e, reasons in held_items:
        review_store.stage_card(e, reasons)
    for ev in treat_events:
        review_store.stage_treatment(ev["card_cid"], ev["citer"],
                                     "adverse treatment of an already-published card")
    if held_items or treat_events:
        review_store.save_pending(pending_review | held_cids, stamp=now_iso)
        rb = ["## Georgia Appellate Watch: %d case(s) held for review" % (len(held_items) + len(treat_events)),
              "",
              "Accept a case by leaving it in this PR and merging. Veto one by commenting "
              "`/veto <cluster_id>` (or deleting its file under `review/`); a vetoed case is left "
              "eligible for a later run to redraft. Merging applies only the cases still present.", ""]
        for i, (e, reasons) in enumerate(held_items, 1):
            rb += _pr_card(e, i)
            rb += ["", "**Held because:** " + "; ".join(reasons),
                   "**To veto this case:** `/veto %d`" % int(e["cluster_id"]), ""]
        for cardnm, newnm, kind in treat_flags:
            rb.append("- treatment: **%s** may be %s by the new decision %s." % (cardnm, kind, newnm))
        if treat_events:
            rb += ["", "To veto a treatment change, `/veto <citing cluster id>`.", ""]
        safeio.atomic_write_text(REVIEW_PR_PATH, "\n".join(rb) + "\n")

    # Seen-state: advance for everything evaluated EXCEPT held cases. A held case stays out of
    # seen so a veto lets a later run rediscover it; the pending ledger suppresses it meanwhile.
    seen_all = (seen | evaluated | have | {int(e["cluster_id"]) for e, _ in auto_cards}) - held_cids
    state["seen_clusters"] = sorted(seen_all)[-SEEN_CAP:]
    state["updated"] = now_iso
    safeio.atomic_write_json(STATE_PATH, state)
    return {"auto": len(auto_cards), "held": len(held_items), "treatments": len(treat_events),
            "wrote_auto": bool(auto_cards), "noop": False}


# Console log prefixes, so a raw job log reads at a glance: "+" an opinion added
# or a routing override, "~" an adverse-treatment flag raised on an existing card,
# "!" a warning or error, "." a minor or best-effort step that was skipped. The
# same run also writes a rendered summary to the Actions run page (safeio.step_summary).
def main():
    if not KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set."); sys.exit(1)
    if not CL_TOKEN:
        print("  ! warning: COURTLISTENER_TOKEN not set; CourtListener REST limits will be tighter.")

    # The PR step reads PR_PATH as its body. Guarantee the file exists on every exit
    # path, including the no-candidates early return, so it never fails on a missing file.
    # It is gitignored and not in the PR add-paths, so a no-op run writes it and opens no PR.
    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write("No update this run.\n")

    # Anthropic status preflight. Log the current status every run; on a confirmed
    # API outage, skip cleanly without fetching, screening, or marking anything, so
    # the next scheduled run retries in a few hours instead of the day's work being
    # lost. Fail-open: an unknown/unreachable status never blocks the run.
    slevel, sdesc = anthropic_status()
    print("Anthropic status: %s%s" % (sdesc, "" if slevel in ("operational", "unknown") else " [%s]" % slevel))
    if slevel == "outage" and STATUS_MODE == "on":
        print("  ! Anthropic API is in a reported outage; skipping this run. "
              "Nothing was fetched or marked seen, so the next scheduled run will retry.")
        return

    entries = json.load(open(JSON_PATH, encoding="utf-8")) if os.path.exists(JSON_PATH) else []
    have = {int(e["cluster_id"]) for e in entries if e.get("cluster_id")}
    by_id = {int(e["cluster_id"]): e for e in entries if e.get("cluster_id")}
    # Pre-treatment snapshot for the AUTO lane. The forward-escalation path below mutates
    # existing cards in place (treatment_core.flag_caution). Those changes modify an
    # already-published card, so they belong to the REVIEW lane; the auto lane writes from
    # this pristine copy so a held treatment change is never baked into an auto-merged page.
    clean_entries = copy.deepcopy(entries)
    # Feed index for the triage adverse-treatment check: id and name of each live
    # card (superseded ones excluded). Small next to an opinion; grows slowly.
    feed_index = "\n".join("%d: %s" % (int(e["cluster_id"]), e.get("name", ""))
                           for e in entries
                           if e.get("cluster_id") and (e.get("treatment") or "ok") != "superseded")

    # Alert-out: extend the triage watch-list to the bedrock authorities the qpwb skills
    # rely on. Most are older controlling cases not in the feed, so they ride beside the feed
    # cards with an "sa:" id the treats loop routes separately. Fail-open: a missing or empty
    # manifest leaves the feed-card path untouched.
    sa_manifest = skill_alert.load_manifest(SA_MANIFEST_PATH)
    sa_items = skill_alert.watch_items(sa_manifest)
    sa_state = skill_alert.load_state(SA_STATE_PATH)
    sa_events = []
    if sa_items:
        _sa_lines = skill_alert.feed_index_lines(sa_items)
        feed_index = (feed_index + "\n" + _sa_lines) if feed_index else _sa_lines

    state = {}
    if os.path.exists(STATE_PATH):
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    seen = set(int(x) for x in state.get("seen_clusters", []))
    # Cases already staged in an open review PR: skip them so the funnel does not re-summarize
    # (and re-stage) a case every four hours while it is awaiting a human decision. A veto
    # clears the case from this ledger, so it is rediscovered and redrafted on a later run.
    pending_review = review_store.load_pending()
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
            print("  ! feed budget reached (%ds); skipping remaining courts" % SEARCH_BUDGET)
            break
        try:
            results += feed_court(court, search_deadline)
        except Exception as e:
            print("  ! courtlistener feed failed for %s: %s" % (court, e))
    if not results:
        print("no candidates returned from the courtlistener feeds "
              "(feed unreachable or empty); nothing written this run.")
        safeio.step_summary("## Georgia Appellate Watch \u00b7 funnel\n\n"
                            "**No candidates returned from the feeds this run.**")
        return
    cand, ids = [], set()
    for r in results:
        cid = cluster_id_of(r)
        if not cid or cid in have or cid in seen or cid in ids or cid in pending_review:
            continue
        if (r.get("dateFiled") or "") and r["dateFiled"] < since:
            continue
        ids.add(cid)
        cand.append(r)
    cand.sort(key=lambda r: (r.get("dateFiled") or "", cluster_id_of(r)), reverse=True)
    cand = cand[:MAX_RUN]
    print("since %s | candidates: %d | tiers: screen=%s pretriage=%s triage=%s summarize=%s"
          % (since, len(cand), SCREEN_MODEL or "off", PRETRIAGE_MODEL or "off", TRIAGE_MODEL or "off", MODEL))

    added, flagged, skipped = [], [], []
    rejections = []                            # screen/triage drops this run, logged to REJECT_PATH for recall review
    run_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    crosschecks = {}   # cluster_id -> {"verdict", "reason"} from the fidelity guard; surfaced in the PR, not written to opinions.json
    completeness = {}  # cluster_id -> {"verdict", "reason"} from the completeness guard; surfaced in the PR, not written to opinions.json
    treat_flags, audit_notes = [], []          # adverse treatment of existing cards (forward escalation)
    treat_events = []      # every new-citer treatment change, staged to the REVIEW lane (existing-card change)
    overruling_cids = set()  # candidates whose opinion caused a treatment change; held with that change if they card
    evaluated, n_screen, n_triage, n_opus, n_audit = set(), 0, 0, 0, 0
    n_pretriage = 0
    treatment_changed = False
    cl_deferred = 0                                # candidates deferred this run on the CourtListener budget
    consec = 0
    cfg_error = False                              # set on a ConfigError (auth/model/credit); forces a non-zero exit
    # Party tokens of every carded case, for the screen override below. Cards with
    # fewer than two distinctive tokens can never reach the two-token threshold, so
    # drop them here.
    card_token_sets = [s for s in (party_tokens(e.get("name", "")) for e in entries) if len(s) >= 2]
    # Signatures of every carded case for the docket-aware duplicate guard in the loop;
    # each card added this run is appended as it is carded, so an in-run twin is caught too.
    dedup_index = [(_dup_sig(e.get("court"), e.get("date"), e.get("dockets"), e.get("name")),
                    e.get("name", "?")) for e in entries]
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
        # Docket-aware duplicate guard: a new cluster id that is the same case as one
        # already carded, or already added this run, is a CourtListener twin or a corrected
        # republish. Skip it, mark it seen so it does not return, and surface the skip for the
        # editor to reconcile by hand. Costs no model or CourtListener calls.
        csig = _dup_sig(COURT_MAP.get(court_id) or court_id, date_filed, docket, name)
        dup = next((nm for sig, nm in dedup_index if _same_case(csig, sig)), None)
        if dup:
            skipped.append((name, "duplicate of carded case %r (same court and shared docket or "
                                  "same-day parties; cluster %s is a twin or a corrected republish)"
                                  % (dup[:60], cid)))
            print("  ~ duplicate skip: %s  ==  %s  (cluster %s)" % (name[:50], dup[:50], cid))
            evaluated.add(cid); consec = 0
            continue
        try:
            # Tier 1: cheap excerpt screen
            if SCREEN_MODEL:
                n_screen += 1
                s = screen(name, docket, snippet_of(r))
                if not s.get("pass"):
                    # A repeat appearance of a carded case (same parties, e.g. the
                    # Supreme Court reviewing a decision we carded from the Court of
                    # Appeals) must not be screened out, or we would miss flagging the
                    # earlier card if this opinion reverses it. When the parties match
                    # an existing card, skip the screen drop and let triage run the
                    # forward treatment escalation.
                    if party_match(name, card_token_sets):
                        print("  + screen override: %s shares parties with a carded case; routing to triage to check treatment"
                              % (name[:60]))
                    else:
                        skipped.append((name, "screen: %s" % (s.get("reason") or "not a fit")))
                        rejections.append({"ts": run_ts, "stage": "screen", "cluster_id": cid, "name": name,
                                           "court": COURT_MAP.get(court_id) or court_id, "docket": docket,
                                           "date": date_filed, "url": url, "reason": (s.get("reason") or "").strip()})
                        consec = 0; evaluated.add(cid); continue
                time.sleep(0.4)
            # Full text, fetched once and reused by tiers 2 and 3.
            # Phase 2: read the PDF enclosure first (static file on storage.courtlistener.com,
            # no REST quota, fast). Fall back to the REST API only when extraction is empty,
            # too short, or unusable, so the worst case degrades to the prior REST behavior.
            text = pdf_text(r.get("pdf_url"), deadline=run_start + BUDGET_SEC)
            deferred = False
            if _pdf_ok(text):
                _dbg("text via pdf for %s (%d chars)" % (name, len(text)))
            else:
                # The PDF gave nothing usable (empty, image-only, or header junk below the
                # quality gate). Blank it so junk can never reach triage: a triage verdict
                # on garbage marks the cluster evaluated and silently drops a possibly
                # relevant case for good. Then fall back to REST, with the same gate.
                text = ""
                if cl_rate.remaining() > 0:
                    # REST fallback only while the shared CourtListener budget has room.
                    # The calls pace and deadline themselves through cl_get; if the budget
                    # runs out mid-fetch, defer this candidate to the next run.
                    dl = run_start + BUDGET_SEC
                    try:
                        rest = opinion_text_full(r, deadline=dl)
                        if _pdf_ok(rest):
                            text = rest
                            _dbg("text via rest for %s (%d chars)" % (name, len(text)))
                    except cl_rate.RateBudgetExceeded:
                        cl_deferred += 1
                        deferred = True
                        _dbg("courtlistener budget reached; deferring %s to next run" % name)
                else:
                    cl_deferred += 1
                    deferred = True
                    _dbg("courtlistener budget reached; deferring %s to next run" % name)
            if deferred and not text:
                # A budget deferral is not a drop: it is already counted in cl_deferred and
                # reported in the CL line, and the cluster stays unevaluated so the next run
                # retries it. Listing it under "dropped" would poison the recall review.
                continue
            if not text:
                skipped.append((name, "no opinion text available")); consec = 0; continue
            time.sleep(0.4)
            # Tier 1.5: cheap full-read screen (Haiku) before the costly Sonnet triage. Drops
            # opinions whose full text shows they cannot belong, so the Sonnet read only ever
            # lands on plausible keepers. High-recall: escalate anything in doubt. Same
            # party-match guard as the excerpt screen, so a repeat appearance of a carded case
            # still reaches triage and runs the forward treatment escalation.
            if PRETRIAGE_MODEL:
                n_pretriage += 1
                ps = pretriage(name, docket, text)
                if not ps.get("pass"):
                    if party_match(name, card_token_sets):
                        print("  + pretriage override: %s shares parties with a carded case; routing to triage"
                              % (name[:60]))
                    else:
                        skipped.append((name, "pretriage: %s" % (ps.get("reason") or "not a fit")))
                        rejections.append({"ts": run_ts, "stage": "pretriage", "cluster_id": cid, "name": name,
                                           "court": COURT_MAP.get(court_id) or court_id, "docket": docket,
                                           "date": date_filed, "url": url, "reason": (ps.get("reason") or "").strip()})
                        consec = 0; evaluated.add(cid); continue
                time.sleep(0.4)
            # Tier 2: full-read relevance gate
            note = ""
            if TRIAGE_MODEL:
                n_triage += 1
                t = triage(name, docket, text, feed_index)
                # Forward escalation: if this opinion appears to treat a carded case
                # negatively, that is a high-risk event for the feed. Confirm each
                # with an Opus audit (which also re-checks the existing card), whether
                # or not this opinion itself earns a place in the feed.
                for tr in (t.get("treats") or []):
                    if not isinstance(tr, dict):
                        continue          # a malformed list element must not drop the whole candidate
                    tid = tr.get("id")
                    if skill_alert.is_authority_id(tid):
                        # Skill-authority hit: not a feed card, so a dedicated general audit
                        # (no published proposition) and a separate record + routing to skills.
                        aname = skill_alert.authority_for_id(sa_items, tid)
                        if not aname or skill_alert.already_seen(sa_state, aname, cid):
                            continue
                        try:
                            n_audit += 1
                            a = authority_audit(name, text, aname)
                        except ConfigError:
                            raise
                        except Exception as ae:
                            print("  ! authority audit failed for %s citing %s: %s" % (aname, name, ae))
                            continue
                        akind = (a.get("kind") or "").lower().strip() or None
                        if (a.get("treatment") or "").lower() == "negative" and akind in treatment_core.NEGATIVE_KINDS:
                            citer = {"cluster_id": cid, "name": name, "court": COURT_MAP.get(court_id),
                                     "date": date_filed, "kind": akind, "note": (a.get("note") or "").strip()}
                            newrec, sk = skill_alert.record(sa_state, sa_manifest, aname, citer)
                            if newrec:
                                sa_events.append((aname, citer, sk))
                                print("  ~ skill-authority adverse: %s treated by %s (%s) -> %s"
                                      % (aname[:40], name[:40], akind, ", ".join(x.replace("qpwb-", "") for x in sk)))
                        continue
                    try:
                        card = by_id.get(int(tr.get("id")))
                    except (TypeError, ValueError):
                        card = None
                    if not card or (card.get("treatment") or "ok") == "superseded":
                        continue
                    try:
                        n_audit += 1
                        a = treatment_audit(name, text, card)
                    except ConfigError:
                        raise
                    except Exception as ae:
                        print("  ! treatment audit failed for card %s citing %s: %s"
                              % (card.get("cluster_id"), name, ae))
                        continue
                    akind = (a.get("kind") or "").lower().strip() or None
                    if (a.get("treatment") or "").lower() == "negative" and a.get("affects_proposition") \
                            and akind in treatment_core.NEGATIVE_KINDS:
                        citer = {"cluster_id": cid, "name": name, "court": COURT_MAP.get(court_id),
                                 "date": date_filed, "kind": akind, "note": (a.get("note") or "").strip()}
                        # A new adverse citer is a change to an already-published card, so it is
                        # routed to the REVIEW lane and applied to the live card only when the
                        # review PR merges. Stage every genuinely new citer (not one already
                        # recorded); flag_caution here is called on the in-memory card only to
                        # decide "already recorded?" and to build the PR-body display -- the auto
                        # lane writes from clean_entries, so this mutation never reaches main
                        # except through review_apply re-running flag_caution on merge.
                        already = any(x.get("cluster_id") == cid for x in (card.get("treated_by") or []))
                        raised = treatment_core.flag_caution(card, citer)
                        if not already:
                            treatment_changed = True
                            overruling_cids.add(cid)
                            treat_events.append({"card_cid": int(card["cluster_id"]), "citer": citer})
                            if raised:
                                treat_flags.append((card.get("name", ""), name, akind))
                            print("  ~ adverse (held for review): %s treated by %s (%s)"
                                  % (card.get("name", "")[:40], name[:40], akind))
                    if a.get("card_review"):
                        audit_notes.append((card.get("name", ""), name, (a.get("card_review_note") or "").strip()))
                if not t.get("relevant") or (t.get("significance") or "").lower() == "low":
                    skipped.append((name, "triage: %s" % (t.get("reason") or "not relevant")))
                    rejections.append({"ts": run_ts, "stage": "triage", "cluster_id": cid, "name": name,
                                       "court": COURT_MAP.get(court_id) or court_id, "docket": docket,
                                       "date": date_filed, "url": url, "reason": (t.get("reason") or "").strip()})
                    consec = 0; evaluated.add(cid); continue
                note = t.get("note") or ""
                time.sleep(0.4)
            # Tier 3: high-effort public summary
            n_opus += 1
            # Publication status and (for federal cards, below) the official PDF
            # URL both come from one bulk search call per court rather than a
            # per-card cluster and opinion fetch. Fall back to the per-card fetch
            # for anything the search window did not return, so fidelity is intact.
            cl_status = enriched(r, since, deadline=run_start + BUDGET_SEC).get("status") \
                or cluster_precedential_status(r, deadline=run_start + BUDGET_SEC)
            v = summarize(court_id, name, docket, date_filed, text, note, cl_status=cl_status)
            consec = 0
            evaluated.add(cid)
        except ConfigError as e:
            print("  ! configuration error, stopping this run so it surfaces (nothing committed): %s" % e)
            cfg_error = True
            break
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
        court = COURT_MAP.get(court_id) or (v.get("court") if v.get("court") in VALID_KEYS else None)
        if not court:
            skipped.append((name, "unrecognized court id %s" % court_id)); continue
        # Card assembly is shared with the backfill (scripts/backfill.py) through
        # assemble_entry, so seeded cards carry the same Phase-4 taxonomy as the live
        # feed. first_seen is today for the daily run; the backfill passes the filing
        # date instead. synopsis/why/disp/additional_holdings are re-bound from the
        # entry for the review and print logic below.
        entry = assemble_entry(v, cid, name, court, areas, docket, date_filed, url,
                               datetime.date.today().isoformat())
        synopsis = entry["synopsis"]; why = entry["why"]; disp = entry["disposition"]
        additional_holdings = entry.get("additional_holdings", [])
        # Official-link enrichment (Phase 5): the rendered title links to the
        # court's own opinion PDF, with CourtListener kept as the full record
        # below. Two sources by court. Georgia Supreme Court: resolve the PDF from
        # gasupreme.us (scripts/official_ga.py). Eleventh Circuit and SCOTUS: the
        # court's own PDF URL is already on CourtListener as the opinion's
        # download_url (media.ca11.uscourts.gov, www.supremecourt.gov), so read it
        # through the same budgeted cl_get rather than fetching the court site,
        # which a server-side run cannot always reach. Stored only when resolved,
        # matching the lean-JSON pattern above. Fail-open: any miss leaves the
        # field absent and the card renders exactly as before; a ConfigError from
        # cl_get still propagates.
        if entry["court"] == "scotga":
            try:
                _ou = official_ga.official_url_for(entry)
                if _ou:
                    entry["official_url"] = _ou
            except Exception as _oe:
                _dbg("official_url lookup failed (%s)" % _oe)
        elif entry["court"] in ("ca11", "scotus"):
            try:
                _ou = enriched(r, since, deadline=run_start + BUDGET_SEC).get("download_url") \
                    or official_download_url(r, deadline=run_start + BUDGET_SEC)
                if _ou:
                    entry["official_url"] = _ou
            except ConfigError:
                raise
            except Exception as _oe:
                _dbg("official_url lookup failed (%s)" % _oe)

        reasons = []
        if (v.get("confidence") or "").lower() == "low":
            reasons.append("low confidence")
        if (CITE_RE.search(synopsis) or CITE_RE.search(why)
                or any(CITE_RE.search(h["synopsis"]) or CITE_RE.search(h["why"]) for h in additional_holdings)):
            reasons.append("contains a reporter-style citation")
        if not disp:
            reasons.append("no disposition")
        if not synopsis or not why:
            reasons.append("empty synopsis or reason")
        if reasons:
            flagged.append((entry["name"], reasons))

        cc = crosscheck(entry["name"], text, entry)
        if cc:
            crosschecks[cid] = cc
        cp = completeness_check(entry["name"], text, entry)
        if cp:
            completeness[cid] = cp
        added.append(entry)
        dedup_index.append((_dup_sig(entry["court"], entry["date"], entry["dockets"], entry["name"]),
                            entry["name"]))
        hold_note = (", %d holdings" % (1 + len(additional_holdings))) if additional_holdings else ""
        print("  + %s [%s] %s (sig=%s%s)" % (entry["name"], ",".join(areas), disp, v.get("significance"), hold_note))

    lines = ["## Georgia Appellate Watch: %d new opinion(s)" % len(added), ""]
    for i, e in enumerate(added, 1):
        lines += _pr_card(e, i)
        checks = []
        fr = dict(flagged).get(e["name"])
        if fr:
            checks.append("review: %s" % "; ".join(fr))
        cc = crosschecks.get(e["cluster_id"])
        if cc and cc["verdict"] == "flag":
            checks.append("cross-check FLAG: %s" % (cc["reason"] or "the summary may misstate the holding; verify against the opinion"))
        elif cc and cc["verdict"] == "unavailable":
            checks.append("cross-check could not run (%s); verify this card manually" % cc["reason"])
        elif cc:
            checks.append("cross-check: holding matches the opinion")
        cp = completeness.get(e["cluster_id"])
        if cp and cp["verdict"] == "flag":
            checks.append("completeness FLAG: %s" % (cp["reason"] or "the opinion may decide a material point in a covered area the card omits; verify against the opinion"))
        elif cp and cp["verdict"] == "unavailable":
            checks.append("completeness check could not run (%s); verify this card manually" % cp["reason"])
        elif cp:
            checks.append("completeness: no material holding omitted")
        if checks:
            lines += ["", "**Checks:**"] + ["- %s" % c for c in checks]
        lines.append("")
    if treat_flags or audit_notes:
        lines += ["", "Treatment flags this run (existing cards; confirm on Shepard\u2019s before relying):"]
        for cardnm, newnm, kind in treat_flags:
            lines.append("- **%s** -- possibly %s by the new decision %s. Raised to caution; confirm, "
                         "then set `treatment` to negative or superseded, or back to ok." % (cardnm, kind, newnm))
        for cardnm, newnm, rev in audit_notes:
            if rev:
                lines.append("- audit -- the **%s** card may need an edit in light of %s: %s" % (cardnm, newnm, rev))
    lines += skill_alert.digest_lines(sa_events)
    noms = golden_nominations(added, crosschecks, {n for n, _ in flagged})
    if noms:
        lines += ["", "Golden-set nominations (the set never adopts on its own; to adopt one, paste the "
                      "object into scripts/golden_set.json, merge, and run golden-check in build mode):"]
        for thin, cand in noms:
            lines.append("- **%s** would anchor thin area(s): %s" % (cand["name"], ", ".join(thin)))
            lines.append("```json\n%s\n```" % json.dumps(cand, ensure_ascii=False, indent=2))
    if skipped:
        lines += ["", "Screened or dropped this run (not added):"]
        lines += ["- %s: %s" % (n, why) for n, why in skipped]
    if not added and not treat_flags and not sa_events:
        lines += ["", "No new relevant opinions this run."]
    pr_body = "\n".join(lines) + "\n"
    funnel = "screened %d, pretriaged %d, triaged %d, summarized %d, audited %d" % (n_screen, n_pretriage, n_triage, n_opus, n_audit)

    cl_line = "CourtListener REST calls: %d%s" % (
        cl_rate.PACER.calls,
        ("; %d candidate(s) deferred to the next run (%s)" % (cl_deferred, cl_rate.PACER.defer_note()))
        if cl_deferred else "")

    def _summary(headline, extra=""):
        # Rendered run summary for the Actions page, so "what did this run do" is
        # legible from a phone without opening the diff or scrolling the log.
        safeio.step_summary(
            "## Georgia Appellate Watch \u00b7 funnel\n\n%s\n\n"
            "| screened | pretriaged | triaged | summarized | audited | dropped |\n"
            "| ---: | ---: | ---: | ---: | ---: | ---: |\n"
            "| %d | %d | %d | %d | %d | %d |\n\n%s"
            % (headline, n_screen, n_pretriage, n_triage, n_opus, n_audit, len(skipped), extra))

    if cfg_error:
        _summary("**Stopped on a configuration error; nothing was committed.**", cl_line)
        print("Stopped on a configuration error; nothing was committed. "
              "Exiting non-zero so the failure is visible (e.g. emailed) rather than silently deferred.")
        print(cl_line)
        sys.exit(1)

    if DRY_RUN:
        print("\n--- DRY RUN, nothing written (%s) ---\n%s" % (funnel, pr_body))
        print(cl_line); return

    os.makedirs(os.path.dirname(PR_PATH), exist_ok=True)
    open(PR_PATH, "w", encoding="utf-8").write(pr_body)

    # Per-run health record (every non-dry run, no-op or not), so the funnel's activity
    # and how much each tier discards are visible without reading raw logs.
    _log_rejections(rejections)
    _log_run({
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "screened": n_screen, "pretriaged": n_pretriage, "triaged": n_triage, "summarized": n_opus, "audited": n_audit,
        "evaluated": len(evaluated), "carded": len(added), "flagged": len(flagged),
        "treatment": len(treat_flags), "dropped": len(skipped), "drops": _drop_counts(skipped),
        "cl_calls": cl_rate.PACER.calls,
        "crosscheck_flags": sum(1 for c in crosschecks.values() if c["verdict"] == "flag"),
        "completeness_flags": sum(1 for c in completeness.values() if c["verdict"] == "flag"),
    })

    if sa_events:
        skill_alert.save_state(SA_STATE_PATH, sa_state)

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---- Two-lane routing (see route_and_publish) --------------------------------------
    # Each carded opinion is AUTO (additive, unflagged, touches no existing card) -> written and
    # rendered for a straight-to-main publish -- or HELD (guard-flagged, or it overrules/modifies
    # an existing card) -> staged under review/ for a bundled review PR a person accepts by merging
    # or vetoes case by case with `/veto <cluster_id>`. Every treatment change is held.
    routed = route_and_publish(added, treat_events, clean_entries, flagged, crosschecks,
                               completeness, overruling_cids, pending_review, state, seen,
                               evaluated, have, now_iso, treat_flags)
    print(cl_line)
    if routed["noop"]:
        _summary("No new opinions this run.", "%s \u00b7 since %s" % (cl_line, since))
        print("no new opinions (%s, dropped %d)" % (funnel, len(skipped)))
    else:
        _summary("**%d auto-published \u00b7 %d held for review**"
                 % (routed["auto"], routed["held"] + routed["treatments"]),
                 "auto cards: %d \u00b7 held cards: %d \u00b7 treatment changes: %d \u00b7 %s \u00b7 since %s"
                 % (routed["auto"], routed["held"], routed["treatments"], cl_line, since))
        print("auto %d \u00b7 held %d \u00b7 treatment %d (%s, dropped %d)"
              % (routed["auto"], routed["held"], routed["treatments"], funnel, len(skipped)))


if __name__ == "__main__":
    main()
