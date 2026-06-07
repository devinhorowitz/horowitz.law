#!/usr/bin/env python3
"""Weekly email digest for Georgia Appellate Watch (Resend broadcast edition).

Reads opinions.json, selects the cases first seen within the lookback window, and sends a
short teaser as a Resend *broadcast* to a Segment of confirmed subscribers. Each case links
to its card on the site, and the synopsis is left on the page on purpose: the goal is to
drive readers to horowitz.law, not to replace it.

Recipients and unsubscribes are managed by Resend, not by this script. The broadcast targets
a Segment (everyone who confirmed via the double opt-in), is scoped to a Topic, and carries
the {{{RESEND_UNSUBSCRIBE_URL}}} merge tag so each recipient gets a managed, per-topic
unsubscribe link. There is no recipient list and no local suppression file.

Environment:
  RESEND_API_KEY     Resend API key with broadcast + contacts access (Full access). A
                     send-only key will not work here, unlike the old per-email send.
                     Without it (or with DIGEST_DRY_RUN), the script renders a preview only.
  RESEND_SEGMENT_ID  The Segment confirmed subscribers are added to. Required to send.
  RESEND_TOPIC_ID    The Topic to scope the send and the unsubscribe link. Recommended.
  DIGEST_FROM        From header. Default 'Georgia Appellate Watch <digest@horowitz.law>'.
  DIGEST_DAYS        Lookback window in days. Default 7.
  DIGEST_DRY_RUN     'true' to render a preview without creating anything. Default false.
  DIGEST_DRAFT       'true' to create the broadcast but NOT send it, so you can review and
                     send it from the Resend dashboard. Default false (create and send).
  SITE_URL           Base page URL. Default 'https://horowitz.law/opinions'.
  DIGEST_POSTAL      Optional physical address line for the footer.
  DIGEST_DISCLAIMER  Optional footer line (e.g. not-legal-advice / attorney-advertising).
  DIGEST_PREHEADER   Inbox preview line.
  DIGEST_PREVIEW     Where to write the rendered HTML in a dry run. Default digest_preview.html.
"""
import os, json, time, html, hashlib, datetime, textwrap
import urllib.request, urllib.error
import render  # shared COURT_LABELS / AREA_LABELS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(REPO, "opinions.json")

DAYS       = int((os.environ.get("DIGEST_DAYS") or "7"))
DRY_RUN    = (os.environ.get("DIGEST_DRY_RUN") or "").lower() in ("1", "true", "yes")
DRAFT      = (os.environ.get("DIGEST_DRAFT") or "").lower() in ("1", "true", "yes")  # create but do not send
SITE       = (os.environ.get("SITE_URL") or "https://horowitz.law/opinions").rstrip("/")
FROM       = os.environ.get("DIGEST_FROM") or "Georgia Appellate Watch <digest@horowitz.law>"
API_KEY    = os.environ.get("RESEND_API_KEY") or ""
SEGMENT_ID = os.environ.get("RESEND_SEGMENT_ID") or ""        # broadcast recipients live here
TOPIC_ID   = os.environ.get("RESEND_TOPIC_ID") or ""          # scopes the send + per-topic unsubscribe
POSTAL     = os.environ.get("DIGEST_POSTAL") or ""            # optional physical address in the footer
DISCLAIMER = os.environ.get("DIGEST_DISCLAIMER") or ""        # optional not-legal-advice / advertising line
PREHEADER  = os.environ.get("DIGEST_PREHEADER") or "New Georgia appellate decisions in civil litigation and insurance practice."
PREVIEW    = os.environ.get("DIGEST_PREVIEW") or os.path.join(REPO, "digest_preview.html")

# Resend fills this per recipient at send time; when a Topic is set, unsubscribing is scoped
# to that Topic. It must appear in the body, so the broadcast has a working unsubscribe link.
UNSUB_TAG = "{{{RESEND_UNSUBSCRIBE_URL}}}"

# Palette drawn from the site's light theme, kept email-client safe.
BG, CARD, FG, MUTED, ACCENT, BORDER = "#f5ede0", "#fffaf2", "#1a1a1a", "#6a6560", "#a4471a", "#d4cab8"


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


def select(entries, days):
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    new = [e for e in entries if (e.get("first_seen") or e.get("date") or "") >= since]
    new.sort(key=lambda e: (e.get("first_seen") or e.get("date") or "", e.get("date") or ""), reverse=True)
    return new, since


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


