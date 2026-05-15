#!/usr/bin/env python3
"""
UK Company Watch — Telegram Bot + Companies House Monitor.
Run as a cron job every 60 minutes.

Environment variables needed:
  TELEGRAM_BOT_TOKEN — from @BotFather
  CH_API_KEY — optional Companies House API key (improves rate limits)
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
import base64
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ───
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CH_API_KEY = os.environ.get("CH_API_KEY", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
CH_API_BASE = "https://api.companieshouse.gov.uk"

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "uk_company_watch.db"
DATA_DIR.mkdir(exist_ok=True)

# ─── Database ───

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            plan TEXT DEFAULT 'free',
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            alerts_today INTEGER DEFAULT 0,
            last_alert_date TEXT,
            max_watched INTEGER DEFAULT 1,
            max_alerts_per_day INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS watched_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            company_number TEXT NOT NULL,
            company_name TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, company_number)
        );
        CREATE TABLE IF NOT EXISTS known_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_number TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            filing_type TEXT,
            description TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_number, filing_date, filing_type)
        );
        CREATE TABLE IF NOT EXISTS known_companies (
            company_number TEXT PRIMARY KEY,
            company_name TEXT,
            status TEXT,
            date_of_creation TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            alerted INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_filings_company ON known_filings(company_number);
        CREATE INDEX IF NOT EXISTS idx_watched_chat ON watched_companies(chat_id);
    """)
    conn.commit()
    return conn


# ─── Companies House API ───

def ch_fetch(path):
    url = f"{CH_API_BASE}{path}"
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


# ─── Telegram ───

def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        print(f"[Would send to {chat_id}]: {text[:200]}")
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


def get_updates(offset=None):
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"{TELEGRAM_API}/getUpdates?timeout=0&limit=50"
    if offset:
        url += f"&offset={offset}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        print(f"getUpdates error: {e}")
        return []


# ─── Command Handlers ───

def handle_start(chat_id):
    send_telegram(chat_id,
        "Welcome to UK Company Watch! 🇬🇧\n\n"
        "Get real-time alerts when UK companies:\n"
        "• File for insolvency\n"
        "• Change directors\n"
        "• File significant documents\n\n"
        "Commands:\n"
        "/search [name] — Search companies\n"
        "/company [number] — Get company details\n"
        "/watch [number] — Watch a company for alerts\n"
        "/watching — Your watched companies\n"
        "/digest — Today's summary\n"
        "/pricing — Upgrade to Pro\n\n"
        "Free: 3 alerts/day, 1 watched company"
    )


def handle_search(chat_id, query):
    if len(query) < 2:
        send_telegram(chat_id, "Query must be at least 2 characters.")
        return
    data = ch_fetch(f"/search/companies?q={urllib.parse.quote(query)}&items_per_page=5")
    if data and data.get("items"):
        lines = [f'Found {data.get("total_results", 0)} companies for "{query}":\n']
        for item in data["items"][:5]:
            lines.append(f'• <b>{item["title"]}</b> ({item["company_number"]}) — {item.get("company_status", "?")}')
            lines.append(f'  /company {item["company_number"]} | /watch {item["company_number"]}\n')
        send_telegram(chat_id, "\n".join(lines))
    else:
        send_telegram(chat_id, f'No results for "{query}".')


def handle_company(chat_id, num):
    data = ch_fetch(f"/company/{num}")
    if data:
        name = data.get("company_name", "Unknown")
        status = data.get("company_status", "?")
        addr = data.get("registered_office_address", {})
        address_parts = [addr.get(k) for k in ["address_line_1", "address_line_2", "locality", "postal_code"] if addr.get(k)]
        sic = ", ".join(data.get("sic_codes", [])) or "N/A"

        msg = f"📊 <b>{name}</b>\nNumber: {num}\nStatus: {status}\nType: {data.get('type', '?')}\n"
        msg += f"Founded: {data.get('date_of_creation', 'N/A')}\nSIC: {sic}\nAddress: {', '.join(address_parts) or 'N/A'}\n"
        if data.get("accounts", {}).get("overdue"):
            msg += "⚠️ Accounts OVERDUE\n"
        if data.get("confirmation_statement", {}).get("overdue"):
            msg += "⚠️ Confirmation statement OVERDUE\n"
        msg += f"\nView: https://find-and-update.company-information.service.gov.uk/company/{num}"
        send_telegram(chat_id, msg)
    else:
        send_telegram(chat_id, f"Company {num} not found.")


