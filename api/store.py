#!/usr/bin/env python3
"""
UK Company Watch — Data store backed by GitHub JSON file.
Reads/writes store.json via the GitHub API for persistence across serverless invocations.
"""

import json
import os
import base64
import urllib.request
import urllib.error
from pathlib import Path
from copy import deepcopy
from datetime import datetime

# GitHub config
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "hermeswillmakemoney/uk-company-watch")
STORE_PATH = "data/store.json"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STORE_PATH}"

# In-memory cache
_store = None
_sha = None  # GitHub blob SHA for updates


def _github_request(method, url, data=None, headers=None):
    """Make a GitHub API request."""
    if not GITHUB_TOKEN:
        print("WARNING: No GITHUB_TOKEN set, data will not persist")
        return None

    req_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"GitHub API error: {e.code} {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"GitHub request error: {e}")
        return None


def load_store():
    """Load store from GitHub JSON file."""
    global _store, _sha

    if _store is not None:
        return _store

    # Try GitHub API first
    if GITHUB_TOKEN:
        resp = _github_request("GET", GITHUB_API)
        if resp and "content" in resp:
            try:
                content = base64.b64decode(resp["content"]).decode()
                _store = json.loads(content)
                _sha = resp.get("sha")
                return _store
            except Exception as e:
                print(f"Failed to decode store: {e}")

    # Fallback: try local file (for development)
    local_path = Path(__file__).parent.parent / "data" / "store.json"
    if local_path.exists():
        with open(local_path) as f:
            _store = json.load(f)
        return _store

    # Default empty store
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
    """Save store to GitHub JSON file."""
    global _store, _sha

    if store is not None:
        _store = store

    if not GITHUB_TOKEN:
        print("WARNING: No GITHUB_TOKEN, saving locally only")
        local_path = Path(__file__).parent.parent / "data" / "store.json"
        local_path.parent.mkdir(exist_ok=True)
        with open(local_path, "w") as f:
            json.dump(_store, f, indent=2, default=str)
        return

    content = json.dumps(_store, indent=2, default=str)
    content_b64 = base64.b64encode(content.encode()).decode()

    data = {
        "message": f"Update data store — {datetime.utcnow().isoformat()}",
        "content": content_b64,
    }
    if _sha:
        data["sha"] = _sha

    resp = _github_request("PUT", GITHUB_API, data=data)
    if resp and "content" in resp:
        _sha = resp["content"].get("sha")
        print("Store saved to GitHub")
    else:
        print("WARNING: Failed to save store to GitHub")


def get_subscribers():
    store = load_store()
    return store.get("subscribers", {})


def add_subscriber(chat_id, plan="free"):
    store = load_store()
    chat_id = str(chat_id)
    if chat_id not in store["subscribers"]:
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
    chat_id = str(chat_id)
    if chat_id in store["subscribers"]:
        store["subscribers"][chat_id].update(kwargs)
        save_store()


def get_watched_companies(chat_id=None):
    store = load_store()
    watched = store.get("watched_companies", {})
    if chat_id:
        chat_id = str(chat_id)
        return {k: v for k, v in watched.items() if v.get("chat_id") == chat_id}
    return watched


def add_watched_company(chat_id, company_number, company_name):
    store = load_store()
    chat_id = str(chat_id)
    key = f"{chat_id}:{company_number}"
    if key not in store["watched_companies"]:
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
    store["last_run"] = datetime.utcnow().isoformat()
    save_store()


def new_companies_count_today():
    store = load_store()
    from datetime import date
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