def build_html(new):
    rows = "".join(case_block(e) for e in new)
    intro = "%s added this week. Tap any case for the full holding and the opinion." % label_for(len(new))
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
          'margin-top:6px;">// Georgia Appellate Watch: new this week</div>' % ACCENT
        + '</td></tr>'
        + '<tr><td style="padding:6px 28px 0;font:15px/1.6 Georgia,&#39;Times New Roman&#39;,serif;color:%s;">' % FG
        + '<p style="margin:12px 0 2px;">%s</p></td></tr>' % esc(intro)
        + '<tr><td style="padding:0 28px;"><table role="presentation" width="100%%" '
          'cellpadding="0" cellspacing="0">%s</table></td></tr>' % rows
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


def build_text(new):
    lines = ["Georgia Appellate Watch", "%s added this week." % label_for(len(new)), "",
             "Read the summaries: %s" % SITE, ""]
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
    lines.append("You are receiving this because you subscribed to the Georgia Appellate Watch digest.")
    lines.append("Unsubscribe: %s" % UNSUB_TAG)
    if POSTAL:
        lines.append(POSTAL)
    if DISCLAIMER:
        lines.append(DISCLAIMER)
    return "\n".join(lines)


def send_broadcast(subject, html_body, text_body, idem=None):
    # One request creates the broadcast targeting the Segment and, with send=true, sends it.
    # Resend handles the recipient queue, throttling, and the per-recipient unsubscribe URL.
    body = {
        "from": FROM,
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "segment_id": SEGMENT_ID,
        "name": "Georgia Appellate Watch digest %s" % datetime.date.today().isoformat(),
        "send": (not DRAFT),
    }
    if TOPIC_ID:
        body["topic_id"] = TOPIC_ID
    req_headers = {"Authorization": "Bearer %s" % API_KEY, "Content-Type": "application/json",
                   "Accept": "application/json",
                   "User-Agent": "horowitz.law-appellate-watch/1.0 (+https://horowitz.law)"}
    if idem:
        req_headers["Idempotency-Key"] = idem  # same key on a retried run, so it is not duplicated
    req = urllib.request.Request(
        "https://api.resend.com/broadcasts", data=json.dumps(body).encode("utf-8"),
        headers=req_headers, method="POST")
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1)); last = e; continue
            raise RuntimeError("HTTP %s: %s" % (e.code, detail or e.reason))
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(3 * (attempt + 1)); last = e; continue
            raise
    if last:
        raise last


def main():
    entries = json.load(open(JSON_PATH, encoding="utf-8"))
    new, since = select(entries, DAYS)
    print("digest window: first_seen >= %s (%d days) | new cases: %d" % (since, DAYS, len(new)))
    if not new:
        print("nothing new in the window; not sending."); return
    subject = "Georgia Appellate Watch: %s (week of %s)" % (
        label_for(len(new)), fmt_date(datetime.date.today().isoformat()))
    html_body, text_body = build_html(new), build_text(new)
    if DRY_RUN or not API_KEY:
        open(PREVIEW, "w", encoding="utf-8").write(html_body)
        why = "DIGEST_DRY_RUN" if DRY_RUN else "no RESEND_API_KEY"
        print("[%s] preview written to %s, nothing created or sent." % (why, PREVIEW))
        print("subject: %s" % subject)
        print("target segment: %s | topic: %s" % (SEGMENT_ID or "(unset)", TOPIC_ID or "(unset)"))
        print("note: {{{RESEND_UNSUBSCRIBE_URL}}} shows literally in this preview; Resend fills it per recipient.")
        for e in new:
            print("  - %s [%s]" % (e["name"], ",".join(e.get("areas", []))))
        return
    if not SEGMENT_ID:
        print("RESEND_API_KEY is set but RESEND_SEGMENT_ID is empty; nothing to send."); return
    # Signature of today's send (date + the exact cases) so a retry reuses the same key.
    sig = hashlib.sha1(("%s|%s" % (datetime.date.today().isoformat(),
          ",".join(str(e.get("cluster_id")) for e in new))).encode("utf-8")).hexdigest()[:12]
    idem = "gaw-bcast-%s" % sig
    try:
        res = send_broadcast(subject, html_body, text_body, idem=idem) or {}
    except Exception as ex:
        print("FAILED to create/send broadcast: %s" % ex)
        raise SystemExit(1)
    bid = res.get("id", "?")
    if DRAFT:
        print("created DRAFT broadcast id=%s (not sent). Review and send it in the Resend dashboard." % bid)
    else:
        print("created and sent broadcast id=%s to segment %s (topic %s)." % (bid, SEGMENT_ID, TOPIC_ID or "none"))


if __name__ == "__main__":
    main()
