# Working on horowitz.law

Operating notes for anyone maintaining this site from a cold start, an AI agent or a
person who has not seen it before. The rest is covered elsewhere: README.md is what the
site is, PIPELINE.md is how the opinions pipeline works and how to tune it, MAINTENANCE.md
is what to edit and what is generated, ROADMAP.md is what is planned, and HANDOFF.md is the
current working state and the open tasks. Read HANDOFF.md first; read this before changing
anything.

## Seeing the true state of main

There is no local clone. `main` lives on GitHub and is edited through the web UI, so the
first job is reading what is actually deployed.

- The authoritative snapshot of `main` is the codeload tarball:
  `https://codeload.github.com/devinhorowitz/horowitz.law/tar.gz/refs/heads/main`. It is
  tokenless and current. `raw.githubusercontent.com` lags by minutes and can serve stale
  files, so do not verify against it.
- `api.github.com` is rate-limited (HTTP 403) on shared IPs, so do not rely on it to list
  pull requests, Actions runs, or commits.
- To enumerate pull requests and branches without the API:
  `git ls-remote https://github.com/devinhorowitz/horowitz.law`. A `refs/pull/N/merge` ref
  exists only for an open PR; `refs/heads/*` are branches. For whether a branch is merged,
  `git clone --filter=blob:none --no-checkout`, then `git branch -r --merged origin/main`.
- Confirm a change landed by re-fetching the tarball and diffing, not by trusting that the
  upload went through.

## How a change reaches production

opinions.json is the source of truth, render.py generates the derived pages, and the
render-sync workflow reconciles them. MAINTENANCE.md has the full source-to-generated map.
The doctrine: the generated pages belong to the render-sync bot, never to a hand upload.
Two safe ways to ship a change that touches rendered output:

1. Upload only the changed source, run render-sync from the Actions tab, and merge the one
   review PR it opens.
2. Pre-render locally (`python scripts/render.py`) and upload the source plus every
   regenerated file in the same commit. CI stays green and no PR is needed.

Never upload a single regenerated page on its own; it drifts from opinions.json or is
overwritten on the next render. A change that touches no rendered output, a script, a
workflow, a config, a doc, or a state file, goes straight to `main`.

Expect a red CI render-idempotency check on a source-only PR. It means the source is ahead
of the pages until render-sync runs, which is correct, not a failure.

Uploading through the web UI: drag whole folders, never select files one by one (per-file
selection silently strands files), and upload dotfiles (`.gitignore`, `.github/`,
`.well-known/`) individually, because a folder drag drops them.

## Validate before staging

Run the offline checks before handing over any deliverable:

- Python: `python -m py_compile` on the changed scripts, an import check, and `ruff check`.
- JavaScript: `node --check` on the changed files.
- render: run `python scripts/render.py` and confirm it is byte-idempotent against the
  committed pages, except for the source you changed.
- Diff the deliverable against the codeload tarball of `main`, so it is built on current
  state rather than a stale copy.

## If you are an AI agent in a session

- Read HANDOFF.md from `main` first. It carries the current state and the open tasks.
- A probe session is read-only: establish ground truth from the actual code, report the
  findings, change nothing.
- A build session stages deliverables to `/mnt/user-data/outputs`, mirroring the repo
  paths, and hands them over with present_files. The user uploads them to GitHub.
- The bash sandbox network is allowlisted (codeload and github, courtlistener and the court
  sites, the package registries); a domain outside the list fails, and the user can add it
  in the network settings.
- A CourtListener MCP connector is available for retrieval.

## Landmines not covered elsewhere

- `claude-opus-4-8` rejects any temperature other than 1 with HTTP 400. This is a parameter
  error, not a model retirement. The model ids live in repository Variables with fallback
  defaults (PIPELINE.md, Tuning).
- CourtListener feeds carry more than the published-only REST filter: `stat_Published=True`
  drops unpublished orders and dispositions that appear in the `/feed/` output. The free-tier
  rate limits are documented at the top of `scripts/cl_rate.py` and in PIPELINE.md.
- The CourtListener MCP connector times out when a court filter (`docket__court`) is combined
  with a date range. Use single-dimension filters.
- Cloudflare Pages has its own settings that must not change (Rocket Loader and Email Address
  Obfuscation off, proxied apex and `www`, a CAA record including `pki.goog`). MAINTENANCE.md
  carries them.
