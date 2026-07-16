#!/usr/bin/env python3
"""Fable senior review of a held case (scripts/fable_review.py).

When a card reaches the review lane -- a fidelity or completeness flag, a guard that could not
run, or an overrule/modify of an existing card -- Claude Fable 5, the most capable model,
adjudicates the flag against the actual opinion text. The funnel uses the verdict two ways
(OPINIONS_FABLE_REVIEW): 'advisory' attaches it to the review PR for the human; 'clear'
additionally auto-publishes a case Fable is highly confident is a FALSE POSITIVE.

Either way this is FAIL-CLOSED: a clear requires the triple of is_false_positive AND high
confidence AND an accept recommendation, on adequate opinion text; any error, thin text, or lesser
confidence leaves the case held. Fable can only ever REDUCE holds, never add a publish on doubt --
a wrong card that looks right is the worst outcome, worse than a card that waited for a person.

review_held() takes the assembled card, its hold reasons, the opinion text, and a `call_json`
callable (update.anthropic_json is passed in, so this module does no network of its own and stays
unit-testable by stubbing that callable -- and there is no import cycle with update.py).
"""

FABLE_SYSTEM = (
    "You are a senior appellate attorney giving a final review to an AI-drafted case-law card that "
    "an automated pipeline held back for human attention before publishing. You are the last check "
    "before it either publishes or waits for a person. Decide whether the reason it was held is a "
    "GENUINE problem or a FALSE POSITIVE.\n\n"
    "Judge only from the OPINION TEXT provided. Clear the flag ONLY when you are highly confident "
    "the card is accurate, complete for its stated holding, and the flag is mistaken. When in any "
    "doubt -- including when the opinion text is missing or too thin to verify -- do NOT clear: a "
    "wrong card that looks right is the worst outcome, worse than one that waits for a person. For "
    "an overrule/modify hold, do not clear unless the text makes plain the new decision does not in "
    "fact overrule or modify existing law.\n\n"
    "Reply with ONLY a JSON object, no prose around it:\n"
    '{"is_false_positive": true|false, "confidence": "high"|"medium"|"low", '
    '"recommendation": "accept"|"veto"|"decline", "assessment": "one or two sentences"}\n'
    "recommendation: accept = the card is right, publish it; veto = the draft is wrong, redraft it; "
    "decline = the case is not worth carding. If the opinion text is absent or inadequate to verify "
    "the flag, set confidence to \"low\"."
)

MIN_TEXT_ALPHA = 400   # below this the opinion text cannot support a confident clear


def _alpha(s):
    return sum(c.isalpha() for c in (s or ""))


def _held(assessment, recommendation="veto", confidence="low", available=False):
    return {"clear": False, "is_false_positive": False, "confidence": confidence,
            "recommendation": recommendation, "assessment": assessment, "available": available}


def review_held(entry, reasons, opinion_text, call_json, model="claude-fable-5", out_tokens=8000):
    """Adjudicate one held card with Fable. Returns a verdict dict:
        {"clear": bool, "is_false_positive": bool, "confidence": str,
         "recommendation": "accept"|"veto"|"decline", "assessment": str, "available": bool}
    `clear` is True ONLY on a high-confidence false positive (accept) with adequate opinion text --
    the sole condition under which the funnel may auto-publish a held case. Never raises: any error
    or thin text yields a fail-closed hold (clear=False, available=False)."""
    if _alpha(opinion_text) < MIN_TEXT_ALPHA:
        return _held("Opinion text unavailable or too thin to verify the flag; left for human review.")

    card = ("%s\nProposition (synopsis): %s\nWhy it matters: %s\nDisposition: %s\nAreas: %s"
            % (entry.get("name", ""), entry.get("synopsis", ""), entry.get("why", ""),
               entry.get("disposition", ""), ", ".join(entry.get("areas") or [])))
    user = ("FLAGGED CARD:\n%s\n\nWHY IT WAS HELD:\n- %s\n\nOPINION TEXT:\n%s"
            % (card, "\n- ".join(reasons or ["(unspecified)"]), opinion_text))
    try:
        v = call_json({"model": model, "max_tokens": out_tokens, "system": FABLE_SYSTEM,
                       "messages": [{"role": "user", "content": user}]}, "fable-review")
    except Exception as e:
        return _held("Fable review unavailable (%s); left for human review." % e)
    if not isinstance(v, dict):
        return _held("Fable review returned an unexpected shape; left for human review.")

    # Parse the boolean fail-closed: a model that emits the STRING "false" (a common JSON-adherence
    # slip) must not clear a hold, but bool("false") is True in Python. So a clear requires an actual
    # True, or a string that unambiguously means true; anything else -- "false", "", None, 0 -- is a
    # not-a-false-positive, which keeps the card held. This is the load-bearing gate: a wrong read here
    # auto-publishes a card the model actually flagged.
    raw_fp = v.get("is_false_positive")
    fp = raw_fp is True or (isinstance(raw_fp, str) and raw_fp.strip().lower() in ("true", "yes", "1"))
    conf = str(v.get("confidence", "")).lower()
    rec = str(v.get("recommendation", "")).lower()
    if rec not in ("accept", "veto", "decline"):
        rec = "veto"
    assessment = (str(v.get("assessment", "")).strip() or "(no assessment)")[:600]
    clear = fp and conf == "high" and rec == "accept"
    return {"clear": clear, "is_false_positive": fp, "confidence": conf or "low",
            "recommendation": rec, "assessment": assessment, "available": True}
