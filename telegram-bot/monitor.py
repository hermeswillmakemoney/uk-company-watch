#!/usr/bin/env python3
"""
UK Company Watch — Companies House Monitor
Fetches latest filings and sends Telegram alerts.

Run via cron: every 60 minutes
"""

import urllib.request
import urllib.parse
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ───
CH_API_BASE = "https://api.companieshouse.gov.uk"
# Companies House doesn't require API key for basic endpoints
# But rate limits are stricter without one (60 req/5min vs 600/5min)

TELEGRAM_BOT_TOKEN = os.environ.get("UCW_TELEGRAM_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

DB_PATH = Path(__file__).parent.parent / "data" / "uk_company_watch.db"
DATA_DIR = DB_PATH.parent
DATA_DIR.mkdir(exist_ok=True)

# Default chat IDs to notify (add your own chat id here)
DEFAULT_CHAT_IDS = os.environ.get("UCW_CHAT_IDS", "").split(",") if os.environ.get("UCW_CHAT_IDS") else []


def init_db():
    """Initialize SQLite database."""
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
            last_checked TEXT,
            UNIQUE(chat_id, company_number),
            FOREIGN KEY(chat_id) REFERENCES subscribers(chat_id)
        );
        
        CREATE TABLE IF NOT EXISTS known_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_number TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            filing_type TEXT,
            description TEXT,
            alerted INTEGER DEFAULT 0,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_number, filing_date, filing_type)
        );
        
        CREATE TABLE IF NOT EXISTS new_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_number TEXT NOT NULL UNIQUE,
            company_name TEXT,
            date_of_creation TEXT,
            status TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            alerted INTEGER DEFAULT 0
        );
        
        CREATE INDEX IF NOT EXISTS idx_filings_company ON known_filings(company_number);
        CREATE INDEX IF NOT EXISTS idx_filings_alerted ON known_filings(alerted);
        CREATE INDEX IF NOT EXISTS idx_new_companies_alerted ON new_companies(alerted);
    """)
    conn.commit()
    return conn


def ch_fetch(path):
    """Fetch from Companies House API."""
    try:
        req = urllib.request.Request(
            f"{CH_API_BASE}{path}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"CH API error: {e}")
        return None


def send_telegram(chat_id, text):
    """Send message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        print(f"[Would send to {chat_id}]: {text[:200]}")
        return False

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
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


def check_new_insolvencies(conn):
    """Check for new insolvency filings."""
    print("Checking insolvencies...")
    
    data = ch_fetch("/search/companies?q=&company_status=insolvency&items_per_page=30")
    if not data or "items" not in data:
        return 0

    c = conn.cursor()
    new_count = 0

    for item in data.items:
        num = item["company_number"]
        name = item.get("title", "Unknown")

        try:
            c.execute("INSERT INTO new_companies (company_number, company_name, status, date_of_creation) VALUES (?, ?, ?, ?)",
                      (num, name, "insolvency", item.get("date_of_creation", "")))
            conn.commit()
            new_count += 1
            print(f"  New insolvency: {name} ({num})")
        except sqlite3.IntegrityError:
            pass  # Already seen

    return new_count


def check_new_companies(conn):
    """Check for newly incorporated companies in the last 24 hours."""
    print("Checking new companies...")

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    data = ch_fetch(f"/advanced-search/companies?incorporated_from={yesterday}&items_per_page=50")
    if not data or "items" not in data:
        return 0

    c = conn.cursor()
    new_count = 0

    for item in data.items:
        num = item["company_number"]
        name = item.get("title", "Unknown")

        try:
            c.execute("INSERT INTO new_companies (company_number, company_name, status, date_of_creation) VALUES (?, ?, ?, ?)",
                      (num, name, item.get("company_status", "active"), item.get("date_of_creation", "")))
            conn.commit()
            new_count += 1
            print(f"  New company: {name} ({num})")
        except sqlite3.IntegrityError:
            pass

    return new_count


def check_filings_for_watched(conn):
    """Check filing history for all watched companies."""
    print("Checking watched companies...")

    c = conn.cursor()
    c.execute("SELECT DISTINCT company_number, company_name FROM watched_companies")
    watched = c.fetchall()

    if not watched:
        print("  No watched companies.")
        return 0

    new_filings = []

    for num, name in watched:
        data = ch_fetch(f"/company/{num}/filing-history?items_per_page=5")
        if not data or "items" not in data:
            continue

        for f in data.items:
            fdate = f.get("date", "")
            ftype = f.get("type", "")
            desc = f.get("description", "")

            try:
                c.execute("INSERT INTO known_filings (company_number, filing_date, filing_type, description) VALUES (?, ?, ?, ?)",
                          (num, fdate, ftype, desc))
                conn.commit()
                new_filings.append({
                    "company_number": num,
                    "company_name": name or num,
                    "date": fdate,
                    "type": ftype,
                    "description": desc,
                })
                print(f"  New filing: {name or num} — {fdate} {ftype} {desc[:60]}")
            except sqlite3.IntegrityError:
                pass  # Already seen

    return new_filings


