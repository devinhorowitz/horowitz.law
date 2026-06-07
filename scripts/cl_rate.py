#!/usr/bin/env python3
"""Single, durable source of truth for the CourtListener REST budget.

CourtListener throttles its REST API on three concurrent limits, and the most
restrictive one given recent traffic is what controls. The free-tier defaults are
5 requests per minute, 50 per hour, and 125 per day; Free Law Project memberships
raise them, and the numbers have moved before (every authenticated user once got
5,000 per hour). So no script hardcodes a number. The limits live here, every
CourtListener call routes through one shared pacer, and the pacer is a true
rolling-window limiter:

  * it spaces calls under the per-minute burst limit,
  * it tracks calls in rolling minute, hour, and day windows and, when a window is
    full, WAITS for the oldest call to age out before allowing the next one,
  * it never waits past the caller's deadline. A short deadline (the daily run)
    means a full window makes the call defer, so the run stays fast and the work
    rolls to the next run; a long deadline (the weekend sweep) means the run waits
    across windows and DRAINS a backlog in a single run, with no second trigger and
    no new credential, and
  * it tightens itself automatically when a 429 reports a lower limit than
    configured, and honors a 429's stated wait without sleeping past the deadline.

So one knob, the caller's wall-clock budget, decides defer-vs-drain. Nothing about
the backlog mechanism depends on the rate numbers, on branch protection, or on a
personal access token.

Configure with repo variables, all optional; an unset value falls back to the
free-tier default:

  CL_PER_MINUTE   requests per minute            (default 5)
  CL_PER_HOUR     requests per hour              (default 50)
  CL_PER_DAY      requests per day               (default 125)
  CL_RATE_MARGIN  fraction of each limit to use  (default 0.8)

On a tier change, set the variables that changed; no code moves, and a higher
limit simply drains a backlog faster.

Pure standard library, no imports of the project's own modules, so any script can
depend on it without a cycle.
"""
import os, re, time


class RateBudgetExceeded(RuntimeError):
    """A call cannot be made within the caller's deadline because a rate window is
    full (or a 429 wait is longer than the deadline). Callers stop and defer the
    remaining work to the next run; they do not retry in a tight loop."""


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float_env(name, default):
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


DEFAULTS = {"minute": 5, "hour": 50, "day": 125}
SPAN = {"minute": 60.0, "hour": 3600.0, "day": 86400.0}
MARGIN = min(max(_float_env("CL_RATE_MARGIN", 0.8), 0.05), 1.0)

_THROTTLE_RE = re.compile(r"(\d+)\s*/\s*(seconds?|sec|minutes?|min|hours?|hr|days?)", re.I)
_WAIT_RE = re.compile(r"in\s+(\d+)\s+second", re.I)
_PERIOD = {"second": "minute", "seconds": "minute", "sec": "minute", "min": "minute",
           "minute": "minute", "minutes": "minute", "hour": "hour", "hours": "hour",
           "hr": "hour", "day": "day", "days": "day"}


def parse_throttle(detail):
    """From a 429 detail string, return (period, limit, wait_seconds); any may be
    None. Lenient, so a wording change on CourtListener's side degrades gracefully
    rather than breaking the parse."""
    period = limit = wait = None
    m = _THROTTLE_RE.search(detail or "")
    if m:
        limit = int(m.group(1))
        period = _PERIOD.get(m.group(2).lower())
    w = _WAIT_RE.search(detail or "")
    if w:
        wait = int(w.group(1))
    return period, limit, wait


