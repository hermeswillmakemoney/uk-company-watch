#!/usr/bin/env python3
"""
UK Company Watch — Data store backed by GitHub JSON file.
Reads/writes store.json from the data directory.
Commits changes back to GitHub for persistence.
"""

import json
import os
import subprocess
from pathlib import Path
from copy import deepcopy

DATA_DIR = Path(__file__).parent.parent / "data"
STORE_PATH = DATA_DIR / "store.json"
REPO_ROOT = Path(__file__).parent.parent

# In-memory cache
_store = None


def load_store():
    """Load store from JSON file."""
    global _store
    if _store is not None:
        return _store

    if STORE_PATH.exists():
        with open(STORE_PATH) as f:
            _store = json.load(f)
    else:
        _store = {
            "subscribers": {},
            "watched_companies": {},
            "known_filings": {},
            "new_companies": {},
            "insolvencies": {},
            "last_run": None,
            "last_insolvency_check": None,
            "last_new_company_check": None,
        }
    return _store


def save_store(store=None):
    """Save store to JSON file and commit to GitHub."""
    global _store
    if store is not None:
        _store = store

    DATA_DIR.mkdir(exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(_store, f, indent=2, default=str)

    # Commit to GitHub for persistence
    try:
        subprocess.run(
            ["git", "add", "data/store.json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", "Update data store", "--allow-empty"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=30,
        )
    except Exception as e:
        print(f"Git commit/push failed (non-critical): {e}")


def get_subscribers():
    store = load_store()
    return store.get("subscribers", {})


def add_subscriber(chat_id, plan="free"):
    store = load_store()
    if chat_id not in store["subscribers"]:
        from datetime import datetime
        store["subscribers"][chat_id] = {
            "plan": plan,
            "joined_at": datetime.utcnow().isoformat(),
            "alerts_today": 0,
            "last_alert_date": None,
            "max_watched": 1 if plan == "free" else (10 if plan == "pro" else 999),
            "max_alerts_per_day": 3 if plan == "free" else (50 if plan == "pro" else 9999),
        }
        save_store()
    return store["subscribers"][chat_id]


def update_subscriber(chat_id, **kwargs):
    store = load_store()
    if chat_id in store["subscribers"]:
        store["subscribers"][chat_id].update(kwargs)
        save_store()


def get_watched_companies(chat_id=None):
    store = load_store()
    watched = store.get("watched_companies", {})
    if chat_id:
        return {k: v for k, v in watched.items() if v.get("chat_id") == chat_id}
    return watched


def add_watched_company(chat_id, company_number, company_name):
    store = load_store()
    key = f"{chat_id}:{company_number}"
    if key not in store["watched_companies"]:
        from datetime import datetime
        store["watched_companies"][key] = {
            "chat_id": chat_id,
            "company_number": company_number,
            "company_name": company_name,
            "added_at": datetime.utcnow().isoformat(),
        }
        save_store()
        return True
    return False


def is_filing_known(company_number, filing_date, filing_type):
    store = load_store()
    key = f"{company_number}:{filing_date}:{filing_type}"
    return key in store.get("known_filings", {})


def add_filing(company_number, filing_date, filing_type, description):
    store = load_store()
    key = f"{company_number}:{filing_date}:{filing_type}"
    if key not in store["known_filings"]:
        from datetime import datetime
        store["known_filings"][key] = {
            "company_number": company_number,
            "filing_date": filing_date,
            "filing_type": filing_type,
            "description": description,
            "first_seen": datetime.utcnow().isoformat(),
        }
        return True
    return False


def is_company_known(company_number):
    store = load_store()
    return company_number in store.get("new_companies", {})


def add_company(company_number, company_name, status, date_of_creation=""):
    store = load_store()
    if company_number not in store["new_companies"]:
        from datetime import datetime
        store["new_companies"][company_number] = {
            "company_name": company_name,
            "status": status,
            "date_of_creation": date_of_creation,
            "first_seen": datetime.utcnow().isoformat(),
            "alerted": False,
        }
        return True
    return False


def get_unalerted_insolvencies(limit=20):
    store = load_store()
    result = []
    for num, data in store.get("new_companies", {}).items():
        if data.get("status") == "insolvency" and not data.get("alerted"):
            result.append((num, data.get("company_name", "Unknown")))
            if len(result) >= limit:
                break
    return result


def mark_insolvencies_alerted(company_numbers):
    store = load_store()
    for num in company_numbers:
        if num in store["new_companies"]:
            store["new_companies"][num]["alerted"] = True
    save_store()


def get_all_watched_company_numbers():
    store = load_store()
    watched = store.get("watched_companies", {})
    return list(set(v["company_number"] for v in watched.values()))


def update_last_run():
    store = load_store()
    from datetime import datetime
    store["last_run"] = datetime.utcnow().isoformat()
    save_store()


def new_companies_count_today():
    store = load_store()
    from datetime import datetime, date
    today = date.today().isoformat()
    count = 0
    for num, data in store.get("new_companies", {}).items():
        if data.get("first_seen", "").startswith(today):
            count += 1
    return count


def insolvencies_count_today():
    store = load_store()
    from datetime import date
    today = date.today().isoformat()
    count = 0
    for num, data in store.get("new_companies", {}).items():
        if data.get("status") == "insolvency" and data.get("first_seen", "").startswith(today):
            count += 1
    return count


def filings_count_today():
    store = load_store()
    from datetime import date
    today = date.today().isoformat()
    count = 0
    for key, data in store.get("known_filings", {}).items():
        if data.get("first_seen", "").startswith(today):
            count += 1
    return count
