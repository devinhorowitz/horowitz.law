#!/usr/bin/env python3
"""CI guard: every reference to a sibling pipeline module's attribute must resolve.

The CI import step proves each script imports; this proves that what the scripts then
read off each other actually exists. It walks each script's AST for `module.attr` access
on the first-party pipeline modules and asserts the attribute is present on the imported
module. It catches, as a red check on the commit instead of an AttributeError at the next
scheduled run or manual dispatch: a stale file upload that silently reverts or drops a
function (the dropped update.search_window that motivated this), a rename that misses a
caller, or a typo'd attribute. Nested access (module.obj.method) is checked only at the
first attribute, since only the module-level name is the module's own contract.

Run by CI as `python scripts/check_refs.py`; importable without side effects.
"""
import ast
import glob
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# First-party pipeline modules other scripts call into -- DERIVED by globbing scripts/*.py, not
# hand-listed, so adding a script can't silently leave it unchecked (a hand list made "a module
# omitted here is a blind spot" a standing hazard; that class is now gone). test_*.py and this
# checker are excluded; every remaining module imports cleanly (the CI import step proves it), so
# importing them in main() to introspect their attributes is side-effect-free. Stdlib and
# third-party (os, json, pypdf, urllib, ...) stay out of scope: this guards only our own surface.
TARGETS = sorted(os.path.basename(f)[:-3] for f in glob.glob(os.path.join(HERE, "*.py"))
                 if not os.path.basename(f).startswith("test_")
                 and os.path.basename(f) != os.path.basename(__file__))


def main():
    sys.path.insert(0, HERE)
    mods = {}
    for name in TARGETS:
        try:
            mods[name] = importlib.import_module(name)
        except Exception as e:  # an import break is the CI import step's job; fail clearly here too
            print("check_refs: cannot import %s: %s" % (name, e))
            return 1

    problems = []
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        base = os.path.basename(path)
        if base == os.path.basename(__file__):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        seen = set()
        # `from x import y` -- verify y exists on x. Catches the lazy in-function
        # from-import that the CI import step never executes (so it would surface only
        # at the next scheduled run), and the module-level one belt-and-suspenders.
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module in mods:
                for a in n.names:
                    if a.name == "*":
                        continue
                    key = (n.module, a.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    if not hasattr(mods[n.module], a.name):
                        problems.append((base, n.module, a.name))
        # Local name bound to each target module in this file (honors `import x as y`).
        local = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name in mods:
                        local[a.asname or a.name] = a.name
        if not local:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id in local):
                target = local[n.value.id]
                key = (target, n.attr)
                if key in seen:
                    continue
                seen.add(key)
                if not hasattr(mods[target], n.attr):
                    problems.append((base, target, n.attr))

    if problems:
        for base, target, attr in sorted(problems):
            print("  %s references %s.%s, which does not exist" % (base, target, attr))
        print("\ncheck_refs: a script references a sibling-module attribute that is missing "
              "(stale upload, rename, or dropped function). Re-check the affected module.")
        return 1
    print("check_refs: all sibling-module references resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
