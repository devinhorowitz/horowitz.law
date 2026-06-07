#!/usr/bin/env python3
"""Single, durable source of truth for the CourtListener REST budget.

CourtListener throttles its REST API on three concurrent limits, and the most
restrictive one given recent traffic is what controls. The free-tier defaults are
5 requests per minute, 50 per hour, and 125 per day; Free Law Project memberships
raise them, and the numbers have moved before (every authenticated user once got
5,000 per hour). So no script hardcodes a number. The limits live here, every
CourtListener call routes through one shared pacer, and the pacer:

  * spaces calls so a run stays under the per-minute burst limit,
  * stops a run cleanly at a per-run budget derived from the per-hour limit,
  * never sleeps off a long (hourly or daily) throttle. It defers to the next
    scheduled run instead of hanging, and
  * tightens itself automatically when a 429 reports a lower limit than configured.

Configure with repo variables, all optional; an unset value falls back to the
free-tier default:

  CL_PER_MINUTE   requests per minute            (default 5)
  CL_PER_HOUR     requests per hour              (default 50)
  CL_PER_DAY      requests per day, advisory     (default 125)
  CL_RATE_MARGIN  fraction of each limit to use  (default 0.8)

On a tier change, set the variables that changed; no code moves. A backlog drains
over successive scheduled runs (each does one batch of up to the per-run budget
and persists its own cursor), so raising a limit or the run cadence drains it
faster, again with no code change.

Pure standard library, no imports of the project's own modules, so any script can
depend on it without a cycle.
"""
import os, re, time


class RateBudgetExceeded(RuntimeError):
    """The per-run CourtListener budget is spent, or a throttle makes further calls
    impossible within the deadline. Callers stop and defer; they do not retry."""


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
    """In-memory, per-run pacer. Each scheduled run is a fresh process, so no
    cross-run state is needed: the schedule keeps separate runs in separate clock
    hours, and a 429 from any same-hour overlap is handled by deferring."""

    def __init__(self):
        self.limits = {
            "minute": _int_env("CL_PER_MINUTE", DEFAULTS["minute"]),
            "hour":   _int_env("CL_PER_HOUR",   DEFAULTS["hour"]),
            "day":    _int_env("CL_PER_DAY",    DEFAULTS["day"]),
        }
        self.calls = 0          # REST calls accounted this run
        self.last_ts = 0.0
        self.last_wait = 0

    def per_minute(self):
        return max(1, self.limits["minute"])

    def per_hour(self):
        return max(1, self.limits["hour"])

    def per_day(self):
        return max(1, self.limits["day"])

    def run_budget(self):
        # A single run stays under the hourly limit (with margin) and never asks for
        # more than the daily limit would allow either.
        return max(1, min(int(self.per_hour() * MARGIN), int(self.per_day() * MARGIN)))

    @property
    def remaining(self):
        return self.run_budget() - self.calls

    def _interval(self):
        # Seconds between calls to stay under the per-minute burst limit.
        return 60.0 / max(1.0, self.per_minute() * MARGIN)

    def acquire(self, deadline=None):
        """Account for one upcoming REST call: enforce the per-run budget and space
        calls under the per-minute limit. Raise RateBudgetExceeded to make the
        caller stop and defer rather than block."""
        if self.calls >= self.run_budget():
            raise RateBudgetExceeded(
                "per-run CourtListener budget reached (%d/hour, %d used)"
                % (self.per_hour(), self.calls))
        now = time.time()
        wait = self._interval() - (now - self.last_ts)
        if wait > 0:
            if deadline is not None and now + wait > deadline:
                raise RateBudgetExceeded("rate spacing would exceed the deadline")
            time.sleep(wait)
        self.last_ts = time.time()
        self.calls += 1

    def penalize(self, detail="", retry_after=0):
        """Record a 429. Lower the reported limit (only ever downward), remember the
        wait, and decide whether the run must defer. Returns (kind, wait) where kind
        is 'short' (a per-minute burst, worth a brief wait then retry) or 'long' (an
        hourly or daily ceiling, defer now and do not sleep on it)."""
        period, limit, wait = parse_throttle(detail)
        wait = retry_after or wait or 0
        if period and limit and limit < self.limits.get(period, limit):
            self.limits[period] = limit                      # auto-tighten to reality
        if period == "minute":
            kind = "short" if wait <= 75 else "long"
        elif period in ("hour", "day"):
            kind = "long"
        else:
            kind = "short" if (wait and wait <= 75) else "long"
        if kind == "long":
            self.calls = max(self.calls, self.run_budget())  # force the run to defer
        self.last_wait = wait
        return kind, wait


PACER = Pacer()


def per_minute(): return PACER.per_minute()
def per_hour():   return PACER.per_hour()
def per_day():    return PACER.per_day()
def run_budget(): return PACER.run_budget()
def remaining():  return PACER.remaining
