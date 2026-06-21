"""siteconfig.py -- single source of truth for site-wide values.

render.py (and update.py) read from here at render time, so the things most
likely to change as the site ages -- the domain, the author identity (a title
change like a promotion), the rolling display window, and the practice-area
taxonomy -- live in ONE file instead of being hunted down across pages. This
mirrors the jurisdictions.py registry pattern: edit a value here, re-render, and
it propagates everywhere it is used.

How identity reaches the static pages: render._stamp_identity fills any element
carrying a data-cfg hook from IDENTITY below --
    data-cfg-text="KEY"      rewrites the element's inner text
    data-cfg-content="KEY"   rewrites the element's content="" attribute
The hooks are plain HTML5 data-* attributes: invisible, valid, and persistent,
so a render fills them again next time. Changing ROLE/ROLE_LINE here updates the
title, the Open Graph and Twitter cards, the page descriptions, the hero byline,
and the resume role entries in one move.
"""

# ---- Site ---------------------------------------------------------------
SITE_URL    = "https://horowitz.law"          # canonical origin, no trailing slash
ARCHIVE_URL = SITE_URL + "/archive"

# ---- Author / identity (a promotion is a one-line change here) ----------
NAME       = "Devin R. Horowitz"
ROLE       = "Partner"                                 # current title
ROLE_LINE  = "Partner, Civil Litigation Attorney"      # title-tag / OG card form
ROLE_SHORT = "Partner \u00b7 Civil Litigation"         # hero-byline form (middle dot)
EMAIL      = "devin@horowitz.law"
FIRM       = "Quintairos, Prieto, Wood & Boyer, P.A."  # raw &; render escapes on inject
FIRM_URL   = "https://qpwblaw.com/"

_DASH = "\u2014"  # em dash used as the title separator, kept to match existing copy

# Keys the page data-cfg hooks resolve against. Composed strings interpolate the
# fields above so a single ROLE/ROLE_LINE edit ripples through all of them.
# The curated core named in every Georgia Appellate Watch description: the Georgia appellate
# courts plus the federal courts that reach a Georgia civil practice. Defined once so the meta
# descriptions, the RSS feed, and the email preheader cannot drift apart again. Supplementary
# Florida and Alabama are deliberately not listed here; they are an "also pulled" mention in the
# colophon and the page subtitle, not a promise in the feed descriptions.
COVERAGE = "Georgia appellate, Eleventh Circuit, and U.S. Supreme Court"

IDENTITY = {
    "name":           NAME,
    "role":           ROLE,
    "role_line":      ROLE_LINE,
    "role_short":     ROLE_SHORT,
    "email":          EMAIL,
    "name_role_line": f"{NAME} {_DASH} {ROLE_LINE}",
    "desc_index":     (f"{NAME} {_DASH} {ROLE} and civil litigation attorney with a decade of "
                       "Georgia trial experience. Personal injury, complex liability. Based in metro Atlanta."),
    "og_desc_index":  (f"{ROLE} and civil litigation attorney with a decade of Georgia trial "
                       "experience. Personal injury and complex liability matters."),
    "desc_opinions":    (f"Curated, AI-assisted synopses of recent {COVERAGE} opinions, filtered "
                         "for civil litigation and insurance practice."),
    "og_desc_opinions": (f"Curated, AI-assisted synopses of recent {COVERAGE} opinions for civil "
                         "litigation and insurance practice."),
    "desc_archive":     (f"The complete Georgia Appellate Watch record, organized by year. AI-assisted "
                         f"synopses of {COVERAGE} opinions for civil litigation and insurance practice."),
    "desc_subscribe":   (f"Subscribe to the Georgia Appellate Watch weekly email: new {COVERAGE} "
                         "decisions for civil litigation and insurance practice."),
    "og_desc_resume":   "Civil litigation attorney with a decade of Georgia trial experience.",
    "firm":             FIRM,
    "og_image_alt":     f"{NAME} {_DASH} {ROLE_LINE}, Georgia state and federal courts.",
}

# Metadata used by render.py for the generated feeds and permalinks (JSON-LD).
AUTHOR_NAME      = NAME
AUTHOR_URL       = SITE_URL + "/"
PUBLISHER_NAME   = NAME

# ---- Public feed window -------------------------------------------------
WINDOW_YEARS = 2

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
def years_word(n=WINDOW_YEARS):
    """2 -> 'two years', 1 -> 'one year', 13 -> '13 years'. Keeps the prose in the
    RSS description and the feed intros in sync with WINDOW_YEARS automatically."""
    word = _ONES[n] if 0 <= n < len(_ONES) else str(n)
    return word + (" year" if n == 1 else " years")

# ---- Practice-area taxonomy (code -> human label) -----------------------
# render.py reads the labels; update.py builds its classifier code-list from
# AREA_CODES, so the enum is defined once instead of in three places.
AREA_LABELS = {
    "coverage": "coverage", "badfaith": "bad faith", "auto": "auto",
    "premises": "premises", "negsec": "negligent security", "expert": "expert",
    "procedure": "procedure", "damages": "damages",
}
AREA_CODES = list(AREA_LABELS)   # ["coverage", "badfaith", ...] for prompt text

