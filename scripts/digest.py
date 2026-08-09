#!/usr/bin/env python3
"""Weekly email digest for Georgia Appellate Watch (Resend broadcast edition).

Reads opinions.json, selects the cases first seen within the lookback window, and sends a
short teaser as a Resend *broadcast* to a Segment of confirmed subscribers. Each case links
to its card on the site, and the synopsis is left on the page on purpose: the goal is to
drive readers to horowitz.law, not to replace it.

It also signals the week's CORRECTIONS. When the daily forward escalation or the weekend
reverse sweep flags an earlier card as treated adversely by a later decision, that card's
treatment_date is stamped. This digest reads the merged opinions.json, so it sees only
corrections that have actually been merged, and lists any whose treatment_date falls inside
the window under a "flagged this week" section. Run it last in the weekly cycle, after the
daily updates and the weekend sweep, and after the week's correction PRs are merged, so the
email reflects and signals the corrected state. (The workflow_dispatch trigger lets you run
it by hand right after merging, if you want exact control.)

Recipients and unsubscribes are managed by Resend, not by this script. The broadcast targets
a Segment (everyone who confirmed via the double opt-in), is scoped to a Topic, and carries
the {{{RESEND_UNSUBSCRIBE_URL}}} merge tag so each recipient gets a managed, per-topic
unsubscribe link. There is no recipient list and no local suppression file.

Environment:
  RESEND_API_KEY     Resend API key with broadcast + contacts access (Full access). A
                     send-only key will not work here, unlike the old per-email send.
                     Without it (or with DIGEST_DRY_RUN), the script renders a preview only.
  RESEND_SEGMENT_ID  The Segment confirmed subscribers are added to. Default in siteconfig.py.
  RESEND_TOPIC_ID    The Topic to scope the send and the unsubscribe link. Default in siteconfig.py.
  DIGEST_FROM        From header. Default in siteconfig.py.
  DIGEST_DAYS        Lookback window in days. Default in siteconfig.py.
  DIGEST_DRY_RUN     'true' to render a preview without creating anything. Default false.
  DIGEST_DRAFT       'true' to create the broadcast but NOT send it, so you can review and
                     send it from the Resend dashboard. Default false (create and send).
  SITE_URL           Base page URL. Default 'https://horowitz.law/opinions'.
  DIGEST_POSTAL      Physical address line for the footer (CAN-SPAM). Default in siteconfig.py.
  DIGEST_DISCLAIMER  Not-legal-advice / advertising footer line. Default in siteconfig.py.
  DIGEST_PREHEADER   Inbox preview line.
  DIGEST_PREVIEW     Where to write the rendered HTML in a dry run. Default digest_preview.html.

  --- Legislative & Regulatory Watch (a SEPARATE broadcast) ---
  Configured in siteconfig.py, not here: siteconfig.LEGISLATION_DIGEST says whether this email is
  MEANT to send, and the two ids below default from siteconfig. That distinction matters -- when
  the email is meant to send and the audience is empty, this warns loudly on every run instead of
  skipping in silence. It shipped the other way and went dormant for three weeks unnoticed when
  the repo Variables holding the ids were purged.
  RESEND_LEGISLATION_SEGMENT_ID  Override for the Segment of legislation opt-ins.
  RESEND_LEGISLATION_TOPIC_ID    Override for the Topic scoping the send + its unsubscribe.
  LEGISLATION_SITE_URL           Base page URL for the legislation email. Default
                     'https://horowitz.law/legislation'.
  DIGEST_LEG_PREVIEW             Where to write the legislation preview in a dry run.
  (The /subscribe "legislation & regulations" checkbox rides the existing `area` field, so mapping
  the "legislation" token in RESEND_AREA_TOPICS to the Topic above wires opt-in with no code change.)
"""
import os, json, time, html, datetime, textwrap
import urllib.request, urllib.error
import render  # shared COURT_LABELS / AREA_LABELS
import siteconfig  # shared COVERAGE phrase
import safeio  # GitHub Actions run-summary helper

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(REPO, "opinions.json")

DAYS       = int(os.environ.get("DIGEST_DAYS") or siteconfig.DIGEST_DAYS)
DRY_RUN    = (os.environ.get("DIGEST_DRY_RUN") or "").lower() in ("1", "true", "yes")
DRAFT      = (os.environ.get("DIGEST_DRAFT") or "").lower() in ("1", "true", "yes")  # create but do not send
SITE       = (os.environ.get("SITE_URL") or "https://horowitz.law/opinions").rstrip("/")
# Defaults live in siteconfig.py (the repo is the master of its own configuration); the env
# var -- a repo Variable, when one is set -- still wins, as a break-glass override.
FROM       = os.environ.get("DIGEST_FROM") or siteconfig.DIGEST_FROM
API_KEY    = os.environ.get("RESEND_API_KEY") or ""
SEGMENT_ID = os.environ.get("RESEND_SEGMENT_ID") or siteconfig.RESEND_SEGMENT_ID   # broadcast recipients live here
TOPIC_ID   = os.environ.get("RESEND_TOPIC_ID") or siteconfig.RESEND_TOPIC_ID       # scopes the send + per-topic unsubscribe
POSTAL     = os.environ.get("DIGEST_POSTAL") or siteconfig.DIGEST_POSTAL           # physical address in the footer (CAN-SPAM)
DISCLAIMER = os.environ.get("DIGEST_DISCLAIMER") or siteconfig.DIGEST_DISCLAIMER   # not-legal-advice / advertising line
PREHEADER  = os.environ.get("DIGEST_PREHEADER") or f"New {siteconfig.COVERAGE} decisions in civil litigation and insurance practice."
PREVIEW    = os.environ.get("DIGEST_PREVIEW") or os.path.join(REPO, "digest_preview.html")

