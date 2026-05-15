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
        CREATE TABLE IF NOT EXISTS watchlists (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT DEFAULT '📋',
            company_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS watchlist_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watchlist_code TEXT NOT NULL,
            company_number TEXT NOT NULL,
            company_name TEXT,
            FOREIGN KEY(watchlist_code) REFERENCES watchlists(code),
            UNIQUE(watchlist_code, company_number)
        );
        CREATE TABLE IF NOT EXISTS watchlist_subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            watchlist_code TEXT NOT NULL,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(watchlist_code) REFERENCES watchlists(code),
            UNIQUE(chat_id, watchlist_code)
        );
        CREATE INDEX IF NOT EXISTS idx_filings_company ON known_filings(company_number);
        CREATE INDEX IF NOT EXISTS idx_watched_chat ON watched_companies(chat_id);
        CREATE INDEX IF NOT EXISTS idx_wl_subs_chat ON watchlist_subscribers(chat_id);
        CREATE INDEX IF NOT EXISTS idx_wl_companies_code ON watchlist_companies(watchlist_code);
    """)
    conn.commit()
    return conn


# ─── Watchlists ───

WATCHLISTS = {
    "fintech": {
        "name": "UK Fintech",
        "icon": "💳",
        "description": "15 major UK fintechs: Revolut, Starling, Wise, GoCardless, Funding Circle, Monese, Clearbank, Railsr, Soldo, Plum Fintech, Atom Bank, Tandem, Nutmeg, Cleo, Wombat",
        "companies": [
            ("08804411", "REVOLUT LTD"),
            ("09092149", "STARLING BANK LIMITED"),
            ("13211214", "WISE LIMITED"),
            ("07495895", "GOCARDLESS LTD"),
            ("06968588", "FUNDING CIRCLE LTD"),
            ("08720992", "MONESE LTD"),
            ("09736376", "CLEARBANK LIMITED"),
            ("14002844", "RAILSROCKET LTD"),
            ("14361848", "SOLDO LTD"),
            ("09952199", "PLUM FINTECH LTD"),
            ("08632552", "ATOM BANK PLC"),
            ("00955491", "TANDEM BANK LIMITED"),
            ("OC458635", "NUTMEG SAVING AND INVESTMENT LIMITED"),
            ("SL027367", "CLEO AI LTD"),
            ("SC709218", "WOMBAT INVESTING LTD"),
        ],
    },
    "crypto": {
        "name": "UK Crypto & Blockchain",
        "icon": "🪙",
        "description": "10 UK crypto & blockchain firms: Kraken, Bitstamp, Chainalysis, Copper, Fireblocks + more",
        "companies": [
            ("14701136", "KRAKEN UK LTD"),
            ("08157033", "BITSTAMP UK LTD"),
            ("11434241", "CHAINALYSIS LTD"),
            ("03772048", "COPPER TECHNOLOGIES LTD"),
            ("13650687", "FIREBLOCKS LTD"),
            ("13974557", "BITSTAMP UK LTD"),
            ("12254454", "KRAKEN UK LTD"),
            ("11125610", "COINBASE UK LTD"),
            ("10004019", "CRYPTOCOM LTD"),
            ("11537321", "GEMINI EUROPE LIMITED"),
        ],
    },
    "ai": {
        "name": "UK AI & Machine Learning",
        "icon": "🤖",
        "description": "10 leading UK AI companies: Graphcore, Improbable, Faculty AI, Onfido, Tractable, Stability AI, Hugging Face, Darktrace + more",
        "companies": [
            ("10185006", "GRAPHCORE LTD"),
            ("08561272", "IMPROBABLE WORLDS LTD"),
            ("16594137", "FACULTY AI LTD"),
            ("07479524", "ONFIDO LTD"),
            ("09315523", "TRACTABLE LTD"),
            ("12295325", "STABILITY AI LTD"),
            ("16465668", "HUGGING FACE LTD"),
            ("13264637", "DARKTRACE PLC"),
            ("15588410", "DEEPMIND TECHNOLOGIES LTD"),
            ("08713046", "WAYVE TECHNOLOGIES LTD"),
        ],
    },
    "property": {
        "name": "UK Property & PropTech",
        "icon": "🏠",
        "description": "15 UK property & proptech firms: Rightmove, Zoopla, OnTheMarket, Purplebricks, Foxtons, Savills, Knight Frank, JLL, LendInvest, Homelet, Goodlord, Flatfair, Molo + more",
        "companies": [
            ("06426485", "RIGHTMOVE PLC"),
            ("06074771", "ZOPLA PROPERTY LTD"),
            ("10887621", "ONTHEMARKET PLC"),
            ("15846533", "PURPLEBRICKS GROUP PLC"),
            ("01680058", "FOXTONS GROUP PLC"),
            ("02122174", "SAVILLS PLC"),
            ("OC305934", "KNIGHT FRANK LLP"),
            ("16760486", "JONES LANG LASALLE PLC"),
            ("16582814", "CUSHMAN & WAKEFIELD PLC"),
            ("08146929", "LENDINVEST PLC"),
            ("15034787", "HOMELET LTD"),
            ("17204403", "GOODLORD LTD"),
            ("10487576", "FLATFAIR LTD"),
            ("11726983", "MOLO FINANCE LTD"),
            ("08657841", "LANDLORD VISION LTD"),
        ],
    },
    "retail": {
        "name": "UK Retail & E-Commerce",
        "icon": "🛒",
        "description": "9 major UK retailers: ASOS, THG, Ocado, Deliveroo, Just Eat, HelloFresh UK, Wayfair UK, Amazon UK + more",
        "companies": [
            ("04006623", "ASOS PLC"),
            ("06539496", "THG PLC"),
            ("16235474", "OCADO GROUP PLC"),
            ("13227665", "DELIVEROO PLC"),
            ("06947854", "JUST EAT TAKEAWAY.COM PLC"),
            ("16626640", "HELLOFRESH UK LTD"),
            ("06776852", "WAYFAIR UK LTD"),
            ("03223028", "AMAZON UK SERVICES LTD"),
            ("15891239", "GOUSTO LTD"),
        ],
    },
    "insurtech": {
        "name": "UK InsurTech",
        "icon": "🛡️",
        "description": "8 UK insurtechs: Zego, Cuvva, Bought By Many, By Miles, Reclaim247, So-Sure, Laka + more",
        "companies": [
            ("14695368", "ZEGO LTD"),
            ("08907985", "CUVVA LTD"),
            ("12735852", "BOUGHT BY MANY LTD"),
            ("09498559", "BY MILES LTD"),
            ("11382136", "RECLAIM247 LTD"),
            ("09365669", "SO-SURE LTD"),
            ("10575209", "LAKA LTD"),
            ("08624700", "DEAD HAPPY LTD"),
        ],
    },
}


def init_watchlists(conn):
    """Initialize pre-built watchlists in the database."""
    c = conn.cursor()
    for code, wl in WATCHLISTS.items():
        c.execute(
            "INSERT OR IGNORE INTO watchlists (code, name, description, icon, company_count) VALUES (?, ?, ?, ?, ?)",
            (code, wl["name"], wl["description"], wl["icon"], len(wl["companies"])),
        )
        for num, name in wl["companies"]:
            c.execute(
                "INSERT OR IGNORE INTO watchlist_companies (watchlist_code, company_number, company_name) VALUES (?, ?, ?)",
                (code, num, name),
            )
    conn.commit()


def get_watchlist_count(chat_id, conn):
    """Get total watchlist subscriptions for a user."""
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM watchlist_subscribers WHERE chat_id = ?", (str(chat_id),))
    return c.fetchone()[0]


def get_watched_count(chat_id, conn):
    """Get total individual company watches for a user."""
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM watched_companies WHERE chat_id = ?", (str(chat_id),))
    return c.fetchone()[0]


def get_total_watch_count(chat_id, conn):
    """Get total watched items (individual + watchlists)."""
    return get_watched_count(chat_id, conn) + get_watchlist_count(chat_id, conn)


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
    sub = c.execute("SELECT max_watched FROM subscribers WHERE chat_id = ?", (str(chat_id),)).fetchone()
    max_watched = sub[0] if sub else 1
    total = get_total_watch_count(chat_id, conn)

    if total >= max_watched:
        send_telegram(chat_id, f"You're at your watch limit ({total}/{max_watched}). Use /pricing to upgrade, or /leave [code] to leave a watchlist first.")
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

    # Also get watchlist subscriptions
    c.execute("""
        SELECT w.code, w.name, w.icon, w.company_count 
        FROM watchlist_subscribers ws 
        JOIN watchlists w ON ws.watchlist_code = w.code 
        WHERE ws.chat_id = ?
    """, (str(chat_id),))
    wl_subs = c.fetchall()

    if not watched and not wl_subs:
        send_telegram(chat_id, "You're not watching anything. Use /watch [number] for individual companies, or /watchlists to browse group watchlists.")
        return

    lines = ["📋 Your watched items:\n"]
    for num, name in watched:
        lines.append(f"• <b>{name}</b> ({num})")
    for code, name, icon, count in wl_subs:
        lines.append(f"{icon} <b>{name}</b> ({count} companies) — /leave {code}")
    send_telegram(chat_id, "\n".join(lines))


def handle_watchlists(chat_id, conn):
    """Show available watchlists."""
    c = conn.cursor()
    c.execute("SELECT code, name, description, icon, company_count FROM watchlists ORDER BY name")
    watchlists = c.fetchall()

    if not watchlists:
        send_telegram(chat_id, "No watchlists available yet.")
        return

    lines = ["📋 <b>Group Watchlists</b>\n"]
    lines.append("Join a watchlist to monitor multiple companies at once. Each watchlist counts as 1 watch.\n")
    for code, name, desc, icon, count in watchlists:
        lines.append(f"{icon} <b>{name}</b> — {count} companies")
        lines.append(f"   {desc}")
        lines.append(f"   /join {code}\n")
    lines.append("Use /watching to see what you've joined.")
    send_telegram(chat_id, "\n".join(lines))


def handle_join(chat_id, code, conn):
    """Join a watchlist."""
    c = conn.cursor()
    code = code.lower().strip()

    # Check if watchlist exists
    c.execute("SELECT name, icon, company_count FROM watchlists WHERE code = ?", (code,))
    wl = c.fetchone()
    if not wl:
        send_telegram(chat_id, f"Watchlist '{code}' not found. Use /watchlists to see available groups.")
        return

    name, icon, count = wl

    # Check if already joined
    c.execute("SELECT 1 FROM watchlist_subscribers WHERE chat_id = ? AND watchlist_code = ?", (str(chat_id), code))
    if c.fetchone():
        send_telegram(chat_id, f"You've already joined {icon} {name}. Use /watching to see all your subscriptions.")
        return

    # Check watch limit
    sub = c.execute("SELECT max_watched FROM subscribers WHERE chat_id = ?", (str(chat_id),)).fetchone()
    max_watched = sub[0] if sub else 1
    total = get_total_watch_count(chat_id, conn)
    if total >= max_watched:
        send_telegram(chat_id, f"You're at your watch limit ({total}/{max_watched}). Use /pricing to upgrade, or /leave [code] to leave a watchlist first.")
        return

    # Join
    c.execute("INSERT INTO watchlist_subscribers (chat_id, watchlist_code) VALUES (?, ?)", (str(chat_id), code))
    conn.commit()
    send_telegram(chat_id, f"✅ Joined {icon} <b>{name}</b>! You're now monitoring {count} companies. You'll receive alerts for any new filings.")


def handle_leave(chat_id, code, conn):
    """Leave a watchlist."""
    c = conn.cursor()
    code = code.lower().strip()

    c.execute("SELECT name, icon FROM watchlists WHERE code = ?", (code,))
    wl = c.fetchone()
    if not wl:
        send_telegram(chat_id, f"Watchlist '{code}' not found.")
        return

    name, icon = wl
    c.execute("DELETE FROM watchlist_subscribers WHERE chat_id = ? AND watchlist_code = ?", (str(chat_id), code))
    if c.rowcount > 0:
        conn.commit()
        send_telegram(chat_id, f"Left {icon} {name}. You'll no longer receive alerts for this group.")
    else:
        send_telegram(chat_id, f"You're not subscribed to {icon} {name}.")


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
            send_telegram(chat_id, "Commands:\n/search [name]\n/company [number]\n/watch [number]\n/watching\n/watchlists\n/join [code]\n/leave [code]\n/digest\n/pricing")
        elif text.startswith("/search "):
            handle_search(chat_id, text[8:].strip())
        elif text.startswith("/company "):
            handle_company(chat_id, text[9:].strip())
        elif text.startswith("/watch "):
            handle_watch(chat_id, text[7:].strip(), conn)
        elif text == "/watching":
            handle_watching(chat_id, conn)
        elif text == "/watchlists":
            handle_watchlists(chat_id, conn)
        elif text.startswith("/join "):
            handle_join(chat_id, text[6:].strip(), conn)
        elif text.startswith("/leave "):
            handle_leave(chat_id, text[7:].strip(), conn)
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
    """Check filing history for individually watched companies AND watchlist companies."""
    print("Checking watched companies...")
    c = conn.cursor()

    # Get individually watched companies
    c.execute("SELECT DISTINCT company_number FROM watched_companies")
    individual = set(row[0] for row in c.fetchall())

    # Get watchlist companies (from all watchlists that have subscribers)
    c.execute("""
        SELECT DISTINCT wc.company_number, wc.company_name, ws.chat_id
        FROM watchlist_companies wc
        JOIN watchlist_subscribers ws ON wc.watchlist_code = ws.watchlist_code
    """)
    watchlist_filings = {}  # chat_id -> list of new filings
    watchlist_companies = set()
    for num, name, chat_id in c.fetchall():
        watchlist_companies.add(num)
        if chat_id not in watchlist_filings:
            watchlist_filings[chat_id] = []

    all_companies = individual | watchlist_companies
    if not all_companies:
        print("  No watched companies or active watchlists.")
        return [], {}

    new_individual = []
    new_watchlist = {}  # chat_id -> list of filings

    for num in all_companies:
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
                filing = {"company_number": num, "date": fdate, "type": ftype, "description": desc}

                if num in individual:
                    new_individual.append(filing)

                # Add to watchlist subscribers
                if num in watchlist_companies:
                    c.execute("""
                        SELECT ws.chat_id FROM watchlist_subscribers ws
                        JOIN watchlist_companies wc ON ws.watchlist_code = wc.watchlist_code
                        WHERE wc.company_number = ?
                    """, (num,))
                    for row in c.fetchall():
                        cid = row[0]
                        if cid not in new_watchlist:
                            new_watchlist[cid] = []
                        new_watchlist[cid].append(filing)

                print(f"  New filing: {num} — {fdate} {ftype}")
            except sqlite3.IntegrityError:
                pass
        time.sleep(0.5)  # Rate limit

    return new_individual, new_watchlist


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
    init_watchlists(conn)

    # 1. Process Telegram messages
    print("\n[1/5] Processing Telegram messages...")
    process_updates(conn)

    # 2. Check insolvencies
    print("\n[2/5] Checking insolvencies...")
    new_ins = check_insolvencies(conn)

    # 3. Check new companies
    print("\n[3/5] Checking new companies...")
    new_com = check_new_companies(conn)

    # 4. Check watched company filings (individual + watchlist)
    print("\n[4/5] Checking watched company filings...")
    new_individual, new_watchlist = check_watched_filings(conn)

    # 5. Send alerts
    print("\n[5/5] Sending alerts...")
    send_insolvency_alerts(conn, new_ins)

    # Send watchlist filing alerts
    for chat_id, filings in new_watchlist.items():
        if filings:
            lines = ["📋 <b>Watchlist Filing Alerts</b>\n"]
            for f in filings[:5]:
                lines.append(f"• {f['company_number']} — {f['date']} {f['type']}")
                lines.append(f"  {f['description'][:80]}\n")
            send_telegram(chat_id, "\n".join(lines))
            time.sleep(0.1)

    # Send individual filing alerts (only for companies each user specifically watches)
    c = conn.cursor()
    c.execute("SELECT DISTINCT chat_id FROM watched_companies")
    for row in c.fetchall():
        cid = str(row[0])
        # Get this user's watched company numbers
        c2 = conn.cursor()
        c2.execute("SELECT company_number FROM watched_companies WHERE chat_id = ?", (cid,))
        user_nums = set(r[0] for r in c2.fetchall())
        user_filings = [f for f in new_individual if f["company_number"] in user_nums]
        if user_filings:
            lines = ["📋 <b>New Filing Alerts</b>\n"]
            for f in user_filings[:5]:
                lines.append(f"• {f['company_number']} — {f['date']} {f['type']}")
                lines.append(f"  {f['description'][:80]}\n")
            send_telegram(cid, "\n".join(lines))
            time.sleep(0.1)

    # Summary
    print(f"\n{'='*60}")
    print(f"Summary: {new_ins} insolvencies, {new_com} new companies, {len(new_individual)} individual filings, {len(new_watchlist)} watchlist alerts")
    print(f"{'='*60}\n")

    conn.close()


if __name__ == "__main__":
    main()
