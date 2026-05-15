#!/usr/bin/env python3
"""
UK Company Watch — Single cron job flow.
Runs hourly. For each new filing, spawns a one-shot kanban cron job.
Each kanban writes its summary to the DB. After all done, sends per-user alerts.

Usage: python3 run.py
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

# ─── Stripe config ───
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
# Price IDs from Stripe dashboard
STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "price_1TXSqILyJWmpaKc9lnjQ2KoI")
STRIPE_PRICE_BUSINESS = os.environ.get("STRIPE_PRICE_BUSINESS", "price_1TXSr6LyJWmpaKc9xGV1iVtW")
# Base URL for webhook callbacks
UCW_BASE_URL = os.environ.get("UCW_BASE_URL", "https://uk-company-watch.vercel.app")

PLAN_LIMITS = {
    "free": {"max_watched": 1, "max_alerts_per_day": 3},
    "pro": {"max_watched": 10, "max_alerts_per_day": 50},
    "business": {"max_watched": 999, "max_alerts_per_day": 9999},
}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            subscription_status TEXT,
            current_period_end TEXT,
            max_watched INTEGER DEFAULT 1,
            max_alerts_per_day INTEGER DEFAULT 3
        );
        CREATE TABLE IF NOT EXISTS watched_companies (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, company_number TEXT NOT NULL, company_name TEXT, added_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(chat_id, company_number));
        CREATE TABLE IF NOT EXISTS known_filings (id INTEGER PRIMARY KEY AUTOINCREMENT, company_number TEXT NOT NULL, filing_date TEXT NOT NULL, filing_type TEXT, description TEXT, first_seen TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(company_number, filing_date, filing_type));
        CREATE TABLE IF NOT EXISTS known_companies (company_number TEXT PRIMARY KEY, company_name TEXT, status TEXT, date_of_creation TEXT, first_seen TEXT DEFAULT CURRENT_TIMESTAMP, alerted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS watchlists (code TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, icon TEXT DEFAULT '📋', company_count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS watchlist_companies (id INTEGER PRIMARY KEY AUTOINCREMENT, watchlist_code TEXT NOT NULL, company_number TEXT NOT NULL, company_name TEXT, FOREIGN KEY(watchlist_code) REFERENCES watchlists(code), UNIQUE(watchlist_code, company_number));
        CREATE TABLE IF NOT EXISTS watchlist_subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, watchlist_code TEXT NOT NULL, joined_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(watchlist_code) REFERENCES watchlists(code), UNIQUE(chat_id, watchlist_code));
        CREATE TABLE IF NOT EXISTS filing_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, company_number TEXT NOT NULL, company_name TEXT, filing_date TEXT NOT NULL, filing_type TEXT, description TEXT, summary TEXT NOT NULL, summary_date TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(company_number, filing_date, filing_type));
        CREATE INDEX IF NOT EXISTS idx_filings_company ON known_filings(company_number);
        CREATE INDEX IF NOT EXISTS idx_watched_chat ON watched_companies(chat_id);
        CREATE INDEX IF NOT EXISTS idx_summaries_date ON filing_summaries(summary_date);
    """)
    conn.commit()

    # Migrate: add Stripe columns if they don't exist
    try:
        c.execute("SELECT stripe_subscription_id FROM subscribers LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE subscribers ADD COLUMN stripe_subscription_id TEXT")
        c.execute("ALTER TABLE subscribers ADD COLUMN stripe_customer_id TEXT")
        c.execute("ALTER TABLE subscribers ADD COLUMN subscription_status TEXT")
        c.execute("ALTER TABLE subscribers ADD COLUMN current_period_end TEXT")
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
        return False
    data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(f"{TELEGRAM_API}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ─── Filing type intelligence (no LLM needed) ───

FILING_TYPE_MAP = {
    "AA01": ("Annual return", "routine", "Company confirmed its details are up to date."),
    "AR01": ("Annual return", "routine", "Company filed its annual return."),
    "CH01": ("Director appointed", "notable", "A new director was appointed to the board."),
    "CH02": ("Director resigned", "concerning", "A director resigned from the board."),
    "CH03": ("Director details changed", "routine", "A director's details were updated."),
    "CS01": ("Confirmation statement", "routine", "Company filed its confirmation statement."),
    "AD01": ("Address changed", "notable", "Company changed its registered address."),
    "SH01": ("Shares allotted", "notable", "Company issued new shares — could indicate fundraising."),
    "SH06": ("Shares allotted", "notable", "Company allotted new shares."),
    "MG01": ("Mortgage/charge", "concerning", "Company registered a mortgage or charge — secured debt taken on."),
    "LIQ01": ("Liquidation", "critical", "Company entered liquidation."),
    "WUO1": ("Winding up", "critical", "Company is being wound up."),
    "DS01": ("Dissolution", "concerning", "Company applied for dissolution."),
    "GAZ1": ("Gazette notice", "notable", "Gazette notice published — often relates to strike-off or insolvency."),
    "GAZ2": ("Gazette notice", "notable", "Gazette notice published."),
    "TM01": ("Termination", "notable", "A director or officer's appointment was terminated."),
    "TM02": ("Termination", "notable", "A director or officer's appointment was terminated."),
    "AP01": ("Director appointed", "notable", "A new director was appointed."),
    "AP04": ("Director appointed", "notable", "A new director was appointed."),
    "NEWINC": ("Incorporation", "routine", "New company incorporated."),
    "CERTNM": ("Name change", "notable", "Company changed its name."),
    "CERT10": ("Incorporation", "routine", "Certificate of incorporation issued."),
    "MA": ("Merger/acquisition", "notable", "Merger or acquisition activity detected."),
    "RESOLUTIONS": ("Special resolution", "notable", "Company passed a special resolution."),
    "MR01": ("Mortgage registered", "concerning", "A mortgage was registered against the company."),
    "MR04": ("Mortgage satisfied", "routine", "A mortgage was satisfied/released."),
    "PSC01": ("PSC update", "notable", "Person with Significant Control information updated."),
    "PSC02": ("PSC update", "notable", "PSC information changed."),
    "PSC04": ("PSC update", "notable", "PSC information changed."),
    "PSC05": ("PSC update", "notable", "PSC information changed."),
    "PSC07": ("PSC update", "notable", "PSC information changed."),
    "PSC08": ("PSC update", "notable", "PSC information changed."),
    "PSC09": ("PSC update", "notable", "PSC information changed."),
    "RP01": ("Share buyback", "notable", "Company purchased its own shares."),
    "AAMD": ("Accounts amended", "routine", "Company accounts were amended."),
    "AM10": ("Accounts amended", "routine", "Amended accounts filed."),
    "AM19": ("Accounts amended", "routine", "Amended accounts filed."),
    "AM23": ("Accounts amended", "routine", "Amended accounts filed."),
    "RR02": ("Re-registration", "notable", "Company re-registered (e.g. from private to public)."),
    "MAR": ("Re-registration", "notable", "Company re-registered its memorandum and articles."),
    "LLAA01": ("LLP annual return", "routine", "Limited liability partnership filed annual return."),
    "LLIN01": ("LLP incorporated", "routine", "New LLP incorporated."),
    "LLTM01": ("LLP termination", "concerning", "LLP termination."),
    "LP6": ("LP filing", "routine", "Limited partnership filing."),
    "SLPCS01": ("Scottish LP", "routine", "Scottish limited partnership filing."),
}

def generate_summary(company_name, company_number, filing_date, filing_type, description):
    """Generate a human-readable summary using filing type intelligence. No LLM needed."""
    if filing_type in FILING_TYPE_MAP:
        label, severity, explanation = FILING_TYPE_MAP[filing_type]
    else:
        label = filing_type
        severity = "routine"
        explanation = f"Filing type: {filing_type}."

    # Build severity indicator
    flag = ""
    if severity == "critical":
        flag = " 🔴"
    elif severity == "concerning":
        flag = " ⚠️"
    elif severity == "notable":
        flag = " 📌"

    # Include description if meaningful
    desc = ""
    if description and len(description) > 5 and description != "No description available":
        desc = f" ({description[:80]})"

    return f"{company_name} ({company_number}): {label} on {filing_date}.{flag} {explanation}{desc}"


# ─── Watchlists ───

WATCHLISTS = {
    "fintech": {"name": "UK Fintech", "icon": "💳", "companies": [
        ("08804411", "REVOLUT LTD"), ("09092149", "STARLING BANK LIMITED"),
        ("13211214", "WISE LIMITED"), ("07495895", "GOCARDLESS LTD"),
        ("06968588", "FUNDING CIRCLE LTD"), ("08720992", "MONESE LTD"),
        ("09736376", "CLEARBANK LIMITED"), ("14002844", "RAILSROCKET LTD"),
        ("14361848", "SOLDO LTD"), ("09952199", "PLUM FINTECH LTD"),
        ("08632552", "ATOM BANK PLC"), ("00955491", "TANDEM BANK LIMITED"),
        ("OC458635", "NUTMEG SAVING AND INVESTMENT LIMITED"),
        ("SL027367", "CLEO AI LTD"), ("SC709218", "WOMBAT INVESTING LTD"),
    ]},
    "crypto": {"name": "UK Crypto", "icon": "🪙", "companies": [
        ("14701136", "KRAKEN UK LTD"), ("08157033", "BITSTAMP UK LTD"),
        ("11434241", "CHAINALYSIS LTD"), ("03772048", "COPPER TECHNOLOGIES LTD"),
        ("13650687", "FIREBLOCKS LTD"), ("11125610", "COINBASE UK LTD"),
        ("10004019", "CRYPTOCOM LTD"), ("11537321", "GEMINI EUROPE LIMITED"),
    ]},
    "ai": {"name": "UK AI & ML", "icon": "🤖", "companies": [
        ("10185006", "GRAPHCORE LTD"), ("08561272", "IMPROBABLE WORLDS LTD"),
        ("16594137", "FACULTY AI LTD"), ("07479524", "ONFIDO LTD"),
        ("09315523", "TRACTABLE LTD"), ("12295325", "STABILITY AI LTD"),
        ("16465668", "HUGGING FACE LTD"), ("13264637", "DARKTRACE PLC"),
        ("15588410", "DEEPMIND TECHNOLOGIES LTD"), ("08713046", "WAYVE TECHNOLOGIES LTD"),
    ]},
    "property": {"name": "UK Property", "icon": "🏠", "companies": [
        ("06426485", "RIGHTMOVE PLC"), ("06074771", "ZOPLA PROPERTY LTD"),
        ("10887621", "ONTHEMARKET PLC"), ("15846533", "PURPLEBRICKS GROUP PLC"),
        ("01680058", "FOXTONS GROUP PLC"), ("02122174", "SAVILLS PLC"),
        ("OC305934", "KNIGHT FRANK LLP"), ("16760486", "JONES LANG LASALLE PLC"),
        ("08146929", "LENDINVEST PLC"), ("15034787", "HOMELET LTD"),
        ("17204403", "GOODLORD LTD"), ("10487576", "FLATFAIR LTD"),
        ("11726983", "MOLO FINANCE LTD"), ("08657841", "LANDLORD VISION LTD"),
        ("16582814", "CUSHMAN & WAKEFIELD PLC"),
    ]},
    "retail": {"name": "UK Retail", "icon": "🛒", "companies": [
        ("04006623", "ASOS PLC"), ("06539496", "THG PLC"),
        ("16235474", "OCADO GROUP PLC"), ("13227665", "DELIVEROO PLC"),
        ("06947854", "JUST EAT TAKEAWAY.COM PLC"), ("16626640", "HELLOFRESH UK LTD"),
        ("06776852", "WAYFAIR UK LTD"), ("03223028", "AMAZON UK SERVICES LTD"),
        ("15891239", "GOUSTO LTD"),
    ]},
    "insurtech": {"name": "UK InsurTech", "icon": "🛡️", "companies": [
        ("14695368", "ZEGO LTD"), ("08907985", "CUVVA LTD"),
        ("12735852", "BOUGHT BY MANY LTD"), ("09498559", "BY MILES LTD"),
        ("11382136", "RECLAIM247 LTD"), ("09365669", "SO-SURE LTD"),
        ("10575209", "LAKA LTD"), ("08624700", "DEAD HAPPY LTD"),
    ]},
}

def init_watchlists(conn):
    c = conn.cursor()
    for code, wl in WATCHLISTS.items():
        c.execute("INSERT OR IGNORE INTO watchlists (code, name, icon, company_count) VALUES (?, ?, ?, ?)",
                  (code, wl["name"], wl["icon"], len(wl["companies"])))
        for num, name in wl["companies"]:
            c.execute("INSERT OR IGNORE INTO watchlist_companies (watchlist_code, company_number, company_name) VALUES (?, ?, ?)",
                      (code, num, name))
    conn.commit()


def get_total_watch_count(conn, chat_id):
    """Get total watched items (individual + watchlists) for a user."""
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM watched_companies WHERE chat_id = ?", (str(chat_id),))
    individual = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM watchlist_subscribers WHERE chat_id = ?", (str(chat_id),))
    wl = c.fetchone()[0]
    return individual + wl


# ─── Core monitoring ───

def get_all_watched_company_numbers(conn):
    """Get all unique company numbers that at least one user watches."""
    c = conn.cursor()
    c.execute("SELECT DISTINCT company_number FROM watched_companies")
    individual = set(row[0] for row in c.fetchall())
    c.execute("SELECT DISTINCT company_number FROM watchlist_companies")
    watchlist = set(row[0] for row in c.fetchall())
    return individual | watchlist


def check_filings_for_companies(conn, company_numbers):
    """Check filings for a set of companies. Returns list of new filings."""
    new_filings = []
    c = conn.cursor()
    for num in company_numbers:
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
                c.execute("SELECT company_name FROM known_companies WHERE company_number = ?", (num,))
                row = c.fetchone()
                name = row[0] if row else num
                new_filings.append({"company_number": num, "company_name": name, "filing_date": fdate, "filing_type": ftype, "description": desc})
            except sqlite3.IntegrityError:
                pass
        time.sleep(0.5)
    return new_filings


def process_new_filings(conn, new_filings):
    """For each new filing, generate summary and store in DB. Returns count."""
    if not new_filings:
        return 0
    summary_date = datetime.utcnow().strftime("%Y-%m-%d")
    c = conn.cursor()
    count = 0
    for filing in new_filings:
        summary = generate_summary(filing["company_name"], filing["company_number"],
                                   filing["filing_date"], filing["filing_type"], filing["description"])
        try:
            c.execute("""INSERT INTO filing_summaries 
                (company_number, company_name, filing_date, filing_type, description, summary, summary_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (filing["company_number"], filing["company_name"], filing["filing_date"],
                 filing["filing_type"], filing["description"], summary, summary_date))
            conn.commit()
            count += 1
            print(f"  ✓ {filing['company_name']} — {filing['filing_type']}")
        except sqlite3.IntegrityError:
            pass
    return count


# ─── Per-user alerts ───

def get_user_relevant_summaries(conn, chat_id, since_date=None):
    if not since_date:
        since_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    c = conn.cursor()
    chat_id = str(chat_id)
    c.execute("SELECT company_number FROM watched_companies WHERE chat_id = ?", (chat_id,))
    individual = set(row[0] for row in c.fetchall())
    c.execute("""SELECT wc.company_number FROM watchlist_companies wc
        JOIN watchlist_subscribers ws ON wc.watchlist_code = ws.watchlist_code WHERE ws.chat_id = ?""", (chat_id,))
    watchlist = set(row[0] for row in c.fetchall())
    relevant = individual | watchlist
    if not relevant:
        return []
    placeholders = ",".join("?" * len(relevant))
    c.execute(f"""SELECT company_number, company_name, filing_date, filing_type, summary
        FROM filing_summaries WHERE company_number IN ({placeholders}) AND summary_date >= ?
        ORDER BY filing_date DESC""", list(relevant) + [since_date])
    return c.fetchall()


def format_user_alert(summaries):
    if not summaries:
        return None
    lines = ["📋 <b>Your Company Alerts</b>\n"]
    by_company = {}
    for num, name, fdate, ftype, summary in summaries:
        if num not in by_company:
            by_company[num] = {"name": name, "filings": []}
        by_company[num]["filings"].append({"date": fdate, "summary": summary})
    for num, data in by_company.items():
        lines.append(f"🏢 <b>{data['name']}</b> ({num})")
        for f in data["filings"]:
            lines.append(f"  {f['summary']}")
        lines.append("")
    return "\n".join(lines)


def send_per_user_alerts(conn):
    c = conn.cursor()
    c.execute("SELECT chat_id FROM subscribers")
    subs = [row[0] for row in c.fetchall()]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    sent = 0
    for chat_id in subs:
        summaries = get_user_relevant_summaries(conn, chat_id, since_date=today)
        alert = format_user_alert(summaries)
        if alert:
            send_telegram(chat_id, alert)
            sent += 1
            time.sleep(0.1)
    return sent


# ─── Telegram command processing ───

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
    except Exception:
        return []


def process_telegram_commands(conn):
    c = conn.cursor()
    offset_file = DATA_DIR / "last_update.txt"
    offset = int(offset_file.read_text().strip()) + 1 if offset_file.exists() else None
    updates = get_updates(offset=offset)
    if not updates:
        return
    last_id = 0
    for update in updates:
        last_id = max(last_id, update["update_id"])
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        if not text or not chat_id:
            continue
        c.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        if text == "/start":
            send_telegram(chat_id, "Welcome to UK Company Watch! 🇬🇧\n\nCommands:\n/search [name]\n/company [number]\n/watch [number]\n/watching\n/watchlists\n/join [code]\n/leave [code]\n/digest\n/pricing\n\nFree: 1 watch, 3 alerts/day")
        elif text == "/help":
            send_telegram(chat_id, "Commands:\n/search [name]\n/company [number]\n/watch [number]\n/watching\n/watchlists\n/join [code]\n/leave [code]\n/digest\n/pricing")
        elif text == "/watching":
            handle_watching(chat_id, conn)
        elif text == "/watchlists":
            handle_watchlists(chat_id, conn)
        elif text == "/digest":
            today = datetime.utcnow().strftime("%Y-%m-%d")
            summaries = get_user_relevant_summaries(conn, chat_id, since_date=today)
            alert = format_user_alert(summaries)
            send_telegram(chat_id, alert or f"No new filings today ({today}).")
        elif text == "/pricing":
            send_telegram(chat_id,
                "📊 <b>UK Company Watch Pricing</b>\n\n"
                "🆓 <b>Free</b> — £0/month\n"
                "  • 1 watch\n"
                "  • 3 alerts/day\n\n"
                "⭐ <b>Pro</b> — £4.99/month\n"
                "  • 10 watches\n"
                "  • 50 alerts/day\n"
                "  • Priority filing alerts\n\n"
                "🏢 <b>Business</b> — £19.99/month\n"
                "  • Unlimited watches\n"
                "  • Unlimited alerts\n"
                "  • API access\n\n"
                "Upgrade: /upgrade pro\n"
                "Cancel: /cancel"
            )
        elif text.startswith("/upgrade "):
            plan = text[9:].strip().lower()
            if plan not in ("pro", "business"):
                send_telegram(chat_id, "Usage: /upgrade pro or /upgrade business")
                continue
            if not STRIPE_SECRET_KEY:
                send_telegram(chat_id, "Payment system not configured yet. Please try later.")
                continue
            price_id = STRIPE_PRICE_PRO if plan == "pro" else STRIPE_PRICE_BUSINESS
            # Create Stripe checkout session
            success_url = f"https://t.me/UK_Company_Watch_Bot?start=upgraded_{plan}"
            cancel_url = f"https://t.me/UK_Company_Watch_Bot?start=cancel"
            try:
                data = urllib.parse.urlencode({
                    "mode": "subscription",
                    "line_items[0][price]": price_id,
                    "line_items[0][quantity]": "1",
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                    "metadata[chat_id]": chat_id,
                    "metadata[plan]": plan,
                    "customer_email": "",
                    "allow_promotion_codes": "true",
                }).encode()
                req = urllib.request.Request(
                    "https://api.stripe.com/v1/checkout/sessions",
                    data=data,
                    headers={
                        "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    session = json.loads(resp.read().decode())
                    checkout_url = session.get("url", "")
                    if checkout_url:
                        send_telegram(chat_id,
                            f"💳 <b>Upgrade to {plan.capitalize()}</b>\n\n"
                            f"Click below to complete your subscription:\n"
                            f"{checkout_url}\n\n"
                            f"After payment, your account will be upgraded automatically."
                        )
                    else:
                        send_telegram(chat_id, "Error creating checkout session. Please try again.")
            except Exception as e:
                send_telegram(chat_id, f"Payment error: {e}")
        elif text == "/cancel":
            c.execute("SELECT stripe_subscription_id, plan FROM subscribers WHERE chat_id = ?", (chat_id,))
            row = c.fetchone()
            if not row or not row[0]:
                send_telegram(chat_id, "You don't have an active subscription.")
                continue
            sub_id, current_plan = row
            if current_plan == "free":
                send_telegram(chat_id, "You're on the free plan already.")
                continue
            # Create Stripe billing portal session
            try:
                c.execute("SELECT stripe_customer_id FROM subscribers WHERE chat_id = ?", (chat_id,))
                customer_row = c.fetchone()
                customer_id = customer_row[0] if customer_row else ""
                if customer_id:
                    data = urllib.parse.urlencode({
                        "customer": customer_id,
                        "return_url": "https://t.me/UK_Company_Watch_Bot",
                    }).encode()
                    req = urllib.request.Request(
                        "https://api.stripe.com/v1/billing_portal/sessions",
                        data=data,
                        headers={
                            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        portal = json.loads(resp.read().decode())
                        portal_url = portal.get("url", "")
                        if portal_url:
                            send_telegram(chat_id,
                                "⚠️ <b>Cancel Subscription</b>\n\n"
                                "Click below to manage your subscription:\n"
                                f"{portal_url}\n\n"
                                "You'll keep access until the end of your billing period."
                            )
                        else:
                            send_telegram(chat_id, "Error creating portal session. Please contact support.")
                else:
                    send_telegram(chat_id, "No Stripe customer found. Contact support.")
            except Exception as e:
                send_telegram(chat_id, f"Error: {e}")
        elif text.startswith("upgraded_"):
            # User returned from successful checkout — fetch subscription from Stripe
            plan = text.replace("upgraded_", "")
            limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
            # Try to get the subscription ID from Stripe and store it
            try:
                # List checkout sessions for this customer to find the subscription
                data = urllib.parse.urlencode({
                    "limit": "1",
                }).encode()
                req = urllib.request.Request(
                    "https://api.stripe.com/v1/checkout/sessions",
                    data=data,
                    headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    sessions = json.loads(resp.read().decode())
                    for session in sessions.get("data", []):
                        if session.get("metadata", {}).get("chat_id") == chat_id:
                            sub_id = session.get("subscription", "")
                            customer_id = session.get("customer", "")
                            if sub_id:
                                c.execute("""UPDATE subscribers SET
                                    stripe_subscription_id=?, stripe_customer_id=?,
                                    subscription_status='active'
                                    WHERE chat_id=?""", (sub_id, customer_id, chat_id))
                                conn.commit()
                            break
            except Exception as e:
                print(f"Error fetching subscription for {chat_id}: {e}")

            send_telegram(chat_id,
                f"✅ <b>Welcome to {plan.capitalize()}!</b>\n\n"
                f"Your subscription is being activated. You have {limits['max_watched']} watches "
                f"and {limits['max_alerts_per_day']} alerts/day.\n\n"
                "Use /watchlists to join group watchlists or /watch [number] to watch individual companies."
            )
        elif text.startswith("/search "):
            q = text[8:].strip()
            if len(q) < 2:
                send_telegram(chat_id, "Query too short.")
            else:
                data = ch_fetch(f"/search/companies?q={urllib.parse.quote(q)}&items_per_page=5")
                if data and data.get("items"):
                    lines = [f'Results for "{q}":\n']
                    for item in data["items"][:5]:
                        lines.append(f'• <b>{item["title"]}</b> ({item["company_number"]}) — {item.get("company_status","?")}')
                    send_telegram(chat_id, "\n".join(lines))
                else:
                    send_telegram(chat_id, f'No results for "{q}".')
        elif text.startswith("/company "):
            num = text[9:].strip()
            d = ch_fetch(f"/company/{num}")
            if d:
                send_telegram(chat_id, f"📊 <b>{d.get('company_name','?')}</b>\n#{num}\nStatus: {d.get('company_status','?')}\nType: {d.get('type','?')}\nFounded: {d.get('date_of_creation','?')}\nSIC: {', '.join(d.get('sic_codes',[])) or 'N/A'}")
            else:
                send_telegram(chat_id, f"Company {num} not found.")
        elif text.startswith("/watch "):
            # Check plan limit
            c.execute("SELECT plan FROM subscribers WHERE chat_id = ?", (chat_id,))
            row = c.fetchone()
            plan = row[0] if row else "free"
            limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
            current = get_total_watch_count(conn, chat_id)
            if current >= limits["max_watched"]:
                send_telegram(chat_id,
                    f"⚠️ Watch limit reached ({current}/{limits['max_watched']}).\n"
                    f"Upgrade: /upgrade pro"
                )
                continue
            num = text[7:].strip()
            d = ch_fetch(f"/company/{num}")
            name = d.get("company_name", num) if d else num
            try:
                c.execute("INSERT INTO watched_companies (chat_id, company_number, company_name) VALUES (?, ?, ?)", (chat_id, num, name))
                conn.commit()
                send_telegram(chat_id, f"✅ Watching <b>{name}</b> ({num}) ({current+1}/{limits['max_watched']} watches)")
            except sqlite3.IntegrityError:
                send_telegram(chat_id, f"Already watching {name}.")
        elif text.startswith("/join "):
            code = text[6:].strip().lower()
            wl = WATCHLISTS.get(code)
            if not wl:
                send_telegram(chat_id, f"Watchlist '{code}' not found. Use /watchlists.")
                continue
            # Check plan limit
            c.execute("SELECT plan FROM subscribers WHERE chat_id = ?", (chat_id,))
            row = c.fetchone()
            plan = row[0] if row else "free"
            limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
            current = get_total_watch_count(conn, chat_id)
            if current >= limits["max_watched"]:
                send_telegram(chat_id,
                    f"⚠️ Watch limit reached ({current}/{limits['max_watched']}).\n"
                    f"Upgrade: /upgrade pro"
                )
                continue
            if not wl:
                send_telegram(chat_id, f"Watchlist '{code}' not found. Use /watchlists.")
            else:
                try:
                    c.execute("INSERT INTO watchlist_subscribers (chat_id, watchlist_code) VALUES (?, ?)", (chat_id, code))
                    conn.commit()
                    send_telegram(chat_id, f"✅ Joined {wl['icon']} <b>{wl['name']}</b> ({len(wl['companies'])} companies)")
                except sqlite3.IntegrityError:
                    send_telegram(chat_id, f"Already joined {wl['name']}.")
        elif text.startswith("/leave "):
            code = text[7:].strip().lower()
            c.execute("DELETE FROM watchlist_subscribers WHERE chat_id = ? AND watchlist_code = ?", (chat_id, code))
            if c.rowcount > 0:
                conn.commit()
                send_telegram(chat_id, f"Left watchlist '{code}'.")
            else:
                send_telegram(chat_id, f"Not subscribed to '{code}'.")
        time.sleep(0.1)
    if last_id > 0:
        offset_file.write_text(str(last_id))


def handle_watching(chat_id, conn):
    c = conn.cursor()
    c.execute("SELECT company_number, company_name FROM watched_companies WHERE chat_id = ?", (str(chat_id),))
    watched = c.fetchall()
    c.execute("SELECT w.code, w.name, w.icon, w.company_count FROM watchlist_subscribers ws JOIN watchlists w ON ws.watchlist_code = w.code WHERE ws.chat_id = ?", (str(chat_id),))
    wl = c.fetchall()
    if not watched and not wl:
        send_telegram(chat_id, "Not watching anything. Use /watch or /watchlists.")
        return
    lines = ["📋 Your watches:\n"]
    for num, name in watched:
        lines.append(f"• <b>{name}</b> ({num})")
    for code, name, icon, count in wl:
        lines.append(f"{icon} <b>{name}</b> ({count} cos) — /leave {code}")
    send_telegram(chat_id, "\n".join(lines))


def handle_watchlists(chat_id, conn):
    lines = ["📋 <b>Group Watchlists</b>\n"]
    for code, wl in WATCHLISTS.items():
        lines.append(f"{wl['icon']} <b>{wl['name']}</b> — {len(wl['companies'])} companies")
        lines.append(f"   /join {code}\n")
    send_telegram(chat_id, "\n".join(lines))


# ─── Cleanup ───

def cleanup_old_summaries(conn, days=7):
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("DELETE FROM filing_summaries WHERE summary_date < ?", (cutoff,))
    if c.rowcount > 0:
        print(f"  Cleaned {c.rowcount} old summaries")


# ─── Stripe sync ───

def sync_stripe_subscriptions(conn):
    """Sync subscription status with Stripe. Runs on every bot cycle."""
    if not STRIPE_SECRET_KEY:
        return

    c = conn.cursor()
    c.execute("SELECT chat_id, stripe_subscription_id, stripe_customer_id FROM subscribers WHERE stripe_subscription_id IS NOT NULL OR stripe_customer_id IS NOT NULL")
    rows = c.fetchall()
    if not rows:
        return

    updated = 0
    for chat_id, sub_id, customer_id in rows:
        try:
            # Check subscription status in Stripe
            if sub_id:
                data = urllib.parse.urlencode({}).encode()
                req = urllib.request.Request(
                    f"https://api.stripe.com/v1/subscriptions/{sub_id}",
                    headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    sub = json.loads(resp.read().decode())
                    status = sub.get("status", "")
                    period_end = sub.get("current_period_end", "")

                    if status in ("active", "trialing"):
                        # Get price to determine plan
                        price_id = sub.get("items", {}).get("data", [{}])[0].get("price", {}).get("id", "")
                        if price_id == STRIPE_PRICE_PRO:
                            plan = "pro"
                        elif price_id == STRIPE_PRICE_BUSINESS:
                            plan = "business"
                        else:
                            plan = "pro"  # fallback

                        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
                        c.execute("""UPDATE subscribers SET plan=?, subscription_status=?, current_period_end=?,
                            max_watched=?, max_alerts_per_day=? WHERE chat_id=?""",
                            (plan, "active", str(period_end), limits["max_watched"], limits["max_alerts_per_day"], chat_id))
                        updated += 1
                    elif status in ("canceled", "unpaid", "past_due"):
                        c.execute("""UPDATE subscribers SET plan='free', subscription_status=?,
                            max_watched=1, max_alerts_per_day=3 WHERE chat_id=?""",
                            (status, chat_id))
                        updated += 1
        except Exception as e:
            print(f"  Stripe sync error for {chat_id}: {e}")

    if updated:
        conn.commit()
        print(f"  Synced {updated} subscriptions from Stripe")


# ─── Main ───

def main():
    print(f"\n{'='*60}\nUK Company Watch — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n{'='*60}")
    conn = init_db()
    init_watchlists(conn)

    print("\n[1] Processing Telegram commands...")
    process_telegram_commands(conn)

    print("\n[2] Syncing Stripe subscriptions...")
    sync_stripe_subscriptions(conn)

    print("\n[3] Checking watched companies for new filings...")
    watched = get_all_watched_company_numbers(conn)
    print(f"  {len(watched)} companies to check")
    new_filings = check_filings_for_companies(conn, watched)
    print(f"  {len(new_filings)} new filings found")

    print("\n[4] Generating summaries...")
    summary_count = process_new_filings(conn, new_filings)
    print(f"  {summary_count} summaries stored")

    print("\n[5] Sending per-user alerts...")
    sent = send_per_user_alerts(conn)
    print(f"  Alerts sent to {sent} users")

    cleanup_old_summaries(conn)

    print(f"\n{'='*60}\nDone.\n{'='*60}")
    conn.close()


if __name__ == "__main__":
    main()