# --- Legislative & Regulatory Watch digest: a SEPARATE weekly broadcast to its own opt-in audience
# and Topic, independent of the opinions digest above. OFF by default and structurally identical to
# the per-area topics: with no segment configured it previews only and sends nothing, so behavior is
# unchanged until the Resend audience/topic are wired. Reads the same card files the watch writes.
LEG_JSON_PATH  = os.path.join(REPO, "legislation.json")
REG_JSON_PATH  = os.path.join(REPO, "regulations.json")
LEG_SEGMENT_ID = os.environ.get("RESEND_LEGISLATION_SEGMENT_ID") or siteconfig.RESEND_LEGISLATION_SEGMENT_ID
LEG_TOPIC_ID   = os.environ.get("RESEND_LEGISLATION_TOPIC_ID") or siteconfig.RESEND_LEGISLATION_TOPIC_ID
LEG_SITE       = (os.environ.get("LEGISLATION_SITE_URL")
                  or siteconfig.SITE_URL + "/legislation").rstrip("/")   # derived, not a second copy of the domain
LEG_PREVIEW    = os.environ.get("DIGEST_LEG_PREVIEW") or os.path.join(REPO, "digest_legislation_preview.html")

# Per-area sends. RESEND_AREA_TOPICS maps area code -> Resend Topic id, e.g.
#   {"coverage":"top_...","premises":"top_..."}
# (same value as the Cloudflare Pages variable confirm.js reads). Unset or
# empty: no per-area broadcasts, and behavior is identical to before this
# feature existed. An area with no mapped topic, or no content in the window,
# is skipped. Each area send is its own broadcast, scoped to that area's Topic,
# so its unsubscribe link leaves only that area.
def _area_topics():
    try:
        raw = os.environ.get("RESEND_AREA_TOPICS")
        m = json.loads(raw) if raw else siteconfig.RESEND_AREA_TOPICS
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}
AREA_TOPICS = _area_topics()

# Resend fills this per recipient at send time; when a Topic is set, unsubscribing is scoped
# to that Topic. It must appear in the body, so the broadcast has a working unsubscribe link.
UNSUB_TAG = "{{{RESEND_UNSUBSCRIBE_URL}}}"

# Palette drawn from the site's light theme, kept email-client safe.
BG, CARD, FG, MUTED, ACCENT, BORDER = "#f5ede0", "#fffaf2", "#1a1a1a", "#6a6560", "#a4471a", "#d4cab8"

# Treatment status labels for the corrections signal.
STATUS_LABEL = {"caution": "Flagged for possible negative treatment",
                "negative": "Negative treatment", "superseded": "Superseded"}


def esc(s):
    return html.escape(s or "", quote=True)


def fmt_date(iso):
    try:
        d = datetime.date.fromisoformat(iso)
        return "%s %d, %d" % (d.strftime("%B"), d.day, d.year)
    except Exception:
        return iso or ""


def label_for(n):
    return "1 new opinion" if n == 1 else "%d new opinions" % n


def label_corrections(n):
    return "1 earlier decision flagged" if n == 1 else "%d earlier decisions flagged" % n


def select(entries, days):
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    new = [e for e in entries if (e.get("first_seen") or e.get("date") or "") >= since]
    new.sort(key=lambda e: (e.get("first_seen") or e.get("date") or "", e.get("date") or ""), reverse=True)
    return new, since


def in_area(e, area):
    """Card membership for a per-area digest, counting additional holdings, via
    the same all_areas the site's filters use."""
    return area in render.all_areas(e)


