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

# ---- Author / identity (a promotion, or a hand-off to a new owner, is an edit here) ----
# Name kept as parts so a re-user changes three obvious fields, not a full string, and so
# the granular forms (givenName/familyName, the app title, the monogram) derive from one
# place. Composed NAME adds the period after the middle initial.
NAME_FIRST  = "Devin"
NAME_MIDDLE = "R"          # middle initial, no period; composed forms add the dot
NAME_LAST   = "Horowitz"
NAME        = f"{NAME_FIRST} {NAME_MIDDLE}. {NAME_LAST}"
INITIALS    = NAME_FIRST[:1] + NAME_LAST[:1]           # "DH" -- QR monogram
ROLE       = "Partner"                                 # current title
ROLE_LINE  = "Partner, Civil Litigation Attorney"      # title-tag / OG card form
ROLE_SHORT = "Partner \u00b7 Civil Litigation"         # hero-byline form (middle dot)
EMAIL      = "devin@horowitz.law"                      # personal / site
EMAIL_FIRM = "devin.horowitz@qpwblaw.com"              # firm
# Phone kept as parts so the three surface forms (E.164 for JSON-LD, the tel: href with the
# DTMF extension, and the human display) stay in lockstep from one edit. Ext "" if none.
_PH_AREA = "770"; _PH_PRE = "650"; _PH_LINE = "8737"; _PH_EXT = "1983"
PHONE_E164    = f"+1-{_PH_AREA}-{_PH_PRE}-{_PH_LINE}"
PHONE_TEL     = f"+1{_PH_AREA}{_PH_PRE}{_PH_LINE}" + (f",{_PH_EXT}" if _PH_EXT else "")
PHONE_DISPLAY = f"({_PH_AREA}) {_PH_PRE}-{_PH_LINE}" + (f" ext. {_PH_EXT}" if _PH_EXT else "")
FIRM       = "Quintairos, Prieto, Wood & Boyer, P.A."  # raw &; render escapes on inject
FIRM_URL   = "https://qpwblaw.com/"
FIRM_PROFILE_URL = "https://qpwblaw.com/attorney/devin-r-horowitz/"
LINKEDIN_URL     = "https://www.linkedin.com/in/devinhorowitz/"

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
    "name_first":     NAME_FIRST,
    "name_last":      NAME_LAST,
    "initials":       INITIALS,
    "email_firm":     EMAIL_FIRM,
    "phone_display":  PHONE_DISPLAY,
    "phone_e164":     PHONE_E164,
    "href_email":        "mailto:" + EMAIL,
    "href_email_firm":   "mailto:" + EMAIL_FIRM,
    "href_tel":          "tel:" + PHONE_TEL,
    "href_firm_profile": FIRM_PROFILE_URL,
    "href_linkedin":     LINKEDIN_URL,
    "qr_label":          f"Scan to save {NAME} contact card",
    "name_role_line": f"{NAME} {_DASH} {ROLE_LINE}",
    "desc_index":     (f"{NAME} {_DASH} {ROLE} and civil litigation attorney with Georgia trial "
                       "experience since 2017. Personal injury, complex liability. Based in metro Atlanta."),
    "og_desc_index":  (f"{ROLE} and civil litigation attorney with Georgia trial experience "
                       "since 2017. Personal injury and complex liability matters."),
    "desc_opinions":    (f"Curated, AI-assisted synopses of recent {COVERAGE} opinions, filtered "
                         "for civil litigation and insurance practice."),
    "og_desc_opinions": (f"Curated, AI-assisted synopses of recent {COVERAGE} opinions for civil "
                         "litigation and insurance practice."),
    "desc_archive":     (f"The complete Georgia Appellate Watch record, organized by year. AI-assisted "
                         f"synopses of {COVERAGE} opinions for civil litigation and insurance practice."),
    "desc_subscribe":   (f"Subscribe to the Georgia Appellate Watch weekly email: new {COVERAGE} "
                         "decisions for civil litigation and insurance practice."),
    "og_desc_resume":   "Civil litigation attorney with Georgia trial experience since 2017.",
    "firm":             FIRM,
    "og_image_alt":     f"{NAME}, Civil Litigation Attorney, Georgia state and federal courts.",
}

