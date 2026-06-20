#!/usr/bin/env python3
"""Generate skill-authorities.json: the join key for GAW drip-in and alert-out.

Walks the qpwb-* skill tree and extracts, per skill, the authorities it relies
on (statutes, rules, bills, and bold reporter-anchored cases) plus a seeded
practice-area mapping onto the opinions.json area vocabulary. The relied-on
signal is the skills' own `[verify]` convention: an authority the author
flagged for source-confirmation is one that, if a new opinion treats it
adversely, should fire alert-out.

Auto layers (statutes, cases) regenerate on every run. The areas layer and any
manual authority overrides live in a "curated" block the generator preserves
across runs, the same regenerate-with-preservation idea render.py uses for its
markers.

Usage:
  python skill_authorities.py [--skills DIR] [--out FILE] [--prev FILE]
    --skills  skill-tree root (default: env QPWB_SKILLS or /mnt/skills/user)
    --out     output manifest (default: skill-authorities.json)
    --prev    manifest to inherit curated edits from (default: --out if present)
"""
import argparse, json, os, re, time, glob

AREA_VOCAB = ["procedure", "damages", "auto", "coverage", "premises", "expert", "negsec", "badfaith"]

# -- citation patterns -------------------------------------------------------
OCGA = re.compile(r"O\.C\.G\.A\.\s*§+\s*(\d+-\d+-\d+(?:\.\d+)?)")
USC = re.compile(r"\b(\d+\s+U\.S\.C\.\s*§+\s*\d+[A-Za-z]?)")
CFR = re.compile(r"\b(\d+\s+C\.F\.R\.\s*§+\s*[\d.]+)")
FRCP = re.compile(r"(?:Fed\.\s*R\.\s*Civ\.\s*P\.|FRCP)\s*(\d+(?:\([a-z0-9]+\))*)")
BILL = re.compile(r"\b((?:SB|HB)\s+\d+)\b")
# Bar rules cite in decimal form (Rule 1.7); unambiguous vs integer FRCP "Rule 26".
BAR = re.compile(r"\bRule (\d+\.\d+)\b")
# Bare section symbols (the Bankruptcy Code cites § 362, not 11 U.S.C. § 362).
# Ambiguous tree-wide, so only captured for a skill with a curated code_context.
BARE_SEC = re.compile(r"(?<![-\d.])§\s*(\d+(?:\.\d+)?)(?![-\d])")
# the relied-on flag, and authorities named inside a targeted marker
VERIFY = re.compile(r"`\[verify[^\]]*\]`")
VERIFY_OCGA = re.compile(r"\[verify[^\]]*?(\d+-\d+-\d+(?:\.\d+)?)")
# bold cite extraction
REPORTER = re.compile(r"\d+\s+(?:Ga\.(?:\s+App\.)?|S\.E\.2?d?|U\.S\.|S\.\s?Ct\.)\s+\d+|\(\d+\s+SE2?d\s+\d+\)")
BOLD = re.compile(r"\*\*([^*]+?)\*\*")
YEAR = re.compile(r"\((\d{4})\)")
NAME_SPLIT = re.compile(r",\s*(?=\d+\s+(?:Ga\.|S\.E\.|U\.S\.))")


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    name = desc = ""
    if m:
        fm = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", fm, re.M)
        if nm:
            name = nm.group(1).strip()
        dm = re.search(r"description:\s*>-?\s*\n(.*)", fm, re.S)
        if dm:
            desc = " ".join(l.strip() for l in dm.group(1).splitlines())
    return name, desc


def cases_from(text):
    out = {}
    for inner in BOLD.findall(text):
        if not REPORTER.search(inner):
            continue                      # bold emphasis, not a cite
        name = NAME_SPLIT.split(inner, 1)[0].strip()
        if " v. " not in name and " v " not in name:
            continue                      # excludes bold statute strings
        yr = YEAR.findall(inner)
        rep = REPORTER.search(inner)
        key = re.sub(r"\s+", " ", name.lower())
        out.setdefault(key, {
            "name": name,
            "year": yr[-1] if yr else None,
            "cite": rep.group(0).strip("()") if rep else None,
        })
    return sorted(out.values(), key=lambda c: c["name"])


