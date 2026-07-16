#!/usr/bin/env python3
"""Fault-injection stress harness for cl_rate -- the shared CourtListener REST budget/pacer. No
network. Injects a FAKE clock (sleep advances virtual time) so thousands of paced calls run instantly
and deterministically.

The pacer is safety-critical for an unattended funnel: if it ever lets more calls into a rolling
window than the budget, CourtListener 429s/blocks the account (a silent stall); if it over-defers,
the run never progresses. This drives acquire() under random call gaps, random deadlines, and injected
429 penalties, asserting:

  - the CORE invariant -- after every acquired call, NO rolling window (minute/hour/day) holds more
    than its budget;
  - acquire() always terminates: it either returns (waiting within the deadline) or raises
    RateBudgetExceeded (a wait past the deadline), never hangs and never sleeps past the deadline;
  - a long deadline DRAINS (many calls succeed over virtual time), a tight deadline DEFERS;
  - a 429 penalty blocks until its stated wait and only tightens a limit DOWNWARD;
  - parse_throttle survives adversarial 429 strings without crashing.

Run directly: `python scripts/stress_cl_rate.py [iterations]`. Exits nonzero on any failure.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cl_rate  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name + (("  -- " + detail) if detail else ""))
        print("  FAIL " + name + (("  -- " + detail) if detail else ""))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0


class FakeTime:
    """Drop-in for cl_rate's `time`: virtual clock, sleep advances it (no real waiting)."""
    def __init__(self, clock):
        self.clock = clock

    def time(self):
        return self.clock.t

    def sleep(self, s):
        self.clock.t += max(0.0, s)


def fresh_pacer(clock, minute=5, hour=50, day=125, margin=1.0):
    cl_rate.MARGIN = margin
    p = cl_rate.Pacer()
    p.limits = {"minute": minute, "hour": hour, "day": day}
    return p


def budgets(p):
    return {period: max(1, int(max(1, p.limits[period]) * cl_rate.MARGIN)) for period in ("minute", "hour", "day")}


def window_ok(p, now):
    """No rolling window may hold more than its budget."""
    b = budgets(p)
    for period in ("minute", "hour", "day"):
        within = sum(1 for t in p.events if now - t < cl_rate.SPAN[period])
        if within > b[period]:
            return period, within, b[period]
    return None