# Metadata used by render.py for the generated feeds and permalinks (JSON-LD).
AUTHOR_NAME      = NAME
AUTHOR_URL       = SITE_URL + "/"
PUBLISHER_NAME   = NAME

# ---- Email (weekly digest + instant alerts) ------------------------------
# MIRRORED in wrangler.toml [vars]: the four values below (DIGEST_FROM, RESEND_SEGMENT_ID,
# RESEND_TOPIC_ID, and SITE_URL above) are also read by the Cloudflare Functions at request time,
# which cannot import this module. check_site.py asserts the two copies agree, so a change here
# without the matching wrangler.toml edit fails CI rather than silently splitting the subscribe
# flow from the digest send.
# The repo is the master of its own configuration: these lived as GitHub repository Variables,
# which nothing in the repo could read, review, or change by PR. Only true secrets stay in
# Actions secrets (RESEND_API_KEY and friends). The matching env vars still win when set, so a
# repo Variable remains available as a break-glass override -- but the committed value here is
# the source of truth. The Resend ids are not secrets: they are inert without RESEND_API_KEY
# and already ride in every sent email's unsubscribe plumbing.
DIGEST_DAYS   = 7     # lookback window, in days; matches the weekly cron
DIGEST_FROM   = "Georgia Appellate Watch <digest@horowitz.law>"
# CAN-SPAM postal line for the email footer; the same address the home page's JSON-LD publishes.
DIGEST_POSTAL = f"{NAME}, 365 Northridge Road, Suite 230, Atlanta, GA 30350"
# Not-legal-advice / advertising footer line (compliance copy -- edit deliberately).
DIGEST_DISCLAIMER = "NOT Legal Advice - You are advised to retain your own counsel."
RESEND_SEGMENT_ID = "1ae517f5-5981-4f7b-bed1-606cfa5649ab"   # confirmed subscribers Segment
RESEND_TOPIC_ID   = "52b81509-0230-4d3d-b5ff-393409df3b2c"   # send scope + per-topic unsubscribe

# ---- Legislative & Regulatory Watch opt-in email -------------------------
# These three belong here for the same reason as the four above, and were missed. The watch
# email landed 2026-07-18 (f268fef) reading them from GitHub repo Variables; the migration
# that made the repo master of its own configuration landed a week later (eed31af,
# 2026-07-25) and moved the opinions values only. When the repo Variables were later purged
# these had no remaining home, and the send went quietly dormant -- the run stayed green
# because an unset audience returns early.
#
# So empty here does NOT mean "off". LEGISLATION_DIGEST is what says whether the email is
# meant to send; when it is on and the audience is unset, digest.py says so loudly on every
# run instead of skipping in silence. Fill the two ids from the Resend dashboard
# (Audiences > Segments, and Topics) to switch it back on.
LEGISLATION_DIGEST = True    # this email is supposed to send; empty ids below are a fault, not a setting
RESEND_LEGISLATION_SEGMENT_ID = ""   # Resend Segment: legislation opt-in recipients
RESEND_LEGISLATION_TOPIC_ID   = ""   # Resend Topic: send scope + per-topic unsubscribe
# Practice-area code -> Resend Topic id, for the per-area opinion broadcasts. Empty means no
# per-area sends, which is the pre-feature behaviour and a legitimate setting.
RESEND_AREA_TOPICS = {}

# ---- Pipeline behaviour --------------------------------------------------
# Operational switches whose value must be visible, diffable and reviewable. The repo is the
# master of its own configuration: a setting that lives only as a repo Variable in the GitHub
# UI is invisible in review, absent from history, and lost if the repo is cloned or moved. So
# the value lives here and the matching env var is an OVERRIDE for a one-off run, never the
# place the real value is kept. Secrets are the only exception, and none of these are secret.