def send_alerts(conn, new_filings, new_insolvencies, new_companies_count):
    """Send alerts to all subscribers."""
    c = conn.cursor()

    # Get all subscribers
    c.execute("SELECT chat_id, plan, max_alerts_per_day FROM subscribers")
    subs = c.fetchall()

    if not subs:
        print("No subscribers to alert.")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    for chat_id, plan, max_alerts in subs:
        if not chat_id:
            continue

        # Reset daily counter
        c.execute("UPDATE subscribers SET alerts_today = 0, last_alert_date = ? WHERE chat_id = ? AND last_alert_date != ?",
                  (today, chat_id, today))
        conn.commit()

        # Check daily limit
        c.execute("SELECT alerts_today FROM subscribers WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        alerts_today = row[0] if row else 0

        if alerts_today >= max_alerts:
            print(f"  {chat_id} hit daily limit ({alerts_today}/{max_alerts})")
            continue

        messages_sent = 0

        # Send filing alerts for their watched companies
        for filing in new_filings:
            if messages_sent + alerts_today >= max_alerts:
                break

            # Check if this user watches this company
            c.execute("SELECT 1 FROM watched_companies WHERE chat_id = ? AND company_number = ?",
                      (chat_id, filing["company_number"]))
            if not c.fetchone():
                continue

            msg = (
                f"📋 <b>New Filing Alert</b>\n\n"
                f"🏢 {filing['company_name']}\n"
                f"📅 {filing['date']}\n"
                f"📄 {filing['description']}\n\n"
                f"View: https://find-and-update.company-information.service.gov.uk/company/{filing['company_number']}"
            )

            send_telegram(chat_id, msg)
            messages_sent += 1

        # Send insolvency alerts (everyone gets these)
        if new_insolvencies > 0 and messages_sent + alerts_today < max_alerts:
            c.execute("SELECT company_number, company_name FROM new_companies WHERE alerted = 0 LIMIT 5")
            new_ins = c.fetchall()

            if new_ins:
                msg = "⚠️ <b>Insolvency Alerts</b>\n\n"
                for num, name in new_ins[:5]:
                    msg += f"• <b>{name}</b> ({num})\n"
                    c.execute("UPDATE new_companies SET alerted = 1 WHERE company_number = ?", (num,))

                msg += "\nSource: Companies House"
                send_telegram(chat_id, msg)
                messages_sent += 1

        if messages_sent > 0:
            c.execute("UPDATE subscribers SET alerts_today = alerts_today + ? WHERE chat_id = ?",
                      (messages_sent, chat_id))
            conn.commit()
            print(f"  Sent {messages_sent} alerts to {chat_id}")


def generate_daily_digest(conn):
    """Generate a daily summary message."""
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM new_companies WHERE date(first_seen) = date('now')")
    new_today = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM new_companies WHERE status = 'insolvency' AND date(first_seen) = date('now')")
    insolvencies_today = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM known_filings WHERE date(first_seen) = date('now')")
    filings_today = c.fetchone()[0]

    msg = (
        f"📊 <b>UK Company Watch — Daily Digest</b>\n"
        f"📅 {datetime.now().strftime('%d %B %Y')}\n\n"
        f"🏢 New companies: <b>{new_today}</b>\n"
        f"⚠️ Insolvencies: <b>{insolvencies_today}</b>\n"
        f"📋 Total filings: <b>{filings_today}</b>\n\n"
        f"Reply /help for commands."
    )
    
    return msg


def process_telegram_updates(conn):
    """Process incoming Telegram messages."""
    if not TELEGRAM_BOT_TOKEN:
        return

    # Simple long-poll for updates
    try:
        data = json.loads(urllib.request.urlopen(
            f"{TELEGRAM_API}/getUpdates?timeout=0&limit=10",
            timeout=5
        ).read().decode())
    except Exception:
        return

    if not data.get("ok"):
        return

    c = conn.cursor()
    last_update_id = 0

    for update in data.get("result", []):
        update_id = update["update_id"]
        last_update_id = max(last_update_id, update_id)

        if "message" not in update:
            continue

        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()

        # Auto-register
        try:
            c.execute("INSERT OR IGNORE INTO subscribers (chat_id, plan) VALUES (?, 'free')", (chat_id,))
            conn.commit()
        except Exception:
            pass

        if text == "/start":
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

        elif text == "/help":
            send_telegram(chat_id,
                "Commands:\n"
                "/search [name] — Search companies\n"
                "/company [number] — Get company details\n"
                "/watch [number] — Watch a company\n"
                "/watching — Your watched companies\n"
                "/digest — Today's summary\n"
                "/pricing — Upgrade plans"
            )

        elif text.startswith("/search "):
            q = text[8:].strip()
            if len(q) < 2:
                send_telegram(chat_id, "Query must be at least 2 chars.")
            else:
                data = ch_fetch(f"/search/companies?q={urllib.parse.quote(q)}&items_per_page=5")
                if data and data.get("items"):
                    lines = [f"Found {data.get('total_results', 0)} companies for \"{q}\":\n"]
                    for item in data["items"][:5]:
                        lines.append(f"• <b>{item['title']}</b> ({item['company_number']}) — {item.get('company_status', '?')}")
                        lines.append(f"  /company {item['company_number']} | /watch {item['company_number']}\n")
                    send_telegram(chat_id, "\n".join(lines))
                else:
                    send_telegram(chat_id, f"No results for \"{q}\".")

        elif text.startswith("/company "):
            num = text[9:].strip()
            data = ch_fetch(f"/company/{num}")
            if data:
                c_data = data
                name = c_data.get("company_name", "Unknown")
                status = c_data.get("company_status", "?")
                addr = c_data.get("registered_office_address", {})
                address_parts = [addr.get(k, "") for k in ["address_line_1", "address_line_2", "locality", "postal_code"] if addr.get(k)]
                address = ", ".join(address_parts) if address_parts else "N/A"
                sic = ", ".join(c_data.get("sic_codes", [])) or "N/A"
                
                msg = f"📊 <b>{name}</b>\n"
                msg += f"Number: {num}\n"
                msg += f"Status: {status}\n"
                msg += f"Type: {c_data.get('type', '?')}\n"
                msg += f"Founded: {c_data.get('date_of_creation', 'N/A')}\n"
                msg += f"SIC: {sic}\n"
                msg += f"Address: {address}\n"
                
                accts = c_data.get("accounts", {})
                if accts.get("overdue"):
                    msg += "⚠️ Accounts OVERDUE\n"
                cs = c_data.get("confirmation_statement", {})
                if cs.get("overdue"):
                    msg += "⚠️ Confirmation statement OVERDUE\n"
                
                msg += f"\nView: https://find-and-update.company-information.service.gov.uk/company/{num}"
                send_telegram(chat_id, msg)
            else:
                send_telegram(chat_id, f"Company {num} not found.")

        elif text.startswith("/watch "):
            num = text[7:].strip()
            # Check limit
            c.execute("SELECT max_watched, (SELECT COUNT(*) FROM watched_companies WHERE chat_id = ?) FROM subscribers WHERE chat_id = ?",
                      (chat_id, chat_id))
            row = c.fetchone()
            if row:
                max_watched, current = row
                if current >= max_watched:
                    send_telegram(chat_id, f"You're watching {current}/{max_watched} companies. Upgrade to /pricing for more.")
                    continue

            # Get company name
            data = ch_fetch(f"/company/{num}")
            name = data.get("company_name", num) if data else num

            try:
                c.execute("INSERT INTO watched_companies (chat_id, company_number, company_name) VALUES (?, ?, ?)",
                          (chat_id, num, name))
                conn.commit()
                send_telegram(chat_id, f"✅ Now watching <b>{name}</b> ({num}). You'll get alerts for new filings.")
            except sqlite3.IntegrityError:
                send_telegram(chat_id, f"You're already watching {name} ({num}).")

        elif text == "/watching":
            c.execute("SELECT company_number, company_name FROM watched_companies WHERE chat_id = ?", (chat_id,))
            watched = c.fetchall()
            if watched:
                lines = ["📋 Your watched companies:\n"]
                for num, name in watched:
                    lines.append(f"• <b>{name}</b> ({num})")
                send_telegram(chat_id, "\n".join(lines))
            else:
                send_telegram(chat_id, "You're not watching any companies. Use /watch [number] to start.")

        elif text == "/digest":
            digest = generate_daily_digest(conn)
            send_telegram(chat_id, digest)

        elif text == "/pricing":
            send_telegram(chat_id,
                "📊 <b>UK Company Watch Pricing</b>\n\n"
                "🆓 <b>Free</b> — £0/month\n"
                "  • 3 alerts/day\n"
                "  • 1 watched company\n\n"
                "⭐ <b>Pro</b> — £4.99/month\n"
                "  • 50 alerts/day\n"
                "  • 10 watched companies\n"
                "  • Email + Telegram alerts\n\n"
                "🏢 <b>Business</b> — £19.99/month\n"
                "  • Unlimited alerts\n"
                "  • Unlimited companies\n"
                "  • Webhook + API access\n\n"
                "Upgrade: Send £4.99 or £19.99 via PayPal to\n"
                "hermeswillmakesmoney@gmail.com\n"
                "with note: UCW Pro [your chat id]\n"
                f"Your chat id: {chat_id}"
            )

    # Acknowledge processed updates
    if last_update_id > 0:
        try:
            urllib.request.urlopen(
                f"{TELEGRAM_API}/getUpdates?offset={last_update_id + 1}",
                timeout=5
            )
        except Exception:
            pass


def main():
    print(f"UK Company Watch — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    conn = init_db()

    # 1. Process Telegram messages
    process_telegram_updates(conn)

    # 2. Check for new insolvencies
    new_ins = check_new_insolvencies(conn)

    # 3. Check for new companies
    new_com = check_new_companies(conn)

    # 4. Check filings for watched companies
    new_filings = check_filings_for_watched(conn)

    # 5. Send alerts
    send_alerts(conn, new_filings, new_ins, new_com)

    # 6. Print summary
    print(f"\nSummary:")
    print(f"  New insolvencies: {new_ins}")
    print(f"  New companies: {new_com}")
    print(f"  New filings for watched: {len(new_filings)}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
