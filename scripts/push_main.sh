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
# The caller must have already committed; this only moves HEAD onto main and pushes.
set -u

for i in 1 2 3 4 5; do
  git fetch origin main || true
  if ! git rebase FETCH_HEAD; then
    git rebase --abort 2>/dev/null || true
    echo "::error::push_main: rebase onto origin/main hit a conflict; manual resolution needed"
    exit 1
  fi
  if git push origin HEAD:main; then
    exit 0
  fi
  echo "push_main: attempt $i rejected (main advanced under us); refetching and retrying"
  sleep $((i * 3))
done

echo "::error::push_main: could not fast-forward main after 5 attempts"
exit 1
