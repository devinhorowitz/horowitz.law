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


# ---------------------------------------------------------------------------
# Senior review of an ALREADY-PUBLISHED card (maintenance re-validation).
#
# Same escalation, opposite failure direction, and the difference is the whole design.
#
# review_held() runs BEFORE publication, so doubt means "keep holding" and its job is to
# decide whether a card may ship. review_published() runs after the card is already live on
# the site: the flag has been raised, a human is going to read it either way, and nothing
# this function returns may ever suppress that. It only ADDS -- an adjudication, the passage
# from the opinion that settles it, and a drafted correction. A failure, thin text, an
# unparseable answer or an ungrounded quote all degrade to "no review attached", never to a
# quieter flag.
#
# Why it exists: the maintenance guards flag a published card and file an issue carrying one
# line of reason. Acting on that means pulling the opinion from CourtListener by hand to find
# the passage that settles it -- which is what the 2026-08-09 fidelity flag on cluster
# 10357471 cost. maintain.py already has the full opinion text in hand at the moment it
# raises the flag, so the expensive part is already paid for.
#
# THE LOAD-BEARING GUARDRAIL IS THE QUOTE. A drafted correction to published legal text that
# rests on a passage the opinion does not contain is worse than no suggestion at all, so the
# quote must appear verbatim in the opinion before any suggestion is shown. That check is
# injected (`grounded`) for the same reason `call_json` is: this module must not import
# update.py, which imports it. maintain.py passes update._quote_substantiated so the house
# normalization is used; the built-in fallback is the same idea, conservatively.
# ---------------------------------------------------------------------------

PUBLISHED_SYSTEM = (
    "You are a senior appellate attorney reviewing a case-law card that is ALREADY PUBLISHED on a "
    "public legal-intelligence site for defense lawyers. An automated guard flagged it. Decide "
    "whether the flag is a GENUINE problem, and if it is, draft the minimal correction.\n\n"
    "Judge only from the OPINION TEXT provided. Your quote must be copied VERBATIM from that text "
    "-- it is checked against the opinion, and a suggestion whose quote is not found there is "
    "discarded. Never paraphrase into the quote field.\n\n"
    "Correct minimally. Change only what the flag is about; keep every accurate sentence as it "
    "stands, including wording you would have phrased differently. If the flag is mistaken, say so "
    "and leave both suggestions empty. If the opinion text cannot settle the question, say "
    "uncertain and leave them empty -- a wrong correction to live text is worse than none.\n\n"
    "Reply with ONLY a JSON object, no prose around it:\n"
    '{"verdict": "genuine"|"false_positive"|"uncertain", "confidence": "high"|"medium"|"low", '
    '"quote": "the passage from the OPINION TEXT that settles it, verbatim", '
    '"assessment": "one or two sentences on what is wrong and why it matters to a practitioner", '
    '"suggested_synopsis": "the corrected synopsis, in full, or empty if it needs no change", '
    '"suggested_why": "the corrected why-it-matters, in full, or empty if it needs no change"}'
)

MAX_SUGGESTION = 2000   # a synopsis is ~900 chars; this bounds a runaway answer, not real output


def _normalize(s):
    return " ".join((s or "").split()).lower()


def _appears_verbatim(quote, source):
    """Fallback grounding check: the quote must be a non-trivial span actually present in the
    source. Deliberately the same shape as update._quote_substantiated, which maintain.py
    injects instead -- this exists so the module is safe to call standalone, not to be a
    second opinion about what counts as grounded."""
    q = _normalize(quote)
    if len(q) < 4 or (len(q) < 6 and " " not in q):
        return False
    return q in _normalize(source)


def _no_review(reason):
    """The flag stands exactly as it was raised, with no suggestion attached."""
    return {"available": False, "verdict": "uncertain", "confidence": "low", "quote": "",
            "grounded": False, "assessment": reason, "suggested_synopsis": "", "suggested_why": ""}


def review_published(entry, reasons, opinion_text, call_json, grounded=None,
                     model="claude-fable-5", out_tokens=8000):
    """Adjudicate a guard flag on a PUBLISHED card and draft the correction.

    Returns:
        {"available": bool, "verdict": "genuine"|"false_positive"|"uncertain",
         "confidence": "high"|"medium"|"low", "quote": str, "grounded": bool,
         "assessment": str, "suggested_synopsis": str, "suggested_why": str}

    Never raises, and never reports anything that would justify dropping the flag: on any
    error, thin text, bad shape, or a quote the opinion does not contain, `available` is False
    (or `grounded` is False) and both suggestions are empty. Suggestions survive ONLY on
    verdict == "genuine" with a grounded quote.
    """
    if _alpha(opinion_text) < MIN_TEXT_ALPHA:
        return _no_review("Opinion text unavailable or too thin to review the flag.")
    card = ("%s\nSynopsis: %s\nWhy it matters: %s\nDisposition: %s\nAreas: %s"
            % (entry.get("name", ""), entry.get("synopsis", ""), entry.get("why", ""),
               entry.get("disposition", ""), ", ".join(entry.get("areas") or [])))
    user = ("PUBLISHED CARD:\n%s\n\nWHAT THE GUARD FLAGGED:\n- %s\n\nOPINION TEXT:\n%s"
            % (card, "\n- ".join(reasons or ["(unspecified)"]), opinion_text))
    try:
        v = call_json({"model": model, "max_tokens": out_tokens, "system": PUBLISHED_SYSTEM,
                       "messages": [{"role": "user", "content": user}]}, "fable-review-published")
    except Exception as e:
        return _no_review("Senior review unavailable (%s)." % e)
    if not isinstance(v, dict):
        return _no_review("Senior review returned an unexpected shape.")

    verdict = str(v.get("verdict", "")).strip().lower()
    if verdict not in ("genuine", "false_positive", "uncertain"):
        verdict = "uncertain"          # an unrecognized verdict is not a licence to suggest an edit
    conf = str(v.get("confidence", "")).strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    quote = str(v.get("quote", "")).strip()
    assessment = (str(v.get("assessment", "")).strip() or "(no assessment)")[:600]

    check = grounded or _appears_verbatim
    try:
        is_grounded = bool(quote) and bool(check(quote, opinion_text))
    except Exception:
        is_grounded = False            # a broken checker must not be read as "grounded"

    syn = str(v.get("suggested_synopsis", "") or "").strip()[:MAX_SUGGESTION]
    why = str(v.get("suggested_why", "") or "").strip()[:MAX_SUGGESTION]
    if verdict != "genuine" or not is_grounded:
        syn = why = ""                 # the only combination under which a correction is shown

    return {"available": True, "verdict": verdict, "confidence": conf, "quote": quote[:600],
            "grounded": is_grounded, "assessment": assessment,
            "suggested_synopsis": syn, "suggested_why": why}



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
