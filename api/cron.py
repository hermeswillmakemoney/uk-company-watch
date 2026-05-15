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
    "AA01": ("Annual return", "routine", "Company confirmed its details are up to date."),
    "AR01": ("Annual return", "routine", "Company filed its annual return."),
    "CH01": ("Director appointed", "notable", "A new director was appointed to the board."),
    "CH02": ("Director resigned", "concerning", "A director resigned from the board."),
    "CH03": ("Director details changed", "routine", "A director's details were updated."),
    "CS01": ("Confirmation statement", "routine", "Company filed its confirmation statement."),
    "AD01": ("Address changed", "notable", "Company changed its registered address."),
    "SH01": ("Shares allotted", "notable", "Company issued new shares."),
    "SH06": ("Shares allotted", "notable", "Company allotted new shares."),
    "MG01": ("Mortgage/charge", "concerning", "Company registered a mortgage or charge."),
    "LIQ01": ("Liquidation", "critical", "Company entered liquidation."),
    "WUO1": ("Winding up", "critical", "Company is being wound up."),
    "DS01": ("Dissolution", "concerning", "Company applied for dissolution."),
    "GAZ1": ("Gazette notice", "notable", "Gazette notice published."),
    "GAZ2": ("Gazette notice", "notable", "Gazette notice published."),
    "TM01": ("Termination", "notable", "A director or officer's appointment was terminated."),
    "TM02": ("Termination", "notable", "A director or officer's appointment was terminated."),
    "AP01": ("Director appointed", "notable", "A new director was appointed."),
    "AP04": ("Director appointed", "notable", "A new director was appointed."),
    "NEWINC": ("Incorporation", "routine", "New company incorporated."),
    "CERTNM": ("Name change", "notable", "Company changed its name."),
    "MR01": ("Mortgage registered", "concerning", "A mortgage was registered against the company."),
    "MR04": ("Mortgage satisfied", "routine", "A mortgage was satisfied/released."),
    "PSC01": ("PSC update", "notable", "Person with Significant Control information updated."),
    "PSC02": ("PSC update", "notable", "PSC information changed."),
    "RP01": ("Share buyback", "notable", "Company purchased its own shares."),
    "RR02": ("Re-registration", "notable", "Company re-registered."),
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


def get_company_details(db, num):
    """Fetch and cache company details from Companies House."""
    if num in db.get("known_companies") and "status" in db["known_companies"][num]:
        return db["known_companies"][num]
    data = ch_fetch(f"/company/{num}")
    if data:
        db["known_companies"][num] = {
            "name": data.get("company_name", num),
            "status": data.get("company_status", ""),
            "type": data.get("type", ""),
            "incorporated": data.get("date_of_creation", ""),
            "sic_codes": data.get("sic_codes", []),
            "address": data.get("registered_office_address", {}).get("address_line_1", ""),
        }
        return db["known_companies"][num]
    return {"name": num, "status": "", "type": "", "incorporated": "", "sic_codes": [], "address": ""}


def format_alert_line(filing, company_details):
    """Format a single filing alert line with company context."""
    summary = generate_summary(
        filing["company_name"], filing["company_number"],
        filing["filing_date"], filing["filing_type"], filing["description"]
    )
    parts = [summary]

    # Add company age context
    if company_details.get("incorporated"):
        try:
            inc_year = int(company_details["incorporated"][:4])
            age = datetime.utcnow().year - inc_year
            parts.append(f"Age: {age}y")
        except (ValueError, IndexError):
            pass

    # Add status if not active
    status = company_details.get("status", "")
    if status and status != "active":
        parts.append(f"Status: {status}")

    # Add SIC code description (first one)
    sic = company_details.get("sic_codes", [])
    if sic:
        parts.append(f"Sector: {sic[0]}")

    return " | ".join(parts)


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

    # Fetch company details for new filings and generate summaries
    for filing in new_filings:
        details = get_company_details(db, filing["company_number"])
        filing["company_details"] = details
        summary = format_alert_line(filing, details)
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
        companies = get_user_watched_companies(db, chat_id)
        user_summaries = [s for s in db["filing_summaries"]
                          if s["company_number"] in companies and s.get("summary_date") == today]
        if user_summaries:
            lines = ["📋 <b>Your Company Alerts</b>\n"]
            for s in user_summaries:
                lines.append(f"🏢 <b>{s['company_name']}</b> ({s['company_number']})")
                lines.append(f"  {s['summary']}")
                lines.append("")
            send_telegram(chat_id, "\n".join(lines))
            sent += 1

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
