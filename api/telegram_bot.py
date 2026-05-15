#!/usr/bin/env python3
"""Telegram bot functions."""

import urllib.request
import urllib.parse
import json
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


def send_telegram(chat_id, text, parse_mode="HTML"):
    """Send message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        print(f"[Would send to {chat_id}]: {text[:200]}")
        return False

    data = urllib.parse.urlencode({
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
    }).encode()

    try:
        req = urllib.request.urlopen(
            urllib.request.Request(f"{TELEGRAM_API}/sendMessage", data=data),
            timeout=15,
        )
        return json.loads(req.read().decode()).get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def set_webhook(url):
    """Set the Telegram webhook URL."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    data = urllib.parse.urlencode({"url": url}).encode()
    try:
        req = urllib.request.urlopen(
            urllib.request.Request(f"{TELEGRAM_API}/setWebhook", data=data),
            timeout=15,
        )
        return json.loads(req.read().decode())
    except Exception as e:
        print(f"setWebhook error: {e}")
        return None


def process_message(chat_id, text, store_module):
    """Process an incoming Telegram message and return response."""
    from datetime import datetime

    chat_id = str(chat_id)

    # Auto-register subscriber
    store_module.add_subscriber(chat_id)

    text = text.strip()

    if text == "/start":
        return (
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
        return (
            "Commands:\n"
            "/search [name] — Search companies\n"
            "/company [number] — Get company details\n"
            "/watch [number] — Watch a company\n"
            "/watching — Your watched companies\n"
            "/digest — Today's summary\n"
            "/pricing — Upgrade plans"
        )

    elif text.startswith("/search "):
        query = text[8:].strip()
        if len(query) < 2:
            return "Query must be at least 2 characters."

        from .companies_house import search_companies
        data = search_companies(query)
        if data and data.get("items"):
            lines = [f'Found {data.get("total_results", 0)} companies for "{query}":\n']
            for item in data["items"][:5]:
                lines.append(
                    f'• <b>{item["title"]}</b> ({item["company_number"]}) — {item.get("company_status", "?")}'
                )
                lines.append(f'  /company {item["company_number"]} | /watch {item["company_number"]}\n')
            return "\n".join(lines)
        return f'No results for "{query}".'

    elif text.startswith("/company "):
        num = text[9:].strip()
        from .companies_house import get_company
        data = get_company(num)
        if data:
            name = data.get("company_name", "Unknown")
            status = data.get("company_status", "?")
            addr = data.get("registered_office_address", {})
            address_parts = [
                addr.get(k, "")
                for k in ["address_line_1", "address_line_2", "locality", "postal_code"]
                if addr.get(k)
            ]
            address = ", ".join(address_parts) if address_parts else "N/A"
            sic = ", ".join(data.get("sic_codes", [])) or "N/A"

            msg = f"📊 <b>{name}</b>\n"
            msg += f"Number: {num}\n"
            msg += f"Status: {status}\n"
            msg += f"Type: {data.get('type', '?')}\n"
            msg += f"Founded: {data.get('date_of_creation', 'N/A')}\n"
            msg += f"SIC: {sic}\n"
            msg += f"Address: {address}\n"

            accts = data.get("accounts", {})
            if accts.get("overdue"):
                msg += "⚠️ Accounts OVERDUE\n"
            cs = data.get("confirmation_statement", {})
            if cs.get("overdue"):
                msg += "⚠️ Confirmation statement OVERDUE\n"

            msg += f"\nView: https://find-and-update.company-information.service.gov.uk/company/{num}"
            return msg
        return f"Company {num} not found."

    elif text.startswith("/watch "):
        num = text[7:].strip()
        sub = store_module.get_subscribers().get(chat_id, {})
        max_watched = sub.get("max_watched", 1)
        current = len(store_module.get_watched_companies(chat_id))

        if current >= max_watched:
            return f"You're watching {current}/{max_watched} companies. Use /pricing to upgrade."

        from .companies_house import get_company
        data = get_company(num)
        name = data.get("company_name", num) if data else num

        added = store_module.add_watched_company(chat_id, num, name)
        if added:
            return f"✅ Now watching <b>{name}</b> ({num}). You'll get alerts for new filings."
        return f"You're already watching {name} ({num})."

    elif text == "/watching":
        watched = store_module.get_watched_companies(chat_id)
        if watched:
            lines = ["📋 Your watched companies:\n"]
            for key, v in watched.items():
                lines.append(f'• <b>{v["company_name"]}</b> ({v["company_number"]})')
            return "\n".join(lines)
        return "You're not watching any companies. Use /watch [number] to start."

    elif text == "/digest":
        subs = store_module.get_subscribers()
        new_companies = store_module.new_companies_count_today()
        insolvencies = store_module.insolvencies_count_today()
        filings = store_module.filings_count_today()

        return (
            f"📊 <b>UK Company Watch — Daily Digest</b>\n"
            f"📅 {datetime.utcnow().strftime('%d %B %Y')}\n\n"
            f"🏢 New companies: <b>{new_companies}</b>\n"
            f"⚠️ Insolvencies: <b>{insolvencies}</b>\n"
            f"📋 New filings tracked: <b>{filings}</b>\n\n"
            f"Reply /help for commands."
        )

    elif text == "/pricing":
        return (
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
            f"with note: UCW Pro [your chat id]\n"
            f"Your chat id: {chat_id}"
        )

    return "Unknown command. Reply /help for available commands."