def select_corrections(entries, days):
    """Cards whose treatment the machine recorded inside the window: the week's
    corrections, where a later case treated an earlier card adversely. Read from
    the merged opinions.json, so only corrections already merged are signaled."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    cor = [e for e in entries
           if (e.get("treatment") or "ok") != "ok" and (e.get("treatment_date") or "") >= since]
    cor.sort(key=lambda e: (e.get("treatment_date") or "", e.get("date") or ""), reverse=True)
    return cor


def subject_line(new, corrections):
    today = fmt_date(datetime.date.today().isoformat())
    if new and corrections:
        return "Georgia Appellate Watch: %s, %d flagged (week of %s)" % (label_for(len(new)), len(corrections), today)
    if new:
        return "Georgia Appellate Watch: %s (week of %s)" % (label_for(len(new)), today)
    return "Georgia Appellate Watch: %s (week of %s)" % (label_corrections(len(corrections)), today)


def case_block(e):
    url = "%s#op-%d" % (SITE, e["cluster_id"])
    court = render.COURT_LABELS.get(e["court"], e["court"])
    bits = [esc(court), "decided %s" % esc(fmt_date(e.get("date", "")))]
    if (e.get("disposition") or "").strip():
        bits.append(esc(e["disposition"].strip()))
    meta = " &middot; ".join(bits)
    why = (e.get("why") or "").strip()
    why = (why[0].upper() + why[1:]) if why else ""
    why_html = ('<div style="font:14px/1.55 Georgia,&#39;Times New Roman&#39;,serif;'
                'color:%s;margin-top:7px;">%s</div>' % (FG, esc(why))) if why else ""
    tags = "".join(
        '<span style="display:inline-block;font:11px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;'
        'color:%s;border:1px solid %s;border-radius:999px;padding:1px 9px;margin:3px 5px 0 0;">%s</span>'
        % (MUTED, BORDER, esc(render.AREA_LABELS.get(a, a))) for a in e.get("areas", []))
    return (
        '<tr><td style="padding:15px 0;border-bottom:1px solid %s;">' % BORDER
        + '<a href="%s" style="font:600 17px/1.35 Georgia,&#39;Times New Roman&#39;,serif;'
          'color:%s;text-decoration:none;">%s</a>' % (url, ACCENT, esc(e["name"]))
        + '<div style="font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;'
          'margin-top:5px;">%s</div>' % (MUTED, meta)
        + why_html
        + ('<div style="margin-top:9px;">%s</div>' % tags if tags else "")
        + '</td></tr>'
    )


def correction_block(e):
    url = "%s#op-%d" % (SITE, e["cluster_id"])
    status = STATUS_LABEL.get(e.get("treatment"), "Flagged")
    note = (e.get("treatment_note") or e.get("treatment_auto_note") or "").strip()
    by = "; ".join(b.get("name", "") for b in (e.get("treated_by") or []) if b.get("name"))
    detail = note or (("Cited by %s." % by) if by else "")
    return (
        '<tr><td style="padding:13px 0;border-bottom:1px solid %s;">' % BORDER
        + '<a href="%s" style="font:600 16px/1.35 Georgia,&#39;Times New Roman&#39;,serif;'
          'color:%s;text-decoration:none;">%s</a>' % (url, ACCENT, esc(e["name"]))
        + '<div style="font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;'
          'margin-top:4px;font-weight:600;">%s</div>' % (MUTED, esc(status))
        + (('<div style="font:13px/1.55 Georgia,&#39;Times New Roman&#39;,serif;color:%s;'
            'margin-top:6px;">%s</div>' % (FG, esc(detail))) if detail else "")
        + '</td></tr>'
    )


def build_html(new, corrections):
    subtitle = "new this week" if new else "flagged this week"
    intro = ("%s added this week. Tap any case for the full holding and the opinion." % label_for(len(new))
             if new else "No new decisions this week.")
    new_section = (('<tr><td style="padding:0 28px;"><table role="presentation" width="100%%" '
                    'cellpadding="0" cellspacing="0">%s</table></td></tr>'
                    % "".join(case_block(e) for e in new)) if new else "")
    corr_section = ""
    if corrections:
        corr_section = (
            '<tr><td style="padding:18px 28px 0;">'
            + '<div style="font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;color:%s;'
              'border-top:1px solid %s;padding-top:16px;">// %s</div>' % (ACCENT, BORDER, esc(label_corrections(len(corrections))))
            + '<div style="font:14px/1.55 Georgia,&#39;Times New Roman&#39;,serif;color:%s;margin-top:8px;">'
              'These earlier decisions were flagged after later cases treated them adversely. '
              'Confirm on a citator before relying.</div></td></tr>' % FG
            + '<tr><td style="padding:4px 28px 0;"><table role="presentation" width="100%%" '
              'cellpadding="0" cellspacing="0">%s</table></td></tr>' % "".join(correction_block(e) for e in corrections)
        )
    foot = ["You are receiving this because you subscribed to the Georgia Appellate Watch digest."]
    foot.append('To stop receiving it, <a href="%s" style="color:%s;">unsubscribe</a>.' % (UNSUB_TAG, MUTED))
    if POSTAL:
        foot.append(esc(POSTAL))
    if DISCLAIMER:
        foot.append(esc(DISCLAIMER))
    footer = "<br>".join(foot)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"><title>Georgia Appellate Watch</title></head>'
        '<body style="margin:0;padding:0;background:%s;">' % BG
        + '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;opacity:0;color:transparent;">%s</div>' % esc(PREHEADER)
        + '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:%s;">' % BG
        + '<tr><td align="center" style="padding:28px 16px;">'
        + '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
          'style="max-width:600px;width:100%%;background:%s;border:1px solid %s;border-radius:10px;">' % (CARD, BORDER)
        + '<tr><td style="padding:24px 28px 6px;">'
        + '<div style="font:700 15px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
          'color:%s;letter-spacing:.5px;">horowitz.law</div>' % FG
        + '<div style="font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;color:%s;'
          'margin-top:6px;">// Georgia Appellate Watch: %s</div>' % (ACCENT, esc(subtitle))
        + '</td></tr>'
        + '<tr><td style="padding:6px 28px 0;font:15px/1.6 Georgia,&#39;Times New Roman&#39;,serif;color:%s;">' % FG
        + '<p style="margin:12px 0 2px;">%s</p></td></tr>' % esc(intro)
        + new_section
        + corr_section
        + '<tr><td align="center" style="padding:24px 28px 28px;">'
        + '<a href="%s" style="display:inline-block;background:%s;color:%s;'
          'font:600 14px/1 -apple-system,Segoe UI,Roboto,sans-serif;text-decoration:none;'
          'padding:13px 24px;border-radius:8px;">Read all summaries &rarr;</a></td></tr>' % (SITE, ACCENT, BG)
        + '<tr><td style="padding:16px 28px 24px;border-top:1px solid %s;'
          'font:12px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;">%s</td></tr>' % (BORDER, MUTED, footer)
        + '</table>'
        + '<div style="font:11px/1.5 -apple-system,sans-serif;color:%s;margin-top:14px;">%s</div>' % (MUTED, esc(SITE))
        + '</td></tr></table></body></html>'
    )


def build_text(new, corrections):
    lines = ["Georgia Appellate Watch",
             ("%s added this week." % label_for(len(new))) if new else "No new decisions this week.",
             "", "Read the summaries: %s" % SITE, ""]
    for e in new:
        court = render.COURT_LABELS.get(e["court"], e["court"])
        bits = [court, "decided %s" % fmt_date(e.get("date", ""))]
        if (e.get("disposition") or "").strip():
            bits.append(e["disposition"].strip())
        why = (e.get("why") or "").strip()
        why = (why[0].upper() + why[1:]) if why else ""
        block = ["- %s" % e["name"], "  %s" % " | ".join(bits)]
        if why:
            block += ["  %s" % line for line in textwrap.wrap(why, 76)]
        block += ["  %s#op-%d" % (SITE, e["cluster_id"]), ""]
        lines += block
    if corrections:
        lines += ["Flagged this week (confirm on a citator before relying):", ""]
        for e in corrections:
            status = STATUS_LABEL.get(e.get("treatment"), "Flagged")
            note = (e.get("treatment_note") or e.get("treatment_auto_note") or "").strip()
            block = ["- %s" % e["name"], "  %s" % status]
            if note:
                block += ["  %s" % line for line in textwrap.wrap(note, 76)]
            block += ["  %s#op-%d" % (SITE, e["cluster_id"]), ""]
            lines += block
    lines.append("You are receiving this because you subscribed to the Georgia Appellate Watch digest.")
    lines.append("Unsubscribe: %s" % UNSUB_TAG)
    if POSTAL:
        lines.append(POSTAL)
    if DISCLAIMER:
        lines.append(DISCLAIMER)
    return "\n".join(lines)


UA_DIGEST = "horowitz.law-appellate-watch/1.0 (+https://horowitz.law)"


def _req(method, path, body=None):
    """One Resend API call. JSON in, JSON out; raises urllib.error.HTTPError on
    a non-2xx so callers can read the status and body."""
    headers = {"Authorization": "Bearer %s" % API_KEY, "Accept": "application/json",
               "User-Agent": UA_DIGEST}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com" + path, data=data,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def find_existing_broadcast(name):
    """The broadcast already created under `name`, or None.

    This name lookup is the dedup key for the whole send path. Resend honors the
    Idempotency-Key header only on POST /emails and /emails/batch (per its
    idempotency docs), NOT on /broadcasts, so a retried create after a lost
    response would otherwise make a second broadcast and mail every subscriber
    twice. The name is date-stamped, so one exists per send day at most.
    Best-effort: returns None on any error and lets the caller decide. First page
    only, which is ample at this volume."""
    try:
        data = _req("GET", "/broadcasts") or {}
        for b in (data.get("data") or []):
            if b.get("name") == name:
                return b
    except Exception as e:
        print("  ! broadcast existence check unavailable: %s" % e)
    return None


def send_existing_broadcast(bid):
    """Send an already-created broadcast by id. Retries transient failures, and
    treats an 'already sent/queued' rejection as success, so a retried send on the
    same id can never duplicate the mailing."""
    last = None
    for attempt in range(3):
        try:
            return _req("POST", "/broadcasts/%s/send" % bid, {}) or {"id": bid}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            low = detail.lower()
            if e.code in (400, 409, 422) and ("already" in low or "sent" in low or "queued" in low):
                print("  . broadcast %s reports already sent or queued; treating as success" % bid)
                return {"id": bid}
            last = "send HTTP %s: %s" % (e.code, detail or e.reason)
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            raise RuntimeError(last)
        except urllib.error.URLError as e:
            last = "send network error: %s" % getattr(e, "reason", e)
            if attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            raise RuntimeError(last)
    raise RuntimeError(last or "broadcast send failed")


def send_broadcast(subject, html_body, text_body, name, topic_id=None, segment_id=None):
    """Create the day's broadcast and (unless DRAFT) send it, duplicate-proof.

    `segment_id` defaults to the opinions SEGMENT_ID; the legislation digest passes its own audience
    so it reaches only legislation opt-ins, not every opinions subscriber.

    Create and send are deliberately separate calls: the create is guarded by the
    name lookup (before the first attempt and again before every retry), and the
    send is by id with 'already sent' treated as success, so neither half can
    double-mail on a lost response. If a create fails ambiguously AND the existence
    check is also unavailable, this raises rather than blindly re-creating: a
    missed digest (surfaced red by the workflow's failure alert, resendable by
    hand) costs less than mailing the whole list twice. A leftover draft from a
    previously interrupted run is adopted and sent, so recovery is automatic."""
    created = find_existing_broadcast(name)
    if created:
        print("  . broadcast %r already exists (id=%s, status=%s); adopting it"
              % (name, created.get("id"), created.get("status") or "?"))
    else:
        body = {
            "from": FROM,
            "subject": subject,
            "html": html_body,
            "text": text_body,
            "segment_id": segment_id or SEGMENT_ID,
            "name": name,
            "send": False,
        }
        top = topic_id or TOPIC_ID
        if top:
            body["topic_id"] = top
        last = None
        for attempt in range(4):
            try:
                created = _req("POST", "/broadcasts", body) or {}
                break
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
                last = "create HTTP %s: %s" % (e.code, detail or e.reason)
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    existing = find_existing_broadcast(name)
                    if existing:
                        print("  . retry found the broadcast already created (id=%s); adopting it"
                              % existing.get("id"))
                        created = existing
                        break
                    continue
                raise RuntimeError(last)
            except urllib.error.URLError as e:
                last = "create network error: %s" % getattr(e, "reason", e)
                if attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    existing = find_existing_broadcast(name)
                    if existing:
                        print("  . retry found the broadcast already created (id=%s); adopting it"
                              % existing.get("id"))
                        created = existing
                        break
                    continue
                raise RuntimeError(last)
        if created is None:
            raise RuntimeError(last or "broadcast create failed")
    bid = created.get("id")
    if not bid:
        raise RuntimeError("broadcast create returned no id: %r" % (created,))
    if DRAFT:
        return {"id": bid, "sent": False}
    status = (created.get("status") or "").lower()
    if status and status != "draft":
        print("  . broadcast %s is already %s; not sending again" % (bid, status))
        return {"id": bid, "sent": True}
    send_existing_broadcast(bid)
    return {"id": bid, "sent": True}


# --------------------------------------------------------------------------- #
# Legislative & Regulatory Watch digest (a separate broadcast).               #
# --------------------------------------------------------------------------- #
_LEG_STATUS = {"enacted": "Enacted", "vetoed": "Vetoed"}


def _load_leg_cards(path):
    """Load a watch's card list (legislation.json / regulations.json); [] on any error (fail-open)."""
    try:
        data = json.load(open(path, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def select_leg(cards, days):
    """Cards first seen within the window, newest first. Both card types carry `first_seen` (discovery
    date) and a display date (status_date for a law, publication_date for a rule)."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    new = [c for c in cards if (c.get("first_seen") or "") >= since]
    new.sort(key=lambda c: (c.get("first_seen") or "",
                            c.get("status_date") or c.get("publication_date") or ""), reverse=True)
    return new, since


def leg_subject_line(leg, reg):
    today = fmt_date(datetime.date.today().isoformat())
    n = len(leg) + len(reg)
    what = "1 update" if n == 1 else "%d updates" % n
    return "Legislative & Regulatory Watch: %s (week of %s)" % (what, today)


def _leg_row(title, meta, body, tags, url):
    """One card row, shared by the law and rule blocks; url is the authoritative source page."""
    body_html = ('<div style="font:14px/1.55 Georgia,&#39;Times New Roman&#39;,serif;color:%s;'
                 'margin-top:7px;">%s</div>' % (FG, esc(body))) if body else ""
    tags_html = ("".join(
        '<span style="display:inline-block;font:11px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;'
        'color:%s;border:1px solid %s;border-radius:999px;padding:1px 9px;margin:3px 5px 0 0;">%s</span>'
        % (MUTED, BORDER, esc(t)) for t in tags))
    head = (('<a href="%s" style="font:600 17px/1.35 Georgia,&#39;Times New Roman&#39;,serif;'
             'color:%s;text-decoration:none;">%s</a>' % (url, ACCENT, esc(title))) if url
            else '<div style="font:600 17px/1.35 Georgia,serif;color:%s;">%s</div>' % (ACCENT, esc(title)))
    return ('<tr><td style="padding:15px 0;border-bottom:1px solid %s;">' % BORDER + head
            + '<div style="font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;'
              'margin-top:5px;">%s</div>' % (MUTED, esc(meta))
            + body_html
            + ('<div style="margin-top:9px;">%s</div>' % tags_html if tags_html else "")
            + '</td></tr>')


def law_block(c):
    num, title = (c.get("number") or "").strip(), (c.get("title") or "").strip()
    heading = ("%s — %s" % (num, title)).strip(" —") or title or num or "(untitled)"
    st = _LEG_STATUS.get((c.get("status") or "").lower(), (c.get("status") or "").title())
    juris = "U.S." if (c.get("state") == "US") else "Georgia"
    bits = [b for b in (juris, st, ("effective %s" % fmt_date(c["effective_date"])) if c.get("effective_date") else "") if b]
    body = " ".join(x for x in [(c.get("synopsis") or "").strip(),
                                ("Why it matters: " + c["impact"].strip()) if (c.get("impact") or "").strip() else ""] if x)
    tags = [render.AREA_LABELS.get(a, a) for a in (c.get("areas") or []) if a in render.AREA_LABELS]
    return _leg_row(heading, " · ".join(bits), body, tags, (c.get("url") or "").strip())


def rule_block(c):
    ag, title = (c.get("agency") or "").strip(), (c.get("title") or "").strip()
    heading = ("%s — %s" % (ag, title)).strip(" —") or title or "(untitled)"
    bits = [b for b in ((c.get("type") or "Rule").strip(), (c.get("cfr") or "").strip(),
                        ("effective %s" % fmt_date(c["effective_date"])) if c.get("effective_date") else "") if b]
    body = " ".join(x for x in [(c.get("synopsis") or "").strip(),
                                ("Why it matters: " + c["impact"].strip()) if (c.get("impact") or "").strip() else ""] if x)
    tags = [render.AREA_LABELS.get(a, a) for a in (c.get("areas") or []) if a in render.AREA_LABELS]
    return _leg_row(heading, " · ".join(bits), body, tags, (c.get("url") or "").strip())


def _leg_section(label, rows_html):
    return ('<tr><td style="padding:18px 28px 0;">'
            '<div style="font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;color:%s;'
            'border-top:1px solid %s;padding-top:16px;">// %s</div></td></tr>' % (ACCENT, BORDER, esc(label))
            + '<tr><td style="padding:2px 28px 0;"><table role="presentation" width="100%%" '
              'cellpadding="0" cellspacing="0">%s</table></td></tr>' % rows_html)


def build_leg_html(leg, reg):
    foot = ["You are receiving this because you subscribed to the horowitz.law Legislative & "
            "Regulatory Watch.",
            'To stop receiving it, <a href="%s" style="color:%s;">unsubscribe</a>.' % (UNSUB_TAG, MUTED)]
    if POSTAL:
        foot.append(esc(POSTAL))
    if DISCLAIMER:
        foot.append(esc(DISCLAIMER))
    sections = ""
    if leg:
        sections += _leg_section("Georgia legislation", "".join(law_block(c) for c in leg))
    if reg:
        sections += _leg_section("Federal regulations", "".join(rule_block(c) for c in reg))
    intro = ("%d new law%s and %d new rule%s this week. Each links to the source; the site carries "
             "the plain-English summary." % (len(leg), "" if len(leg) == 1 else "s",
                                             len(reg), "" if len(reg) == 1 else "s"))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"><title>Legislative &amp; Regulatory Watch</title></head>'
        '<body style="margin:0;padding:0;background:%s;">' % BG
        + '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:%s;">' % BG
        + '<tr><td align="center" style="padding:28px 16px;">'
        + '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
          'style="max-width:600px;width:100%%;background:%s;border:1px solid %s;border-radius:10px;">' % (CARD, BORDER)
        + '<tr><td style="padding:24px 28px 6px;">'
        + '<div style="font:700 15px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:%s;'
          'letter-spacing:.5px;">horowitz.law</div>' % FG
        + '<div style="font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;color:%s;margin-top:6px;">'
          '// Legislative &amp; Regulatory Watch</div>' % ACCENT
        + '</td></tr>'
        + '<tr><td style="padding:6px 28px 0;font:15px/1.6 Georgia,&#39;Times New Roman&#39;,serif;color:%s;">' % FG
        + '<p style="margin:12px 0 2px;">%s</p></td></tr>' % esc(intro)
        + sections
        + '<tr><td align="center" style="padding:24px 28px 28px;">'
        + '<a href="%s" style="display:inline-block;background:%s;color:%s;'
          'font:600 14px/1 -apple-system,Segoe UI,Roboto,sans-serif;text-decoration:none;'
          'padding:13px 24px;border-radius:8px;">Read the summaries &rarr;</a></td></tr>' % (LEG_SITE, ACCENT, BG)
        + '<tr><td style="padding:16px 28px 24px;border-top:1px solid %s;'
          'font:12px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;">%s</td></tr>' % (BORDER, MUTED, "<br>".join(foot))
        + '</table>'
        + '<div style="font:11px/1.5 -apple-system,sans-serif;color:%s;margin-top:14px;">%s</div>' % (MUTED, esc(LEG_SITE))
        + '</td></tr></table></body></html>'
    )


def build_leg_text(leg, reg):
    lines = ["horowitz.law — Legislative & Regulatory Watch",
             "%d new law(s) and %d new rule(s) this week." % (len(leg), len(reg)),
             "", "Read the summaries: %s" % LEG_SITE, ""]
    if leg:
        lines += ["Georgia legislation:", ""]
        for c in leg:
            heading = ("%s — %s" % ((c.get("number") or "").strip(), (c.get("title") or "").strip())).strip(" —")
            st = _LEG_STATUS.get((c.get("status") or "").lower(), c.get("status") or "")
            lines += ["- %s" % heading, "  %s" % st]
            syn = (c.get("synopsis") or "").strip()
            if syn:
                lines += ["  %s" % ln for ln in textwrap.wrap(syn, 76)]
            if (c.get("url") or "").strip():
                lines.append("  %s" % c["url"].strip())
            lines.append("")
    if reg:
        lines += ["Federal regulations:", ""]
        for c in reg:
            heading = ("%s — %s" % ((c.get("agency") or "").strip(), (c.get("title") or "").strip())).strip(" —")
            lines += ["- %s" % heading, "  %s" % (c.get("type") or "Rule").strip()]
            syn = (c.get("synopsis") or "").strip()
            if syn:
                lines += ["  %s" % ln for ln in textwrap.wrap(syn, 76)]
            if (c.get("url") or "").strip():
                lines.append("  %s" % c["url"].strip())
            lines.append("")
    lines.append("You are receiving this because you subscribed to the horowitz.law "
                 "Legislative & Regulatory Watch.")
    lines.append("Unsubscribe: %s" % UNSUB_TAG)
    if POSTAL:
        lines.append(POSTAL)
    if DISCLAIMER:
        lines.append(DISCLAIMER)
    return "\n".join(lines)


def send_legislation_digest():
    """The Legislative & Regulatory Watch broadcast: a SEPARATE email to its own opt-in audience
    (RESEND_LEGISLATION_SEGMENT_ID) and Topic, sent only in weeks with new legislation/regulation
    cards. Fully independent of the opinions digest -- an opinions subscriber is not mailed this, and
    a quiet opinions week does not suppress it. With no key, or in a dry run, it previews only. With
    no SEGMENT it cannot send -- and whether that is a setting or a fault is decided by
    siteconfig.LEGISLATION_DIGEST, which is what makes an unconfigured-but-expected send loud."""
    leg, _ = select_leg(_load_leg_cards(LEG_JSON_PATH), DAYS)
    reg, _ = select_leg(_load_leg_cards(REG_JSON_PATH), DAYS)
    if not leg and not reg:
        print("legislation digest: nothing new in the window; not sending.")
        return
    subject = leg_subject_line(leg, reg)
    html_body, text_body = build_leg_html(leg, reg), build_leg_text(leg, reg)
    if DRY_RUN or not API_KEY:
        open(LEG_PREVIEW, "w", encoding="utf-8").write(html_body)
        why = "DIGEST_DRY_RUN" if DRY_RUN else "no RESEND_API_KEY"
        print("[%s] legislation preview -> %s (%d law(s), %d rule(s))"
              % (why, LEG_PREVIEW, len(leg), len(reg)))
        print("  subject: %s" % subject)
        print("  target segment: %s | topic: %s" % (LEG_SEGMENT_ID or "(unset)", LEG_TOPIC_ID or "(unset)"))
        return
    if not LEG_SEGMENT_ID:
        # An unset audience used to print one line and return, and the run stayed green. That is
        # how this email went dormant for three weeks without anyone noticing: the values lived
        # only as GitHub repo Variables, were purged, and nothing said so. A misconfiguration
        # that presents as a successful run is the failure mode this repo keeps paying for.
        #
        # So the two cases are now told apart, and only one of them is quiet. siteconfig
        # declares whether the email is MEANT to send; if it is, an unset audience is a fault
        # and says so as a workflow annotation and on the run summary, every week, until it is
        # fixed. It still does not abort: the opinions digest has already sent by this point,
        # and legislation must never fail a run that otherwise succeeded.
        if getattr(siteconfig, "LEGISLATION_DIGEST", False):
            msg = ("legislation digest: NOT SENT -- the audience is unset. Set "
                   "RESEND_LEGISLATION_SEGMENT_ID (and _TOPIC_ID) in scripts/siteconfig.py from "
                   "the Resend dashboard. %d law(s) and %d rule(s) went unsent this week."
                   % (len(leg), len(reg)))
            print("::warning::" + msg)
            safeio.step_summary("## Legislative & Regulatory Watch · NOT SENT\n\n" + msg)
        else:
            print("legislation digest: disabled (siteconfig.LEGISLATION_DIGEST is off); nothing to send.")
        return
    # Same CAN-SPAM postal gate as the opinions digest, but a skip (not a crash): the opinions send
    # already enforces it hard, and legislation must never abort a run that otherwise succeeded.
    if not POSTAL and (os.environ.get("DIGEST_ALLOW_NO_POSTAL") or "").lower() not in ("1", "true", "yes"):
        print("legislation digest: REFUSING TO SEND -- DIGEST_POSTAL is empty (CAN-SPAM). "
              "Set DIGEST_POSTAL, or DIGEST_ALLOW_NO_POSTAL=1 to send anyway.")
        return
    name = "Legislative & Regulatory Watch digest %s" % datetime.date.today().isoformat()
    try:
        res = send_broadcast(subject, html_body, text_body, name,
                             topic_id=LEG_TOPIC_ID, segment_id=LEG_SEGMENT_ID) or {}
    except Exception as ex:
        print("legislation digest FAILED to create/send: %s" % ex)
        return
    print("legislation digest: %s broadcast id=%s to segment %s (topic %s)."
          % ("drafted" if DRAFT else "sent", res.get("id", "?"), LEG_SEGMENT_ID, LEG_TOPIC_ID or "none"))
    safeio.step_summary("## Legislative & Regulatory Watch · weekly digest\n\n"
                        "%s %d new law(s) and %d new rule(s)."
                        % ("Drafted" if DRAFT else "Sent", len(leg), len(reg)))


def main():
    # The Legislative & Regulatory Watch is a separate broadcast to its own audience; run it first so
    # a quiet opinions week (which returns early below) never suppresses it. It is inert until its
    # Resend audience is configured.
    send_legislation_digest()
    entries = json.load(open(JSON_PATH, encoding="utf-8"))
    new, since = select(entries, DAYS)
    corrections = select_corrections(entries, DAYS)
    print("digest window: since %s (%d days) | new: %d | corrections: %d"
          % (since, DAYS, len(new), len(corrections)))
    if not new and not corrections:
        print("nothing new and no corrections in the window; not sending.")
        safeio.step_summary("## Georgia Appellate Watch \u00b7 weekly digest\n\n"
                            "Nothing new or corrected this window; no digest sent.")
        return
    subject = subject_line(new, corrections)
    html_body, text_body = build_html(new, corrections), build_text(new, corrections)
    if DRY_RUN or not API_KEY:
        open(PREVIEW, "w", encoding="utf-8").write(html_body)
        why = "DIGEST_DRY_RUN" if DRY_RUN else "no RESEND_API_KEY"
        print("[%s] preview written to %s, nothing created or sent." % (why, PREVIEW))
        print("subject: %s" % subject)
        print("target segment: %s | topic: %s" % (SEGMENT_ID or "(unset)", TOPIC_ID or "(unset)"))
        print("note: {{{RESEND_UNSUBSCRIBE_URL}}} shows literally in this preview; Resend fills it per recipient.")
        for e in new:
            print("  + new: %s [%s]" % (e["name"], ",".join(e.get("areas", []))))
        for e in corrections:
            print("  ~ flagged: %s [%s]" % (e["name"], e.get("treatment")))
        if AREA_TOPICS:
            for area, topic in sorted(AREA_TOPICS.items()):
                a_new = [e for e in new if in_area(e, area)]
                a_cor = [e for e in corrections if in_area(e, area)]
                if a_new or a_cor:
                    print("  > area digest %r -> topic %s: %d new, %d flagged"
                          % (area, topic or "(unmapped)", len(a_new), len(a_cor)))
        return
    if not SEGMENT_ID:
        print("RESEND_API_KEY is set but RESEND_SEGMENT_ID is empty; nothing to send."); return
    # Compliance gate. A commercial email must carry a valid physical postal address
    # (CAN-SPAM), and bar advertising rules may require an identification line. The
    # safeguard is structural: an empty DIGEST_POSTAL refuses to send rather than
    # quietly mailing without it. Override once with DIGEST_ALLOW_NO_POSTAL=1.
    if not POSTAL:
        if (os.environ.get("DIGEST_ALLOW_NO_POSTAL") or "").lower() in ("1", "true", "yes"):
            print("  ! WARNING: DIGEST_POSTAL is empty; sending anyway because "
                  "DIGEST_ALLOW_NO_POSTAL is set. Set siteconfig.DIGEST_POSTAL.")
        else:
            print("REFUSING TO SEND: DIGEST_POSTAL is empty. A commercial email needs a "
                  "physical postal address in the footer (CAN-SPAM); set "
                  "siteconfig.DIGEST_POSTAL. To send anyway this once, set DIGEST_ALLOW_NO_POSTAL=1.")
            raise SystemExit(1)
    if not DISCLAIMER:
        print("  ! note: DIGEST_DISCLAIMER is empty; confirm no identification or "
              "advertising line is required for this audience.")
    # The date-stamped name is the duplicate-proofing key for the whole send path;
    # see send_broadcast. One broadcast per send day, found by name on any retry.
    name = "Georgia Appellate Watch digest %s" % datetime.date.today().isoformat()
    try:
        res = send_broadcast(subject, html_body, text_body, name) or {}
    except Exception as ex:
        print("FAILED to create/send broadcast: %s" % ex)
        raise SystemExit(1)
    bid = res.get("id", "?")
    if DRAFT:
        print("created DRAFT broadcast id=%s (not sent). Review and send it in the Resend dashboard." % bid)
    else:
        print("created and sent broadcast id=%s to segment %s (topic %s)." % (bid, SEGMENT_ID, TOPIC_ID or "none"))

    safeio.step_summary(
        "## Georgia Appellate Watch \u00b7 weekly digest\n\n"
        "%s %d new opinion(s) and %d correction(s) to the main list."
        % ("Drafted" if DRAFT else "Sent", len(new), len(corrections)))

    # Per-area digests: one broadcast per area with content in the window,
    # scoped to that area's Topic so only its subscribers receive it and its
    # unsubscribe leaves only that area. The main broadcast above is untouched;
    # a contact opted into both gets both, by their own choice.
    if AREA_TOPICS:
        sent_areas = 0
        for area, topic in sorted(AREA_TOPICS.items()):
            if not topic:
                continue
            a_new = [e for e in new if in_area(e, area)]
            a_cor = [e for e in corrections if in_area(e, area)]
            if not a_new and not a_cor:
                continue
            label = render.AREA_LABELS.get(area, area)
            a_subject = "Georgia Appellate Watch \u00b7 %s: %s" % (
                label, subject_line(a_new, a_cor).split(": ", 1)[1])
            a_html, a_text = build_html(a_new, a_cor), build_text(a_new, a_cor)
            a_name = "Georgia Appellate Watch digest %s [%s]" % (datetime.date.today().isoformat(), area)
            try:
                a_res = send_broadcast(a_subject, a_html, a_text, a_name, topic_id=topic) or {}
                sent_areas += 1
                print("  + area %r: broadcast id=%s (topic %s, %d new, %d flagged)%s"
                      % (area, a_res.get("id", "?"), topic, len(a_new), len(a_cor),
                         " [draft]" if DRAFT else ""))
            except Exception as ex:
                print("  ! area %r FAILED: %s" % (area, ex))
        if not sent_areas:
            print("  . per-area topics configured, but no area had content this window.")


if __name__ == "__main__":
    main()
