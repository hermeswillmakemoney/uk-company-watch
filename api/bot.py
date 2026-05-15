"""
UK Company Watch — Telegram Bot Webhook Handler.
Vercel serverless function at /api/bot.
"""

import json
import os
import urllib.request
import urllib.parse
import base64
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CH_API_KEY = os.environ.get("CH_API_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "price_1TXSqILyJWmpaKc9lnjQ2KoI")
STRIPE_PRICE_BUSINESS = os.environ.get("STRIPE_PRICE_BUSINESS", "price_1TXSr6LyJWmpaKc9xGV1iVtW")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import load_db, save_db, get_subscriber, get_total_watch_count, get_user_watched_companies, init_watchlists, PLAN_LIMITS, WATCHLISTS


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


def create_stripe_checkout(chat_id, plan):
    price_id = STRIPE_PRICE_PRO if plan == "pro" else STRIPE_PRICE_BUSINESS
    success_url = f"https://t.me/UK_Company_Watch_Bot?start=upgraded_{plan}"
    cancel_url = f"https://t.me/UK_Company_Watch_Bot?start=cancel"
    data = urllib.parse.urlencode({
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[chat_id]": str(chat_id),
        "metadata[plan]": plan,
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
        return json.loads(resp.read().decode())


def handle_command(db, chat_id, text):
    chat_id = str(chat_id)
    sub = get_subscriber(db, chat_id)
    init_watchlists(db)

    if text.startswith("upgraded_"):
        plan = text.replace("upgraded_", "")
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        sub["plan"] = plan
        sub["subscription_status"] = "active"
        sub["max_watched"] = limits["max_watched"]
        sub["max_alerts_per_week"] = limits["max_alerts_per_week"]
        save_db(db, "")
        return (
            f"✅ <b>Welcome to {plan.capitalize()}!</b>\n\n"
            f"Your subscription is active. You have {limits['max_watched']} watches "
            f"and {limits['max_alerts_per_week']} alerts/week.\n\n"
            "Use /watchlists to join group watchlists or /watch [number] to watch individual companies."
        )

    if text == "/start":
        return (
            "Welcome to UK Company Watch! 🇬🇧\n\n"
            "Commands:\n"
            "/search [name] — Search companies\n"
            "/company [number] — Company details\n"
            "/watch [number] — Watch a company\n"
            "/watching — Your watches\n"
            "/watchlists — Group watchlists\n"
            "/join [code] — Join a watchlist\n"
            "/leave [code] — Leave a watchlist\n"
            "/digest — Today's filings\n"
            "/pricing — View plans\n"
            "/upgrade — Upgrade plan\n"
            "/cancel — Cancel subscription\n\n"
            "Free: 1 watch, 3 alerts/week"
        )

    if text == "/help":
        return (
            "Commands:\n"
            "/search [name] — Search companies\n"
            "/company [number] — Company details\n"
            "/watch [number] — Watch a company\n"
            "/watching — Your watches\n"
            "/watchlists — Group watchlists\n"
            "/join [code] — Join a watchlist\n"
            "/leave [code] — Leave a watchlist\n"
            "/digest — Today's filings\n"
            "/pricing — View plans"
        )

    if text == "/pricing":
        return (
            "📊 <b>UK Company Watch Pricing</b>\n\n"
            "🆓 <b>Free</b> — £0/month\n"
            "  • 1 watch\n"
            "  • 3 alerts/week\n\n"
            "⭐ <b>Pro</b> — £4.99/month\n"
            "  • 10 watches\n"
            "  • 50 alerts/week\n"
            "  • Priority filing alerts\n\n"
            "🏢 <b>Business</b> — £19.99/month\n"
            "  • Unlimited watches\n"
            "  • Unlimited alerts\n"
            "  • API access\n\n"
            "Upgrade: /upgrade pro\n"
            "Cancel: /cancel"
        )

    if text == "/watching":
        individual = db["watched_companies"].get(chat_id, [])
        lines = ["📋 Your watches:\n"]
        for num in individual:
            name = db["known_companies"].get(num, {}).get("name", num)
            lines.append(f"• <b>{name}</b> ({num})")
        user_wl = [code for code, members in db["watchlist_subscribers"].items() if chat_id in members]
        for code in user_wl:
            wl = db["watchlists"].get(code, {})
            count = len(wl.get("companies", []))
            lines.append(f"{wl.get('icon', '📋')} <b>{wl.get('name', code)}</b> ({count} cos) — /leave {code}")
        if not individual and not user_wl:
            return "Not watching anything. Use /watch or /watchlists."
        return "\n".join(lines)

    if text == "/watchlists":
        lines = ["📋 <b>Group Watchlists</b>\n"]
        for code, wl in db["watchlists"].items():
            count = len(wl.get("companies", []))
            lines.append(f"{wl.get('icon', '📋')} <b>{wl.get('name', code)}</b> — {count} companies")
            lines.append(f"   /join {code}\n")
        return "\n".join(lines)

    if text == "/digest":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        companies = get_user_watched_companies(db, chat_id)
        summaries = [s for s in db.get("filing_summaries", []) if s["company_number"] in companies and s.get("summary_date") == today]
        if not summaries:
            return f"No new filings today ({today})."
        lines = ["📋 <b>Today's Filings</b>\n"]
        for s in summaries:
            lines.append(f"🏢 <b>{s['company_name']}</b> ({s['company_number']})")
            lines.append(f"  {s['summary']}")
            lines.append("")
        return "\n".join(lines)

    if text.startswith("/search "):
        q = text[8:].strip()
        if len(q) < 2:
            return "Query too short."
        data = ch_fetch(f"/search/companies?q={urllib.parse.quote(q)}&items_per_page=5")
        if data and data.get("items"):
            lines = [f'Results for "{q}":\n']
            for item in data["items"][:5]:
                lines.append(f'• <b>{item["title"]}</b> ({item["company_number"]}) — {item.get("company_status", "?")}')
            return "\n".join(lines)
        return f'No results for "{q}".'

    if text.startswith("/company "):
        num = text[9:].strip()
        d = ch_fetch(f"/company/{num}")
        if d:
            return (f"📊 <b>{d.get('company_name', '?')}</b>\n#{num}\n"
                    f"Status: {d.get('company_status', '?')}\nType: {d.get('type', '?')}\n"
                    f"Founded: {d.get('date_of_creation', '?')}\n"
                    f"SIC: {', '.join(d.get('sic_codes', [])) or 'N/A'}")
        return f"Company {num} not found."

    if text.startswith("/watch "):
        num = text[7:].strip()
        limits = PLAN_LIMITS.get(sub["plan"], PLAN_LIMITS["free"])
        current = get_total_watch_count(db, chat_id)
        if current >= limits["max_watched"]:
            return f"⚠️ Watch limit reached ({current}/{limits['max_watched']}).\nUpgrade: /upgrade pro"
        d = ch_fetch(f"/company/{num}")
        name = d.get("company_name", num) if d else num
        if chat_id not in db["watched_companies"]:
            db["watched_companies"][chat_id] = []
        if num not in db["watched_companies"][chat_id]:
            db["watched_companies"][chat_id].append(num)
            db["known_companies"][num] = {"name": name}
            save_db(db, "")
        return f"✅ Watching <b>{name}</b> ({num}) ({current+1}/{limits['max_watched']} watches)"

    if text.startswith("/join "):
        code = text[6:].strip().lower()
        wl = db["watchlists"].get(code)
        if not wl:
            return f"Watchlist '{code}' not found. Use /watchlists."
        limits = PLAN_LIMITS.get(sub["plan"], PLAN_LIMITS["free"])
        current = get_total_watch_count(db, chat_id)
        if current >= limits["max_watched"]:
            return f"⚠️ Watch limit reached ({current}/{limits['max_watched']}).\nUpgrade: /upgrade pro"
        if code not in db["watchlist_subscribers"]:
            db["watchlist_subscribers"][code] = []
        if chat_id not in db["watchlist_subscribers"][code]:
            db["watchlist_subscribers"][code].append(chat_id)
            save_db(db, "")
        return f"✅ Joined {wl.get('icon', '')} <b>{wl['name']}</b> ({len(wl.get('companies', []))} companies)"

    if text.startswith("/leave "):
        code = text[7:].strip().lower()
        if code in db["watchlist_subscribers"] and chat_id in db["watchlist_subscribers"][code]:
            db["watchlist_subscribers"][code].remove(chat_id)
            save_db(db, "")
            return f"Left watchlist '{code}'."
        return f"Not subscribed to '{code}'."

    if text.startswith("/upgrade "):
        plan = text[9:].strip().lower()
        if plan not in ("pro", "business"):
            return "Usage: /upgrade pro or /upgrade business"
        if not STRIPE_SECRET_KEY:
            return "Payment system not configured. Please try later."
        try:
            session = create_stripe_checkout(chat_id, plan)
            checkout_url = session.get("url", "")
            if checkout_url:
                return (f"💳 <b>Upgrade to {plan.capitalize()}</b>\n\n"
                        f"Click below to complete your subscription:\n{checkout_url}\n\n"
                        "After payment, your account will be upgraded automatically.")
            return "Error creating checkout session."
        except Exception as e:
            return f"Payment error: {e}"

    if text == "/cancel":
        if sub["plan"] == "free":
            return "You're on the free plan already."
        sub["plan"] = "free"
        sub["max_watched"] = 1
        sub["max_alerts_per_week"] = 3
        sub["subscription_status"] = None
        save_db(db, "")
        return "✅ Your subscription has been cancelled. You're now on the Free plan."

    return None


# Vercel Python serverless function entry point
def handler(request):
    if request.method == "GET":
        return {"statusCode": 200, "body": "UK Company Watch Bot — OK"}

    try:
        body = json.loads(request.body)
    except Exception:
        return {"statusCode": 400, "body": "Bad request"}

    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    if not text or not chat_id:
        return {"statusCode": 200, "body": "ok"}

    db, sha = load_db()
    result = handle_command(db, chat_id, text)

    if result:
        send_telegram(chat_id, result)

    return {"statusCode": 200, "body": "ok"}
