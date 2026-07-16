#!/usr/bin/env python3
"""Adversarial-data stress harness for render.py -- the byte-identical CI gate.

render.py projects opinions.json into ~17 pages/feeds and is guarded by _valid_shape, which is
meant to SKIP a malformed card (a bad hand-edit) rather than crash. This feeds it a battery of
adversarial opinions.json variants -- missing/empty/wrong-typed fields, window-boundary and future
dates, unicode, huge strings, HTML/script injection, duplicate ids, null values, plus a randomized
fuzz -- and asserts, for each, that render:

  - does not crash (exit 0: the guard drops bad cards, it never tracebacks), and
  - is deterministic/idempotent: rendering the same data twice yields byte-identical output.

Runs the REAL scripts/render.py via subprocess in an isolated copy of the repo, so nothing touches
the working tree. OPINIONS_RENDER_ASOF is pinned so the clock cannot make output differ.

Run directly: `python scripts/stress_render.py [fuzz-iterations]`. Exits nonzero on any failure.
"""
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASOF = "2026-07-16"

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def snapshot(web):
    """A hash over every file under `web` (path + bytes), so two renders can be compared exactly."""
    h = hashlib.sha256()
    for root, _dirs, files in os.walk(web):
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, web).encode("utf-8") + b"\0")
            with open(p, "rb") as fh:
                h.update(fh.read())
        h.update(b"|")
    return h.hexdigest()


def render_run(repo):
    env = dict(os.environ, OPINIONS_RENDER_ASOF=ASOF)
    return subprocess.run([sys.executable, "scripts/render.py"], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)


def valid_card(cid, docket="A26A%04d" % 0, date="2026-06-01", name="Alpha v. Beta"):
    return {"cluster_id": cid, "name": name, "court": "gactapp", "date": date,
            "dockets": [docket], "areas": ["auto"], "disposition": "affirmed",
            "synopsis": "A holding about the case.", "why": "It matters for practitioners.",
            "url": "https://www.courtlistener.com/opinion/%d/x/" % cid, "first_seen": date,
            "additional_holdings": []}


def adversarial_datasets():
    """(label, entries) pairs. Every one must render without a crash: _valid_shape drops the bad
    cards, the good ones render, and the result is deterministic."""
    good = valid_card(1000)
    ds = []
    ds.append(("empty list", []))
    ds.append(("one good card", [valid_card(1001)]))
    ds.append(("card: no cluster_id", [dict(good, cluster_id=None), valid_card(1002)]))
    ds.append(("card: missing cluster_id key", [{k: v for k, v in good.items() if k != "cluster_id"}, valid_card(1003)]))
    ds.append(("card: empty dockets", [dict(good, dockets=[]), valid_card(1004)]))
    ds.append(("card: dockets not a list", [dict(good, dockets="A26A0001"), valid_card(1005)]))
    ds.append(("card: synopsis not a string (int)", [dict(good, synopsis=5), valid_card(1006)]))
    ds.append(("card: missing synopsis key", [{k: v for k, v in good.items() if k != "synopsis"}, valid_card(1007)]))
    ds.append(("card: null fields", [dict(good, why=None, disposition=None), valid_card(1008)]))
    ds.append(("card: cluster_id as string", [dict(good, cluster_id="1009")]))
    ds.append(("card: date as int", [dict(good, date=20260601)]))
    ds.append(("duplicate cluster_ids", [valid_card(1010), valid_card(1010, name="Dup v. Twin")]))
    ds.append(("future-dated card", [valid_card(1011, date="2099-01-01")]))
    ds.append(("ancient card (before window)", [valid_card(1012, date="1990-01-01"), valid_card(1013)]))
    ds.append(("window-boundary dates", [valid_card(1014, date="2024-07-16"), valid_card(1015, date="2024-07-17")]))
    ds.append(("unicode + emoji fields", [dict(valid_card(1016), name="Ünïcode 案件 v. State 🏛️",
                                               synopsis="Held: façade — naïve ☃ " + "长" * 50)]))
    ds.append(("huge synopsis", [dict(valid_card(1017), synopsis="word " * 40000)]))
    ds.append(("HTML/script injection in fields", [dict(valid_card(1018),
               name="<script>alert(1)</script> v. <b>State</b>",
               synopsis="</td></tr><script>x</script> & \"quotes\" 'apos' <a href=x>",
               why="1 < 2 && 3 > 2")]))
    ds.append(("control chars + newlines", [dict(valid_card(1019), synopsis="line1\nline2\ttab\x00nul\r")]))
    ds.append(("extra unknown fields", [dict(valid_card(1020), mystery={"deep": [1, 2, {"x": None}]}, flag=True)]))
    ds.append(("areas: unknown/empty/int", [dict(valid_card(1021), areas=["not_an_area", "", 5, "auto"])]))
    ds.append(("additional_holdings malformed", [dict(valid_card(1022), additional_holdings=[{"synopsis": None}, "str", 7])]))
    ds.append(("many cards", [valid_card(2000 + i, docket="A26B%04d" % i, date="2026-06-%02d" % (i % 28 + 1))
                              for i in range(60)]))
    return ds