# Escalate a flagged published card to the Fable senior reviewer during maintenance
# re-validation, so the tracking issue carries a verdict, the passage from the opinion, and a
# drafted correction instead of one line of guard reason. Costs one Fable call per flag; the
# opinion text is already fetched. Off by default: a new model call on a production path is a
# choice someone makes deliberately. Override for one run with OPINIONS_MAINT_REVIEW=on.
MAINT_REVIEW = False

# ---- Top-level page registry --------------------------------------------
# The site's top-level pages, as (path, label, changefreq, priority, lastmod).
# render.py drives both the /404 "ls /" listing and the sitemap's static URLs
# from this, so adding a page is a single edit here. An empty label drops the
# page from the /404 listing (home is sitemap-only). An empty lastmod means
# render fills it from the feed data, for the pages whose content tracks the
# feed (opinions, archive, changes, stats, digests); a date is the hand-set
# value for the rarely-touched pages.
PAGES = [
    ("/",          "",           "monthly", "1.0", "2026-05-30"),
    ("/opinions",  "opinions/",  "weekly",  "0.8", ""),
    ("/legislation", "legislation", "weekly", "0.6", "2026-07-17"),
    ("/archive",   "archive/",   "weekly",  "0.5", ""),
    ("/changes",   "changes",    "weekly",  "0.5", ""),
    ("/stats",     "stats",      "weekly",  "0.3", ""),
    ("/digests",   "digests",    "weekly",  "0.4", ""),
    ("/subscribe", "subscribe",  "monthly", "0.4", "2026-06-10"),
    ("/resume",    "resume",     "monthly", "0.8", "2026-05-30"),
    ("/colophon",  "colophon",   "monthly", "0.5", "2026-05-30"),
]

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

# One-line gloss per code, prompt-injected by update.py so the classifier tags by the
# curated meaning of each code instead of guessing from a bare token. Written to the
# decides-under discipline: a code fits a holding only when the court decides a question
# OF that body of law, not when the facts merely involve its subject (the boundary that
# separated Martin v. Six Flags, a true negligent-security holding, from cases that only
# arise from a crime -- see the golden-set notes for Venetian Hills and the Kinsale retag).
# Keys must exactly match AREA_LABELS; update.py raises at import if they drift.
AREA_GLOSSES = {
    "coverage": ("insurance policy interpretation and scope: exclusions, UM/UIM coverage, duty to "
                 "defend or indemnify, declaratory rulings on coverage"),
    "badfaith": ("insurer bad faith and settlement conduct: O.C.G.A. 33-4-6 / 33-4-7 penalties, "
                 "Holt-type failure-to-settle excess exposure, and equivalent non-Georgia bad-faith law"),
    "auto": ("motor-vehicle tort liability: collisions and rules of the road, plus trucking and "
             "motor-carrier liability (including FAAAA preemption and broker claims); a UM/UIM or "
             "other motor-vehicle insurance dispute carries auto ALONGSIDE coverage"),
    "premises": ("owner/occupier liability for the premises: O.C.G.A. 51-3-1 duties, invitee status "
                 "and superior knowledge, hazards, and the possession boundary with out-of-possession "
                 "landlord liability under O.C.G.A. 44-7-14"),
    "negsec": ("negligent security -- liability for third-party criminal attacks: the duty to protect, "
               "foreseeability of crime (prior similar crimes, totality of the circumstances), and the "
               "adequacy or causal effect of security measures, under the Sturbridge line and O.C.G.A. "
               "51-3-50 et seq.; use only when the court decides such a question, never merely because "
               "the injury came from a crime"),
    "expert": ("expert opinion evidence: admissibility and methodology under O.C.G.A. 24-7-702 / "
               "Daubert, expert qualification, and expert-affidavit requirements"),
    "procedure": ("civil procedure and appellate practice: summary judgment, JNOV, and new-trial "
                  "standards, jury charges, discovery, service and limitations, appellate "
                  "jurisdiction and preservation"),
    "damages": ("measure and recoverability of damages: caps, punitive damages, apportionment of "
                "fault under O.C.G.A. 51-12-33, wrongful-death full value, attorney fees, remittitur"),
}