def main():
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    real_time = cl_rate.time
    clock = Clock()
    cl_rate.time = FakeTime(clock)
    saved_margin = cl_rate.MARGIN
    try:
        # --- 1. tight deadline defers once a window is full; drain over time -----------------------
        clock.t = 1_000_000.0
        p = fresh_pacer(clock, minute=5, hour=50, day=125)
        # 5 quick calls with a generous-but-finite deadline (all fit the minute window) ...
        for _i in range(5):
            p.acquire(deadline=clock.t + 1)   # no wait needed for the first 5
        v = window_ok(p, clock.t)
        check("burst of 5 respects the minute budget", v is None, repr(v))
        # ... the 6th within the same minute must wait ~ to the first call's age-out; a tight deadline
        # (no room to wait) defers instead.
        deferred = False
        try:
            p.acquire(deadline=clock.t + 1)   # only 1s of slack, but the minute window needs ~55s
        except cl_rate.RateBudgetExceeded:
            deferred = True
        check("6th call in a full minute + tight deadline -> defers", deferred)
        # With a long deadline it drains: waits (virtual) and succeeds, still within budget.
        before = clock.t
        p.acquire(deadline=clock.t + 3600)
        check("long deadline drains the 6th call", p.calls == 6)
        check("draining advanced the virtual clock", clock.t > before)
        check("still within the minute budget after draining", window_ok(p, clock.t) is None)

        # --- 2. a 429 penalty blocks until its wait and only tightens downward --------------------
        p = fresh_pacer(clock, minute=5, hour=50, day=125)
        p.penalize(detail="Request was throttled. Expected available in 42 seconds.", retry_after=42)
        t0 = clock.t
        p.acquire(deadline=clock.t + 3600)     # must wait ~42s for the block to clear
        check("429 wait is honored before the next call", clock.t - t0 >= 42)
        p.penalize(detail="throttled: 3/min", retry_after=0)
        check("429 tightens a limit downward (5/min -> 3/min)", p.limits["minute"] == 3)
        p.penalize(detail="throttled: 99/min", retry_after=0)
        check("429 never RAISES a limit (stays 3/min)", p.limits["minute"] == 3)

        # --- 3. parse_throttle unit conversion + survives adversarial 429 strings ------------------
        # A per-second rate maps onto the minute window, so the COUNT must be scaled x60 -- else the
        # pacer strangles itself to N/minute on a limit that is really N*60/minute.
        check("parse_throttle: '2/second' -> 120/minute (x60, not 2/minute)",
              cl_rate.parse_throttle("throttled to 2/second") == ("minute", 120, None))
        check("parse_throttle: '5/sec' -> 300/minute", cl_rate.parse_throttle("5/sec") == ("minute", 300, None))
        check("parse_throttle: '5/min' unchanged", cl_rate.parse_throttle("5/min") == ("minute", 5, None))
        check("parse_throttle: '100/hour' unchanged", cl_rate.parse_throttle("100/hour") == ("hour", 100, None))
        rng = random.Random(424242)
        frag = ["throttled", "Expected available in", "seconds", "5/min", "50/hour", "125/day",
                "%d" % rng.randint(0, 10**7), "/", "  ", "\n", "min", "hr", "days", "", "?/?"]
        for _ in range(500):
            s = " ".join(rng.choice(frag) for _ in range(rng.randint(0, 8)))
            try:
                period, limit, wait = cl_rate.parse_throttle(s)
            except Exception as e:
                check("parse_throttle never crashes", False, "%r on %r" % (e, s))
                break
            if not (period in (None, "minute", "hour", "day")
                    and (limit is None or isinstance(limit, int))
                    and (wait is None or isinstance(wait, int))):
                check("parse_throttle returns sane types", False, "%r -> %r" % (s, (period, limit, wait)))
                break

        # --- 4. randomized fuzz: the CORE invariant holds across thousands of paced calls ---------
        for it in range(iters):
            p = fresh_pacer(clock,
                            minute=rng.randint(2, 8), hour=rng.randint(10, 60), day=rng.randint(60, 200),
                            margin=rng.choice([1.0, 0.8, 0.5]))
            clock.t += rng.randint(0, 100000)
            for _ in range(rng.randint(1, 40)):
                clock.t += rng.choice([0, 0, 1, 5, 30, 120])   # virtual work between calls
                if rng.random() < 0.08:
                    p.penalize(detail=rng.choice(["3/min", "throttled", "20/hour", ""]),
                               retry_after=rng.choice([0, 0, 5, 30]))
                deadline = clock.t + rng.choice([0, 1, 30, 300, 100000])   # tight..drain
                try:
                    p.acquire(deadline=deadline)
                except cl_rate.RateBudgetExceeded:
                    check("fuzz: a defer never sleeps past the deadline", clock.t <= deadline)
                    continue
                # Every ACQUIRED call must respect every rolling window's budget.
                v = window_ok(p, clock.t)
                if v is not None:
                    check("fuzz it=%d: rolling window over budget" % it, False,
                          "%s window held %d > budget %d" % v)
                    break
                # And a completed acquire must not have slept past its deadline.
                check("fuzz: an acquired call did not sleep past the deadline", clock.t <= deadline + 1e-6)
            if FAILS:
                break
    finally:
        cl_rate.time = real_time
        cl_rate.MARGIN = saved_margin

    if FAILS:
        print("\nFAILED (%d):" % len(set(FAILS)))
        for f in sorted(set(FAILS))[:40]:
            print("  - " + f)
        return 1
    print("cl_rate stress: %d fuzz iterations, core window-budget invariant held" % iters)
    print("ALL CL_RATE STRESS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