def run_dataset(base_repo, pristine_web, label, entries):
    """Reset public/ from pristine, render twice, return (rc1, stderr1, deterministic_bool)."""
    web = os.path.join(base_repo, "public")
    shutil.rmtree(web)
    shutil.copytree(pristine_web, web)
    with open(os.path.join(base_repo, "opinions.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    r1 = render_run(base_repo)
    snap1 = snapshot(web) if r1.returncode == 0 else None
    r2 = render_run(base_repo)
    snap2 = snapshot(web) if r2.returncode == 0 else None
    deterministic = (snap1 is not None and snap1 == snap2)
    return r1.returncode, (r1.stderr or "")[-400:], deterministic


def main():
    fuzz_n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    tmp = tempfile.mkdtemp(prefix="render-stress-")
    base = os.path.join(tmp, "repo")
    try:
        # Isolated repo copy (no .git / caches) so render's REPO/WEB resolve inside the sandbox.
        def ignore(_d, names):
            return [n for n in names if n in (".git", "__pycache__", ".ruff_cache", "node_modules",
                                              ".wrangler", "scratchpad")]
        shutil.copytree(REPO, base, ignore=ignore)
        pristine_web = os.path.join(tmp, "pristine_public")
        shutil.copytree(os.path.join(base, "public"), pristine_web)

        print("render adversarial-data stress (real render.py in an isolated copy):")
        for label, entries in adversarial_datasets():
            rc, err, det = run_dataset(base, pristine_web, label, entries)
            check("no crash: %s" % label, rc == 0, "rc=%d err=%s" % (rc, err))
            if rc == 0:
                check("deterministic: %s" % label, det)

        # Randomized fuzz: random cards with random field presence/types/values.
        rng = random.Random(31415926)
        BAD = [None, 5, [], {}, "", "x" * 500, ["a", 1], {"k": None}, True, 3.14, "2026-13-40"]
        for it in range(fuzz_n):
            n = rng.randint(0, 12)
            entries = []
            for j in range(n):
                e = valid_card(5000 + it * 100 + j, docket="F%02d%03d" % (it % 100, j),
                               date="2026-%02d-%02d" % (rng.randint(1, 12), rng.randint(1, 28)))
                # Corrupt a random subset of fields with adversarial values / deletions.
                for k in list(e.keys()):
                    r = rng.random()
                    if r < 0.12:
                        e[k] = rng.choice(BAD)
                    elif r < 0.16:
                        del e[k]
                entries.append(e)
            if rng.random() < 0.2 and entries:
                entries.append(dict(entries[0]))          # a duplicate
            rc, err, det = run_dataset(base, pristine_web, "fuzz %d (%d cards)" % (it, n), entries)
            if rc != 0:
                check("fuzz it=%d: render did not crash" % it, False, "rc=%d err=%s" % (rc, err))
                break
            if not det:
                check("fuzz it=%d: render deterministic" % it, False)
                break
        else:
            check("fuzz: %d random datasets rendered without crash, all deterministic" % fuzz_n, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print("\nFAILED: %s" % ", ".join(FAILS[:20]))
        return 1
    print("\nALL RENDER STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
