#!/usr/bin/env bash
# Run a `gh` command, retrying it when the failure looks like GitHub rather than us.
#
# Usage is `gh` with the binary swapped out:
#
#     bash scripts/gh_retry.sh issue create --repo "$GITHUB_REPOSITORY" --title "..." --body "..."
#
# WHY THIS EXISTS.
#
# Nearly every workflow here ends with a step that files or updates a tracking issue: the
# funnel's failure report, the heartbeat's stall alert, the link crawler's two-strike list,
# the golden-set regression notice. Those steps were the whole notification system -- there
# is no pager, no dashboard, no second channel -- and every one of them called `gh` exactly
# once, with `|| true` or `2>/dev/null` around it so a hiccup could not fail the run.
#
# Which means a hiccup could not fail the run and could not report itself either. When
# api.github.com is degraded, the alert about the thing that just broke is itself the thing
# that breaks, silently, and the run goes green-ish while nobody hears about it. That is the
# worst-behaved failure mode in the repo: it is exactly the moment the alert matters most.
#
# So: retry the transient failures, and when the budget is gone, say so as a workflow
# annotation instead of swallowing it. A `::warning::` in the run log is a poor substitute
# for the issue that should have been filed, but it is not nothing, and it is what makes the
# difference between "no alert" and "no alert, and no trace that there should have been one".
#
# WHAT IS RETRIED, AND WHAT IS DELIBERATELY NOT.
#
# The default here is the OPPOSITE of scripts/push_main.sh, and on purpose. There, an
# unrecognised error is retried, because giving up early discards a whole run's model calls.
# Here, an unrecognised error is NOT retried, because the overwhelmingly likely cause is the
# command being wrong -- a bad flag, a label that does not exist, a repo the token cannot see
# -- and no amount of waiting fixes any of those. Retrying them would just add a minute of
# sleep to every genuine mistake and bury the real message under four identical copies.
#
# One accepted risk: a `gh issue create` that times out may have succeeded server-side, so a
# retry can file a duplicate. Two issues is a strictly better failure than zero, the callers
# all search for an existing issue before creating, and the alternative -- not retrying
# creates -- gives up precisely the call that matters most.
#
# ONE THING IT CANNOT COVER: a step whose job never checked out the repo cannot run this
# file. Every current caller checks out first (scripts/test_gh_retry.py asserts it), but an
# `if: failure()` step still runs when the CHECKOUT ITSELF is what failed -- a live
# possibility during the very outage this guards against -- and would then die on a missing
# file rather than alerting. That gap is closed one layer up, in scripts/run_watchdog.py:
# a reporting step that fails is treated as a job that could not report itself.
#
# Env:
#   GH_RETRY_TRIES     attempts before giving up (default 5)
#   GH_RETRY_BACKOFF   backoff base in seconds (default 5 -> 5/10/20/40, ~75s total)
set -u
TRIES="${GH_RETRY_TRIES:-5}"
BACKOFF="${GH_RETRY_BACKOFF:-5}"

# Matched against gh's stderr. Every entry is something that goes away on its own: a
# degraded api.github.com, a rate limit, a dropped connection, a DNS blip.
is_transient() {
  printf '%s' "$1" | grep -qiE \
    'rate limit|abuse detection|please (wait|try again)|try again later|timeout|timed out|deadline exceeded|connection (reset|refused|closed)|broken pipe|unexpected EOF|no such host|server misbehaving|temporary failure|TLS handshake|EOF$|HTTP 40[08]|HTTP 429|HTTP 5[0-9][0-9]|bad gateway|service unavailable|gateway time-?out|internal server error|error connecting to'
}

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

attempt=0
while : ; do
  attempt=$((attempt + 1))
  # stdout passes straight through, untouched: callers capture it with $( ) and parse it as
  # --json output, so nothing this script says may ever land there. All of our own chatter
  # goes to stderr.
  gh "$@" 2>"$tmp"
  rc=$?
  cat "$tmp" >&2
  [ "$rc" -eq 0 ] && exit 0

  err="$(cat "$tmp")"
  if ! is_transient "$err"; then
    # Not a blip. Fail immediately with gh's own status, so `|| true` at the call site
    # behaves exactly as it did before this wrapper existed.
    exit "$rc"
  fi

  if [ "$attempt" -ge "$TRIES" ]; then
    echo "::warning::gh_retry: \`gh $1 ${2:-}\` still failing after $attempt attempts against a degraded GitHub; this alert was not delivered. Last error: $(printf '%s' "$err" | tr '\n' ' ' | cut -c1-300)" >&2
    exit "$rc"
  fi

  wait=$(( BACKOFF * (1 << (attempt - 1)) ))
  echo "gh_retry: attempt $attempt of \`gh $1 ${2:-}\` failed transiently; retrying in ${wait}s" >&2
  sleep "$wait"
done