def handle_watch(chat_id, num, conn):
    c = conn.cursor()
    c.execute("SELECT max_watched FROM subscribers WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    max_watched = row[0] if row else 1
    c.execute("SELECT COUNT(*) FROM watched_companies WHERE chat_id = ?", (str(chat_id),))
    current = c.fetchone()[0]

    if current >= max_watched:
        send_telegram(chat_id, f"You're watching {current}/{max_watched} companies. Use /pricing to upgrade.")
        return

    data = ch_fetch(f"/company/{num}")
    name = data.get("company_name", num) if data else num

    try:
        c.execute("INSERT INTO watched_companies (chat_id, company_number, company_name) VALUES (?, ?, ?)",
                  (str(chat_id), num, name))
        conn.commit()
        send_telegram(chat_id, f"✅ Now watching <b>{name}</b> ({num}). You'll get alerts for new filings.")
    except sqlite3.IntegrityError:
        send_telegram(chat_id, f"You're already watching {name} ({num}).")


def handle_watching(chat_id, conn):
    c = conn.cursor()
    c.execute("SELECT company_number, company_name FROM watched_companies WHERE chat_id = ?", (str(chat_id),))
    watched = c.fetchall()
    if watched:
        lines = ["📋 Your watched companies:\n"]
        for num, name in watched:
            lines.append(f"• <b>{name}</b> ({num})")
        send_telegram(chat_id, "\n".join(lines))
    else:
        send_telegram(chat_id, "You're not watching any companies. Use /watch [number] to start.")


def handle_digest(chat_id, conn):
    c = conn.cursor()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) FROM known_companies WHERE date(first_seen) = ?", (today,))
    new_today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM known_companies WHERE status='insolvency' AND date(first_seen) = ?", (today,))
    ins_today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM known_filings WHERE date(first_seen) = ?", (today,))
    fil_today = c.fetchone()[0]

    send_telegram(chat_id,
        f"📊 <b>UK Company Watch — Daily Digest</b>\n"
        f"📅 {datetime.utcnow().strftime('%d %B %Y')}\n\n"
        f"🏢 New companies tracked: <b>{new_today}</b>\n"
        f"⚠️ Insolvencies: <b>{ins_today}</b>\n"
        f"📋 New filings: <b>{fil_today}</b>\n\n"
        f"Reply /help for commands."
    )


def handle_pricing(chat_id):
    send_telegram(chat_id,
        "📊 <b>UK Company Watch Pricing</b>\n\n"
        "🆓 <b>Free</b> — £0/month\n  • 3 alerts/day\n  • 1 watched company\n\n"
        "⭐ <b>Pro</b> — £4.99/month\n  • 50 alerts/day\n  • 10 watched companies\n\n"
        "🏢 <b>Business</b> — £19.99/month\n  • Unlimited alerts & companies\n  • Webhook + API access\n\n"
        "Upgrade: Send via PayPal to hermeswillmakesmoney@gmail.com\n"
        f"with note: UCW Pro [your chat id: {chat_id}]"
    )


# ─── Update Processing ───

def process_updates(conn):
    """Process incoming Telegram messages."""
    c = conn.cursor()
    c.execute("SELECT MAX(update_id) FROM (SELECT 0 as update_id)")  # placeholder
    # We track last update ID in a simple file
    offset_file = DATA_DIR / "last_update.txt"
    offset = None
    if offset_file.exists():
        try:
            offset = int(offset_file.read_text().strip()) + 1
        except ValueError:
            pass

    updates = get_updates(offset=offset)
    if not updates:
        return

    last_id = 0
    for update in updates:
        update_id = update["update_id"]
        last_id = max(last_id, update_id)

        if "message" not in update:
            continue
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()

        if not text:
            continue

        # Auto-register
        c.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
        conn.commit()

        if text == "/start":
            handle_start(chat_id)
        elif text == "/help":
            send_telegram(chat_id, "Commands:\n/search [name]\n/company [number]\n/watch [number]\n/watching\n/digest\n/pricing")
        elif text.startswith("/search "):
            handle_search(chat_id, text[8:].strip())
        elif text.startswith("/company "):
            handle_company(chat_id, text[9:].strip())
        elif text.startswith("/watch "):
            handle_watch(chat_id, text[7:].strip(), conn)
        elif text == "/watching":
            handle_watching(chat_id, conn)
        elif text == "/digest":
            handle_digest(chat_id, conn)
        elif text == "/pricing":
            handle_pricing(chat_id)
        else:
            send_telegram(chat_id, "Unknown command. Reply /help for available commands.")

        time.sleep(0.1)  # Rate limit

    if last_id > 0:
        offset_file.write_text(str(last_id))


# ─── Monitoring ───

