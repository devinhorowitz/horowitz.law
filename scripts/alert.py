#!/usr/bin/env python3
"""Instant alert for a landmark decision (Georgia Appellate Watch).

Sent the day a card merges, not on the weekly cadence. Triggered by the
[instant-alert] flag in a merged opinions PR's body (.github/workflows/alert.yml),
or by hand with explicit ids. The cards to announce come from the merge itself:
the cluster_ids present in opinions.json at HEAD but not at HEAD~1. Reuses the
digest's Resend plumbing -- same segment, the main Topic, the same date+id
duplicate-proof broadcast naming, the same compliance gate -- so an alert can
no more double-send than the digest can.

Environment (the digest's, plus):
  ALERT_IDS   Optional comma-separated cluster_ids to announce, bypassing the
              git diff (for workflow_dispatch / local runs).
Without RESEND_API_KEY (or with DIGEST_DRY_RUN), prints the plan and writes a
preview; nothing is created or sent.
"""
import os, json, html, datetime, subprocess
import render
import digest  # the Resend plumbing: send_broadcast, palette, compliance gate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(REPO, "opinions.json")
PREVIEW = os.environ.get("ALERT_PREVIEW") or os.path.join(REPO, "alert_preview.html")


def esc(s):
    return html.escape(s or "", quote=True)


def new_ids_from_git():
    """cluster_ids added to opinions.json by the commit at HEAD: ids(HEAD) minus
    ids(HEAD~1). Returns [] when HEAD~1 is unavailable or had no opinions.json
    (first commit, shallow clone without depth 2), so a misconfigured trigger
    announces nothing rather than the wrong thing."""
    try:
        prev = subprocess.run(["git", "show", "HEAD~1:opinions.json"],
                              capture_output=True, text=True, cwd=REPO, timeout=30)
        if prev.returncode != 0:
            return []
        before = {int(e["cluster_id"]) for e in json.loads(prev.stdout)}
    except Exception:
        return []
    now = {int(e["cluster_id"]) for e in json.load(open(JSON_PATH, encoding="utf-8"))}
    return sorted(now - before)


def card_block(e):
    """One announced decision, linking its permanent page."""
    url = "https://horowitz.law/o/%d" % e["cluster_id"]
    court = render.COURT_LABELS.get(e["court"], e["court"])
    meta = " &middot; ".join([esc(court), "decided %s" % esc(digest.fmt_date(e.get("date", "")))]
                             + ([esc(e["disposition"].strip())] if (e.get("disposition") or "").strip() else []))
    why = (e.get("why") or "").strip()
    why = (why[0].upper() + why[1:]) if why else ""
    syn = (e.get("synopsis") or "").strip()
    return (
        '<tr><td style="padding:15px 0;border-bottom:1px solid %s;">' % digest.BORDER
        + '<a href="%s" style="font:600 18px/1.35 Georgia,&#39;Times New Roman&#39;,serif;'
          'color:%s;text-decoration:none;">%s</a>' % (url, digest.ACCENT, esc(e["name"]))
        + '<div style="font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;'
          'margin-top:5px;">%s</div>' % (digest.MUTED, meta)
        + ('<div style="font:14px/1.6 Georgia,&#39;Times New Roman&#39;,serif;color:%s;'
           'margin-top:8px;">%s</div>' % (digest.FG, esc(syn)) if syn else "")
        + ('<div style="font:14px/1.55 Georgia,&#39;Times New Roman&#39;,serif;color:%s;'
           'margin-top:7px;"><strong>Why it matters:</strong> %s</div>' % (digest.FG, esc(why)) if why else "")
        + '<div style="font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;'
          'margin-top:8px;font-style:italic;">AI-drafted summary &middot; the linked opinion is the authority.</div>' % digest.MUTED
        + '</td></tr>'
    )