class Pacer:
    """In-memory, per-run rolling-window limiter. Each scheduled run is a fresh
    process; the schedule keeps separate runs in separate clock hours, and a 429
    from any same-hour overlap is absorbed by deferring, so no cross-run state is
    needed."""

    def __init__(self):
        self.limits = {
            "minute": _int_env("CL_PER_MINUTE", DEFAULTS["minute"]),
            "hour":   _int_env("CL_PER_HOUR",   DEFAULTS["hour"]),
            "day":    _int_env("CL_PER_DAY",    DEFAULTS["day"]),
        }
        self.events = []            # ascending timestamps of calls made this run
        self.calls = 0              # total REST calls this run
        self.blocked_until = 0.0    # a 429 told us to wait until this time
        self.last_wait = 0.0        # seconds of the most recent wait / would-be wait
        self.last_period = None     # which window bound it: 'minute' | 'hour' | 'day'
        self._blocked_period = None # period of the most recent 429, if known

    def per_minute(self): return max(1, self.limits["minute"])
    def per_hour(self):   return max(1, self.limits["hour"])
    def per_day(self):    return max(1, self.limits["day"])

    def _budget(self, period):
        return max(1, int(max(1, self.limits[period]) * MARGIN))

    def run_budget(self):
        # The per-hour allowance (with margin). Informational; the rolling windows,
        # not this number, are what acquire enforces.
        return self._budget("hour")

    @property
    def remaining(self):
        # Calls available in the current rolling hour, right now.
        now = time.time()
        self._prune(now)
        within = sum(1 for t in self.events if now - t < SPAN["hour"])
        return max(0, self._budget("hour") - within)

    def _prune(self, now):
        if self.events:
            self.events = [t for t in self.events if now - t < SPAN["day"]]

    def _wait_needed(self, now):
        """Return (seconds, period): the wait until a call may be made under all
        three rolling windows (0 if one may be made now), and which window binds."""
        wait, which = 0.0, None
        for period in ("minute", "hour", "day"):
            span = SPAN[period]
            budget = self._budget(period)
            within = [t for t in self.events if now - t < span]
            if len(within) >= budget:
                # The (len - budget)-th oldest in-window call is the last that must
                # age out for the count to drop below budget.
                target = within[len(within) - budget]
                w = (target + span) - now
                if w > wait:
                    wait, which = w, period
        return max(0.0, wait), which

    def acquire(self, deadline=None):
        """Account for one upcoming REST call. Wait as needed to stay within the
        rolling windows (and any 429 wait), but never past `deadline`; if the wait
        would exceed it, raise RateBudgetExceeded so the caller defers."""
        while True:
            now = time.time()
            self._prune(now)
            wait, which = self._wait_needed(now)
            if self.blocked_until - now > wait:
                wait, which = self.blocked_until - now, (self._blocked_period or which)
            if wait <= 0:
                break
            self.last_wait, self.last_period = wait, which
            if deadline is not None and now + wait > deadline:
                raise RateBudgetExceeded(
                    "CourtListener %s limit reached; about %ds until a slot frees"
                    % (which or "rate", int(wait)))
            time.sleep(wait)
        self.events.append(time.time())
        self.calls += 1

    def penalize(self, detail="", retry_after=0):
        """Record a 429: tighten the reported limit (only downward), set the wait
        the server asked for, and classify it. Returns (kind, wait) where kind is
        'short' (a per-minute burst) or 'long' (an hourly or daily ceiling)."""
        period, limit, wait = parse_throttle(detail)
        wait = retry_after or wait or 0
        if period and limit and limit < self.limits.get(period, limit):
            self.limits[period] = limit                  # auto-tighten to reality
        if wait:
            self.blocked_until = max(self.blocked_until, time.time() + wait)
        if period:
            self._blocked_period = period
        if period == "minute":
            kind = "short" if wait <= 75 else "long"
        elif period in ("hour", "day"):
            kind = "long"
        else:
            kind = "short" if (wait and wait <= 75) else "long"
        self.last_wait, self.last_period = wait, period
        return kind, wait

    def defer_note(self):
        """A short, operator-facing line explaining the most recent rate wait or
        deferral: which limit bound, its configured value, and roughly how long
        until it frees. Empty string if nothing has been recorded."""
        if not self.last_wait:
            return ""
        secs = int(self.last_wait)
        if secs >= 3600:
            human = "about %.1f hours" % (secs / 3600.0)
        elif secs >= 90:
            human = "about %d minutes" % round(secs / 60.0)
        else:
            human = "about %d seconds" % secs
        period = self.last_period
        if period in ("minute", "hour", "day"):
            lim = self.limits.get(period)
            limtxt = " (%d/%s)" % (lim, period) if lim else ""
            return "CourtListener %s limit%s; %s until it frees" % (period, limtxt, human)
        return "CourtListener rate limit; %s until it frees" % human


PACER = Pacer()


def per_minute(): return PACER.per_minute()
def per_hour():   return PACER.per_hour()
def per_day():    return PACER.per_day()
def run_budget(): return PACER.run_budget()
def remaining():  return PACER.remaining
def defer_note(): return PACER.defer_note()
