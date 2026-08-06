#!/usr/bin/env bash
# Push the current HEAD to origin/main with a fetch-rebase-retry loop.
#
# Several workflows write main from independent concurrency groups (the 4-hourly
# funnel, the review-apply on a PR merge, the daily maintenance keepalive, the
# golden-set build). GitHub does not serialize across concurrency groups, so two
# of them pushing in the same window means the second push is rejected
# non-fast-forward -- and, with a plain `git push`, that run's committed work is
# lost. This helper rebases the local commit(s) onto the freshly fetched main and
# retries, so a collision with an unrelated commit (a keepalive, a status blip,
# another card) resolves instead of dropping work. An unresolvable rebase
# conflict fails loudly rather than force-pushing over the other run.
#
# TWO DIFFERENT FAILURES, TWO DIFFERENT WAITS.
#
# The original loop treated every failure as a collision and retried on a 3/6/9/12s
# ramp -- about 30 seconds in total. That is right for a collision: the other run has
# already landed, so refetch and go again immediately. It is far too short for the
# other case. When GitHub itself is unwell -- git or api 5xx, a TLS reset, DNS
# trouble, a partial Actions incident -- 30 seconds expires long before the incident
# does, and the run gives up having already paid for everything upstream of the push:
# the funnel's model calls, the sweep's classifications, the golden set's rebuild.
# All committed, none pushed, all discarded.
#
# So the two are told apart by what git says. A rejection ("non-fast-forward",
# "fetch first", "stale info") means somebody beat us there: retry fast, up to
# PUSH_MAIN_TRIES. Anything else means the remote is not healthy, so back off
# geometrically up to PUSH_MAIN_OUTAGE_TRIES, which by default rides out roughly ten
# minutes rather than thirty seconds.
#
# THE FETCH IS PART OF THE OUTAGE PATH, NOT THE CONFLICT PATH.
#
# Worth stating because the obvious shape gets it backwards. During an outage the
# push is usually not even the first thing to fail -- `git fetch` is. If the fetch
# error is swallowed (`git fetch ... || true`) the next line, `git rebase FETCH_HEAD`,
# dies with "invalid upstream 'FETCH_HEAD'", which reads exactly like a rebase
# conflict and exits immediately. The retry budget below would never be reached in
# the one situation it exists for. So a failed fetch is classified as an outage and
# retried; only a rebase that actually ran and could not reconcile is a conflict.
#
# The caller must have already committed; this only moves HEAD onto main and pushes.
#
# Env (scripts/test_push_main.py sets the backoffs to 0 to exercise the exhaustion
# paths without spending real time; nothing else should change them):
#   PUSH_MAIN_BACKOFF         collision backoff multiplier, seconds (default 3 -> 3/6/9/12)
#   PUSH_MAIN_TRIES           collision attempts (default 5)
#   PUSH_MAIN_OUTAGE_BACKOFF  outage backoff base, seconds (default 15 -> 15/30/60/120/240)
#   PUSH_MAIN_OUTAGE_TRIES    outage attempts (default 6)
set -u
BACKOFF="${PUSH_MAIN_BACKOFF:-3}"
TRIES="${PUSH_MAIN_TRIES:-5}"
OUTAGE_BACKOFF="${PUSH_MAIN_OUTAGE_BACKOFF:-15}"
OUTAGE_TRIES="${PUSH_MAIN_OUTAGE_TRIES:-6}"

# A push rejected because main moved. Matched on git's own wording; anything that does
# not match is treated as an outage, which is the safe default -- waiting longer than
# necessary costs a little time, giving up too early costs the whole run's work. The
# cost of that default is that a genuinely permanent rejection (a branch-protection
# hook, say) burns the full outage budget before failing. That is the trade taken
# deliberately: this repo does not protect main against these jobs, and a permanent
# rejection needs a human either way.
is_collision() {
  printf '%s' "$1" | grep -qiE 'non-fast-forward|fetch first|stale info|cannot lock ref|failed to lock'
}

collisions=0
outages=0

# One shared budget for every "the remote is unwell" failure, whether it showed up as a
# failed fetch or a failed push -- they are the same incident, and bounding them together
# bounds the total wait. Exits the script when the budget is gone.
outage_step() {
  outages=$((outages + 1))
  echo "push_main: attempt $outages failed ($1); the remote may be degraded"
  if [ "$outages" -ge "$OUTAGE_TRIES" ]; then
    echo "::error::push_main: gave up after $outages attempts against a remote that kept failing for a non-collision reason; treating this as a GitHub outage that outlasted the retry budget. This run's commit is still local and its work is lost -- re-run once GitHub recovers."
    exit 1
  fi
  # Geometric, not linear: an incident lasts minutes, not seconds.
  wait=$(( OUTAGE_BACKOFF * (1 << (outages - 1)) ))
  echo "push_main: waiting ${wait}s before retrying"
  sleep "$wait"
}

while : ; do
  if ! ferr="$(git fetch origin main 2>&1 >/dev/null)"; then
    [ -n "$ferr" ] && echo "$ferr"
    outage_step "could not fetch origin/main"
    continue
  fi

  if ! git rebase FETCH_HEAD; then
    git rebase --abort 2>/dev/null || true
    echo "::error::push_main: rebase onto origin/main hit a conflict; manual resolution needed"
    exit 1
  fi

  # Capture stderr to classify the failure; still echo it, so a real run's log is unchanged.
  err="$(git push origin HEAD:main 2>&1 >/dev/null)" && { [ -n "$err" ] && echo "$err"; exit 0; }
  [ -n "$err" ] && echo "$err"

  if is_collision "$err"; then
    collisions=$((collisions + 1))
    echo "push_main: attempt $collisions rejected (main advanced under us); refetching and retrying"
    if [ "$collisions" -ge "$TRIES" ]; then
      echo "::error::push_main: could not fast-forward main after $collisions attempts"
      exit 1
    fi
    sleep $((collisions * BACKOFF))
  else
    outage_step "push failed without a collision message"
  fi
done
