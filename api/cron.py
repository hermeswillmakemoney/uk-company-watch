#!/usr/bin/env python3
"""
UK Company Watch — Cron Job Handler.
Vercel serverless function at /api/cron.
Runs every hour to check Companies House for new filings/insolvencies.
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def handler(event, context=None):
    """Vercel cron job entry point."""
    from api import store
    from api.companies_house import (
        search_insolvencies,
        advanced_search,
        get_filing_history,
    )
    from api.telegram_bot import send_telegram

    print(f"UK Company Watch cron — {datetime.utcnow().isoformat()}")

    store.update_last_run()
    results = {"insolvencies": 0, "new_companies": 0, "new_filings": 0, "alerts_sent": 0}

    # 1. Check for new insolvencies
    try:
        insolvency_data = search_insolvencies(items_per_page=30)
        if insolvency_data and insolvency_data.get("items"):
            for item in insolvency_data["items"]:
                num = item["company_number"]
                name = item.get("title", "Unknown")
                if not store.is_company_known(num):
                    store.add_company(num, name, "insolvency", item.get("date_of_creation", ""))
                    results["insolvencies"] += 1
                    print(f"  New insolvency: {name} ({num})")
    except Exception as e:
        print(f"  Insolvency check error: {e}")

    # 2. Check for new companies (last 24 hours)
    try:
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        new_data = advanced_search(incorporated_from=yesterday, items_per_page=50)
        if new_data and new_data.get("items"):
            for item in new_data["items"]:
                num = item["company_number"]
                name = item.get("title", "Unknown")
                if not store.is_company_known(num):
                    store.add_company(
                        num, name, item.get("company_status", "active"), item.get("date_of_creation", "")
                    )
                    results["new_companies"] += 1
    except Exception as e:
        print(f"  New company check error: {e}")

    # 3. Check filings for all watched companies
    try:
        watched_numbers = store.get_all_watched_company_numbers()
        for num in watched_numbers:
            try:
                filing_data = get_filing_history(num, items_per_page=3)
                if filing_data and filing_data.get("items"):
                    for f in filing_data["items"]:
                        fdate = f.get("date", "")
                        ftype = f.get("type", "")
                        desc = f.get("description", "")
                        if not store.is_filing_known(num, fdate, ftype):
                            store.add_filing(num, fdate, ftype, desc)
                            results["new_filings"] += 1
            except Exception as e:
                print(f"  Filing check error for {num}: {e}")
    except Exception as e:
        print(f"  Watched companies error: {e}")

    # 4. Send insolvency alerts to all subscribers
    try:
        subscribers = store.get_subscribers()
        unalerted = store.get_unalerted_insolvencies(limit=10)
        if unalerted:
            nums = [n for n, _ in unalerted]
            msg = "⚠️ <b>New Insolvency Alerts</b>\n\n"
            for num, name in unalerted[:5]:
                msg += f"• <b>{name}</b> ({num})\n"
            msg += "\nSource: Companies House"
            for chat_id in subscribers:
                send_telegram(chat_id, msg)
                results["alerts_sent"] += 1
            store.mark_insolvencies_alerted(nums[:5])
    except Exception as e:
        print(f"  Alert error: {e}")

    # 5. Save store
    try:
        store.save_store()
    except Exception as e:
        print(f"  Save error: {e}")

    summary = (
        f"Cron complete: {results['insolvencies']} insolvencies, "
        f"{results['new_companies']} new companies, "
        f"{results['new_filings']} new filings, "
        f"{results['alerts_sent']} alerts sent"
    )
    print(summary)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True, "results": results, "summary": summary}),
    }