def statutes_from(text, code_context=None):
    s = set()
    for m in OCGA.findall(text):
        s.add("O.C.G.A. § " + m)
    for m in VERIFY_OCGA.findall(text):
        s.add("O.C.G.A. § " + m)
    for m in USC.findall(text):
        s.add(re.sub(r"\s+", " ", m))
    for m in CFR.findall(text):
        s.add(re.sub(r"\s+", " ", m))
    for m in FRCP.findall(text):
        s.add("Fed. R. Civ. P. " + m)
    for m in BILL.findall(text):
        s.add(re.sub(r"\s+", " ", m))
    for m in BAR.findall(text):
        s.add("Ga. R. Prof. Conduct " + m)
    if code_context:
        for m in BARE_SEC.findall(text):
            s.add(code_context + " § " + m)
    return sorted(s)


def seed_areas(name, desc):
    t = (name + " " + desc).lower()
    a = set()
    if "negligent secur" in t or "negsec" in t:
        a |= {"negsec", "premises"}
    if "premises" in t:
        a.add("premises")
    if "expert" in t or "daubert" in t:
        a.add("expert")
    if "bad-faith" in t or "bad faith" in t:
        a.add("badfaith")
    if any(k in t for k in ["coverage", "uninsured", "underinsured", "tripartite", " um ", "reservation of rights"]):
        a.add("coverage")
    if any(k in t for k in ["damages", "evaluation", "medical-records", "medical records", "wrongful-death", "wrongful death"]):
        a.add("damages")
    if any(k in t for k in ["trucking", "commercial-vehicle", "commercial motor", "vehicle inspection", "vehicle-inspection", "motor-vehicle", "motor vehicle", "motor carrier", "auto"]):
        a.add("auto")
    if any(k in t for k in ["motion", "discovery", "complaint", "answer", "filing", "procedur", "dismiss", "compel", "summary judgment", "summary-judgment", "notice", "appellate", "arbitration", "removal", "default", "pleading", "pretrial", "pre-trial"]):
        a.add("procedure")
    return sorted(a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills", default=os.environ.get("QPWB_SKILLS", "/mnt/skills/user"))
    ap.add_argument("--out", default="skill-authorities.json")
    ap.add_argument("--prev", default=None)
    args = ap.parse_args()

    prev_path = args.prev or args.out
    curated = {}
    if os.path.exists(prev_path):
        try:
            curated = json.load(open(prev_path)).get("curated", {})
        except Exception:
            pass

    skills = {}
    for d in sorted(glob.glob(os.path.join(args.skills, "qpwb-*"))):
        base = os.path.basename(d)
        texts = []
        sm = os.path.join(d, "SKILL.md")
        if os.path.exists(sm):
            texts.append(open(sm, encoding="utf-8").read())
        for r in sorted(glob.glob(os.path.join(d, "references", "*.md"))):
            texts.append(open(r, encoding="utf-8").read())
        if not texts:
            continue
        full = "\n".join(texts)
        name, desc = frontmatter(texts[0])
        cur = curated.get(base, {})
        statutes = statutes_from(full, cur.get("code_context"))
        for extra in cur.get("add_statutes", []):
            if extra not in statutes:
                statutes.append(extra)
        statutes = sorted(s for s in statutes if s not in cur.get("drop_statutes", []))
        skills[base] = {
            "areas": cur.get("areas", seed_areas(name, desc)),
            "areas_status": "curated" if "areas" in cur else "seeded",
            "statutes": statutes,
            "cases": cases_from(full),
            "verify_markers": len(VERIFY.findall(full)),
        }

    by_auth = {}
    for sk, rec in skills.items():
        for st in rec["statutes"]:
            by_auth.setdefault(st, []).append(sk)
        for c in rec["cases"]:
            by_auth.setdefault(c["name"], []).append(sk)
    by_auth = {k: sorted(v) for k, v in sorted(by_auth.items())}

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skills_root": args.skills,
        "area_vocab": AREA_VOCAB,
        "skill_count": len(skills),
        "skills": skills,
        "by_authority": by_auth,
        "curated": curated,   # preserved across runs; hand-edit per skill: areas, add_statutes, drop_statutes
    }
    json.dump(out, open(args.out, "w"), indent=2, ensure_ascii=False)
    print("wrote", args.out)
    print("skills %d | statutes %d | cases %d | authorities indexed %d" % (
        len(skills),
        sum(len(r["statutes"]) for r in skills.values()),
        sum(len(r["cases"]) for r in skills.values()),
        len(by_auth)))


if __name__ == "__main__":
    main()