def build_alert_html(cards):
    blocks = "".join(card_block(e) for e in cards)
    foot = ["You are receiving this because you subscribed to the Georgia Appellate Watch digest. "
            "Instant alerts go out only for decisions worth knowing the day they land."]
    foot.append('To stop receiving these, <a href="%s" style="color:%s;">unsubscribe</a>.'
                % (digest.UNSUB_TAG, digest.MUTED))
    if digest.POSTAL:
        foot.append(esc(digest.POSTAL))
    if digest.DISCLAIMER:
        foot.append(esc(digest.DISCLAIMER))
    footer = "<br>".join(foot)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"><title>Georgia Appellate Watch</title></head>'
        '<body style="margin:0;padding:0;background:%s;">' % digest.BG
        + '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:%s;">' % digest.BG
        + '<tr><td align="center" style="padding:28px 16px;">'
        + '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
          'style="max-width:600px;width:100%%;background:%s;border:1px solid %s;border-radius:10px;">' % (digest.CARD, digest.BORDER)
        + '<tr><td style="padding:24px 28px 6px;">'
        + '<div style="font:700 15px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
          'color:%s;letter-spacing:.5px;">horowitz.law</div>' % digest.FG
        + '<div style="font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;color:%s;'
          'margin-top:6px;">// instant alert: worth knowing today</div>' % digest.ACCENT
        + '</td></tr>'
        + '<tr><td style="padding:0 28px;"><table role="presentation" width="100%%" '
          'cellpadding="0" cellspacing="0">%s</table></td></tr>' % blocks
        + '<tr><td style="padding:16px 28px 24px;border-top:1px solid %s;'
          'font:12px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:%s;">%s</td></tr>' % (digest.BORDER, digest.MUTED, footer)
        + '</table></td></tr></table></body></html>'
    )


def build_alert_text(cards):
    lines = ["Georgia Appellate Watch -- instant alert", ""]
    for e in cards:
        court = render.COURT_LABELS.get(e["court"], e["court"])
        lines += ["- %s" % e["name"],
                  "  %s | decided %s" % (court, digest.fmt_date(e.get("date", ""))),
                  "  https://horowitz.law/o/%d" % e["cluster_id"], ""]
    lines.append("AI-drafted summaries; the linked opinions are the authority.")
    lines.append("Unsubscribe: %s" % digest.UNSUB_TAG)
    if digest.POSTAL:
        lines.append(digest.POSTAL)
    if digest.DISCLAIMER:
        lines.append(digest.DISCLAIMER)
    return "\n".join(lines)


def main():
    raw = (os.environ.get("ALERT_IDS") or "").strip()
    ids = sorted({int(x) for x in raw.split(",") if x.strip().isdigit()}) if raw else new_ids_from_git()
    if not ids:
        print("alert: no newly added cards found (and no ALERT_IDS); nothing to announce.")
        return
    entries = json.load(open(JSON_PATH, encoding="utf-8"))
    cards = [e for e in entries if int(e.get("cluster_id", 0)) in set(ids)]
    if not cards:
        print("alert: ids %r not present in opinions.json; nothing to announce." % ids)
        return
    cards.sort(key=lambda e: e.get("date") or "", reverse=True)
    if len(cards) == 1:
        subject = "Instant alert: %s" % cards[0]["name"]
    else:
        subject = "Instant alert: %d decisions worth knowing today" % len(cards)
    html_body, text_body = build_alert_html(cards), build_alert_text(cards)

    if digest.DRY_RUN or not digest.API_KEY:
        open(PREVIEW, "w", encoding="utf-8").write(html_body)
        why = "DIGEST_DRY_RUN" if digest.DRY_RUN else "no RESEND_API_KEY"
        print("[%s] preview written to %s, nothing created or sent." % (why, PREVIEW))
        print("subject: %s" % subject)
        for e in cards:
            print("  ! alert: %s (/o/%d)" % (e["name"], e["cluster_id"]))
        return
    if not digest.SEGMENT_ID:
        print("RESEND_API_KEY is set but RESEND_SEGMENT_ID is empty; nothing to send.")
        return
    if not digest.POSTAL and (os.environ.get("DIGEST_ALLOW_NO_POSTAL") or "").lower() not in ("1", "true", "yes"):
        print("REFUSING TO SEND: DIGEST_POSTAL is empty (CAN-SPAM). Set the repo Variable, "
              "or DIGEST_ALLOW_NO_POSTAL=1 to send this once.")
        raise SystemExit(1)
    # Same duplicate-proofing as the digest: a date+id-stamped name, looked up
    # before create and on every retry inside send_broadcast.
    name = "Instant alert %s [%s]" % (datetime.date.today().isoformat(),
                                      "+".join(str(i) for i in ids))
    try:
        res = digest.send_broadcast(subject, html_body, text_body, name) or {}
    except Exception as ex:
        print("FAILED to create/send alert broadcast: %s" % ex)
        raise SystemExit(1)
    print("created%s alert broadcast id=%s (%d card%s) to segment %s (topic %s)."
          % ("" if digest.DRAFT else " and sent", res.get("id", "?"),
             len(cards), "s" if len(cards) != 1 else "",
             digest.SEGMENT_ID, digest.TOPIC_ID or "none"))


if __name__ == "__main__":
    main()
