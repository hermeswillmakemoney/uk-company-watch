"""
UK Company Watch — Filing Check Cron Job.
Vercel cron: daily at 07:00 UTC. Checks Companies House for new filings, sends alerts.
"""

from flask import Flask, jsonify
import json
import os
import urllib.request
import urllib.parse
import base64
from datetime import datetime, timedelta

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CH_API_KEY = os.environ.get("CH_API_KEY", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import load_db, save_db, get_user_watched_companies, init_watchlists

FILING_TYPE_MAP = {
    "AA01": ("Annual return", "routine", "Annual compliance check-in. Nothing unusual."),
    "AR01": ("Annual return", "routine", "Annual compliance check-in. Nothing unusual."),
    "CH01": ("Director appointed", "notable", "New director on the board — could signal strategic shift or new investment."),
    "CH02": ("Director resigned", "concerning", "Board member left. Watch for follow-up resignations or instability."),
    "CH03": ("Director details changed", "routine", "Minor admin update to a director's recorded details."),
    "CS01": ("Confirmation statement", "routine", "Annual compliance filing. Confirms company details are current."),
    "AD01": ("Address changed", "notable", "Registered office moved. Could be growth, cost-cutting, or red flag if moving to a PO box."),
    "SH01": ("Shares allotted", "notable", "New shares issued — likely fundraising, employee options, or bringing in a new investor."),
    "SH06": ("Shares allotted", "notable", "New shares issued — likely fundraising, employee options, or bringing in a new investor."),
    "MG01": ("Mortgage/charge", "concerning", "Secured debt registered. Company has borrowed against assets."),
    "LIQ01": ("Liquidation", "critical", "Company is being wound up. Creditors should act immediately."),
    "WUO1": ("Winding up", "critical", "Company is being wound up. Creditors should act immediately."),
    "DS01": ("Dissolution", "concerning", "Company applied to be struck off. Could be dormant or closing down."),
    "GAZ1": ("Gazette notice", "notable", "Published in the London Gazette — often relates to insolvency or strike-off proceedings."),
    "GAZ2": ("Gazette notice", "notable", "Published in the London Gazette — often relates to insolvency or strike-off proceedings."),
    "TM01": ("Appointment terminated", "notable", "A director or officer's appointment was formally ended."),
    "TM02": ("Appointment terminated", "notable", "A director or officer's appointment was formally ended."),
    "AP01": ("Director appointed", "notable", "New director on the board — could signal strategic shift or new investment."),
    "AP04": ("Director appointed", "notable", "New director on the board — could signal strategic shift or new investment."),
    "NEWINC": ("Incorporation", "routine", "New company registered."),
    "CERTNM": ("Name change", "notable", "Company changed its name — could signal rebrand, pivot, or acquisition."),
    "MR01": ("Mortgage registered", "concerning", "A mortgage or charge registered against company assets."),
    "MR04": ("Mortgage satisfied", "routine", "A previous mortgage/charge has been paid off and released."),
    "PSC01": ("PSC update", "notable", "Person with Significant Control changed — ultimate ownership may have shifted."),
    "PSC02": ("PSC update", "notable", "Person with Significant Control changed — ultimate ownership may have shifted."),
    "RP01": ("Share buyback", "notable", "Company bought back its own shares — could signal confidence or insider activity."),
    "RR02": ("Re-registration", "notable", "Company changed its legal structure (e.g. private to public)."),
}


def generate_summary(company_name, company_number, filing_date, filing_type, description):
    if filing_type in FILING_TYPE_MAP:
        label, severity, explanation = FILING_TYPE_MAP[filing_type]
    else:
        label = filing_type
        severity = "routine"
        explanation = f"Filing type: {filing_type}."
    flag = ""
    if severity == "critical":
        flag = " 🔴"
    elif severity == "concerning":
        flag = " ⚠️"
    elif severity == "notable":
        flag = " 📌"
    return f"{company_name} ({company_number}): {label} on {filing_date}.{flag} {explanation}"


def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return False
    data = urllib.parse.urlencode({
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(f"{TELEGRAM_API}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def ch_fetch(path):
    url = f"https://api.companieshouse.gov.uk{path}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if CH_API_KEY:
            credentials = base64.b64encode(f"{CH_API_KEY}:".encode()).decode()
            req.add_header("Authorization", f"Basic {credentials}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"CH API error for {path}: {e}")
        return None


def handler(request):
    print(f"UCW Cron — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")

    db, sha = load_db()
    init_watchlists(db)

    # Collect all watched company numbers
    all_companies = set()
    for wl_code, members in db.get("watchlist_subscribers", {}).items():
        wl = db["watchlists"].get(wl_code, {})
        for entry in wl.get("companies", []):
            all_companies.add(entry["number"])
    for chat_id, companies in db.get("watched_companies", {}).items():
        for num in (companies if isinstance(companies, list) else []):
            all_companies.add(num)

    print(f"  {len(all_companies)} companies to check")

    # Check filings
    new_filings = []
    today = datetime.utcnow().strftime("%Y-%m-%d")
    known_set = set()
    for f in db.get("known_filings", []):
        known_set.add(f"{f['company_number']}|{f['filing_date']}|{f.get('filing_type', '')}")

    for num in all_companies:
        data = ch_fetch(f"/company/{num}/filing-history?items_per_page=1")
        if not data or "items" not in data:
            continue
        for f in data["items"][:1]:
            fdate = f.get("date", "")
            ftype = f.get("type", "")
            desc = f.get("description", "")
            key = f"{num}|{fdate}|{ftype}"
            if key not in known_set:
                known_set.add(key)
                name = db["known_companies"].get(num, {}).get("name", num)
                new_filings.append({
                    "company_number": num, "company_name": name,
                    "filing_date": fdate, "filing_type": ftype, "description": desc,
                })
                db["known_filings"].append({
                    "company_number": num, "filing_date": fdate,
                    "filing_type": ftype, "description": desc,
                })

    print(f"  {len(new_filings)} new filings")

    # Generate summaries
    for filing in new_filings:
        summary = generate_summary(
            filing["company_name"], filing["company_number"],
            filing["filing_date"], filing["filing_type"], filing["description"]
        )
        db["filing_summaries"].append({
            "company_number": filing["company_number"],
            "company_name": filing["company_name"],
            "filing_date": filing["filing_date"],
            "filing_type": filing["filing_type"],
            "summary": summary,
            "summary_date": today,
        })

    # Send per-user alerts
    sent = 0
    for chat_id in db["subscribers"]:
        sub = db["subscribers"][chat_id]
        companies = get_user_watched_companies(db, chat_id)
        user_summaries = [s for s in db["filing_summaries"]
                          if s["company_number"] in companies and s.get("summary_date") == today]

        if not user_summaries:
            continue

        # Check weekly alert limit
        max_alerts = sub.get("max_alerts_per_week", 3)
        alerts_sent_this_week = sub.get("alerts_sent_this_week", 0)
        week_start = sub.get("alert_week_start", "")

        # Reset counter on new week (Monday)
        today_date = datetime.utcnow().strftime("%Y-%m-%d")
        current_week_start = (datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())).strftime("%Y-%m-%d")
        if week_start != current_week_start:
            alerts_sent_this_week = 0
            sub["alert_week_start"] = current_week_start
            sub["alerts_sent_this_week"] = 0

        # Calculate remaining
        remaining = max_alerts - alerts_sent_this_week

        if remaining <= 0:
            # Limit hit — send warning if not already sent today
            if sub.get("limit_warning_sent_date") != today_date:
                reset_date = (datetime.utcnow() + timedelta(days=7 - datetime.utcnow().weekday())).strftime("%Y-%m-%d")
                send_telegram(chat_id,
                    f"⚠️ <b>Weekly alert limit reached</b>\n\n"
                    f"You've hit your limit of {max_alerts} alerts this week.\n"
                    f"Your counter resets on {reset_date} (Monday).\n\n"
                    f"Upgrade to /upgrade pro for 50 alerts/week, or /upgrade business for unlimited."
                )
                sub["limit_warning_sent_date"] = today_date
            continue

        # Send the alert
        lines = ["📋 <b>Your Company Alerts</b>\n"]
        for s in user_summaries:
            lines.append(f"🏢 <b>{s['company_name']}</b> ({s['company_number']})")
            lines.append(f"  {s['summary']}")
            lines.append("")
        send_telegram(chat_id, "\n".join(lines))

        # Update counter
        alerts_sent_this_week += 1
        sub["alerts_sent_this_week"] = alerts_sent_this_week
        sent += 1

        # If this was the last alert before limit, tell them
        if alerts_sent_this_week >= max_alerts:
            reset_date = (datetime.utcnow() + timedelta(days=7 - datetime.utcnow().weekday())).strftime("%Y-%m-%d")
            send_telegram(chat_id,
                f"⚠️ <b>Alert limit reached</b>\n\n"
                f"That was your last alert this week ({max_alerts}/{max_alerts}).\n"
                f"Counter resets on {reset_date} (Monday).\n\n"
                f"Upgrade: /upgrade pro"
            )

    print(f"  Alerts sent to {sent} users")

    # Cleanup old summaries (7 days)
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    db["filing_summaries"] = [s for s in db["filing_summaries"] if s.get("summary_date", "") >= cutoff]

    save_db(db, sha)
    print("Done.")
    return jsonify({"filings": len(new_filings), "alerts_sent": sent})


@app.route("/", methods=["GET", "POST"])
@app.route("/api/cron", methods=["GET", "POST"])
def cron_handler():
    return handler(None)