def check_insolvencies(conn):
    """Check for new insolvency cases."""
    print("Checking insolvencies...")
    data = ch_fetch("/search/companies?q=&company_status=insolvency&items_per_page=30")
    if not data or "items" not in data:
        return 0

    c = conn.cursor()
    new_count = 0
    for item in data["items"]:
        num = item["company_number"]
        name = item.get("title", "Unknown")
        try:
            c.execute("INSERT INTO known_companies (company_number, company_name, status, date_of_creation) VALUES (?, ?, ?, ?)",
                      (num, name, "insolvency", item.get("date_of_creation", "")))
            conn.commit()
            new_count += 1
            print(f"  New insolvency: {name} ({num})")
        except sqlite3.IntegrityError:
            pass
    return new_count


def check_new_companies(conn):
    """Check for newly incorporated companies."""
    print("Checking new companies...")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = ch_fetch(f"/advanced-search/companies?incorporated_from={yesterday}&items_per_page=50")
    if not data or "items" not in data:
        return 0

    c = conn.cursor()
    new_count = 0
    for item in data["items"]:
        num = item["company_number"]
        name = item.get("title", "Unknown")
        try:
            c.execute("INSERT INTO known_companies (company_number, company_name, status, date_of_creation) VALUES (?, ?, ?, ?)",
                      (num, name, item.get("company_status", "active"), item.get("date_of_creation", "")))
            conn.commit()
            new_count += 1
        except sqlite3.IntegrityError:
            pass
    return new_count


def check_watched_filings(conn):
    """Check filing history for watched companies."""
    print("Checking watched companies...")
    c = conn.cursor()
    c.execute("SELECT DISTINCT company_number FROM watched_companies")
    watched = [row[0] for row in c.fetchall()]
    if not watched:
        return []

    new_filings = []
    for num in watched:
        data = ch_fetch(f"/company/{num}/filing-history?items_per_page=3")
        if not data or "items" not in data:
            continue
        for f in data["items"]:
            fdate = f.get("date", "")
            ftype = f.get("type", "")
            desc = f.get("description", "")
            try:
                c.execute("INSERT INTO known_filings (company_number, filing_date, filing_type, description) VALUES (?, ?, ?, ?)",
                          (num, fdate, ftype, desc))
                conn.commit()
                new_filings.append({"company_number": num, "date": fdate, "type": ftype, "description": desc})
                print(f"  New filing: {num} — {fdate} {ftype}")
            except sqlite3.IntegrityError:
                pass
        time.sleep(0.5)  # Rate limit: 60 req/5min without API key

    return new_filings


def send_insolvency_alerts(conn, new_insolvencies):
    """Send insolvency alerts to all subscribers."""
    if new_insolvencies <= 0:
        return

    c = conn.cursor()
    c.execute("SELECT company_number, company_name FROM known_companies WHERE status='insolvency' AND alerted=0 LIMIT 5")
    unalerted = c.fetchall()
    if not unalerted:
        return

    c.execute("SELECT chat_id FROM subscribers")
    subs = [row[0] for row in c.fetchall()]
    if not subs:
        return

    msg = "⚠️ <b>New Insolvency Alerts</b>\n\n"
    for num, name in unalerted[:5]:
        msg += f"• <b>{name}</b> ({num})\n"
    msg += "\nSource: Companies House"

    for chat_id in subs:
        send_telegram(chat_id, msg)
        time.sleep(0.1)

    # Mark as alerted
    for num, _ in unalerted[:5]:
        c.execute("UPDATE known_companies SET alerted=1 WHERE company_number=?", (num,))
    conn.commit()
    print(f"  Sent insolvency alerts to {len(subs)} subscribers")


# ─── Main ───

def main():
    print(f"\n{'='*60}")
    print(f"UK Company Watch — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: No TELEGRAM_BOT_TOKEN set. Bot will not send messages.")

    conn = init_db()

    # 1. Process Telegram messages
    print("\n[1/4] Processing Telegram messages...")
    process_updates(conn)

    # 2. Check insolvencies
    print("\n[2/4] Checking insolvencies...")
    new_ins = check_insolvencies(conn)

    # 3. Check new companies
    print("\n[3/4] Checking new companies...")
    new_com = check_new_companies(conn)

    # 4. Check watched company filings
    print("\n[4/4] Checking watched company filings...")
    new_filings = check_watched_filings(conn)

    # 5. Send alerts
    print("\n[5/5] Sending alerts...")
    send_insolvency_alerts(conn, new_ins)

    # Summary
    print(f"\n{'='*60}")
    print(f"Summary: {new_ins} insolvencies, {new_com} new companies, {len(new_filings)} new filings")
    print(f"{'='*60}\n")

    conn.close()


if __name__ == "__main__":
    main()
