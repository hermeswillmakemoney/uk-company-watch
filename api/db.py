"""
UK Company Watch — JSON file database layer.
Reads/writes data to a JSON file in the GitHub repo via the GitHub API.
"""

import json
import os
import urllib.request
import urllib.parse
import base64
from datetime import datetime, timedelta

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "hermeswillmakemoney/uk-company-watch")
DATA_FILE = "data/db.json"
GITHUB_API = "https://api.github.com"


def github_request(path, method="GET", data=None):
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    if data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method=method)
    else:
        req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"GitHub API error: {e}")
        return None


def load_db():
    result = github_request(f"/repos/{GITHUB_REPO}/contents/{DATA_FILE}")
    if result and "content" in result:
        content = base64.b64decode(result["content"]).decode()
        return json.loads(content), result.get("sha", "")
    return {
        "subscribers": {},
        "watched_companies": {},
        "known_companies": {},
        "watchlists": {},
        "watchlist_subscribers": {},
        "filing_summaries": [],
        "known_filings": [],
    }, ""


def save_db(db, sha):
    content = json.dumps(db, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    data = {
        "message": f"UCW data update {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "content": encoded,
    }
    if sha:
        data["sha"] = sha
    result = github_request(f"/repos/{GITHUB_REPO}/contents/{DATA_FILE}", method="PUT", data=data)
    return result is not None


PLAN_LIMITS = {
    "free": {"max_watched": 1, "max_alerts_per_week": 3},
    "pro": {"max_watched": 10, "max_alerts_per_week": 50},
    "business": {"max_watched": 999, "max_alerts_per_week": 9999},
}

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
        ("04006623", "ASOS PLC"), ("06968588", "THG PLC"),
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


def get_subscriber(db, chat_id):
    chat_id = str(chat_id)
    if chat_id not in db["subscribers"]:
        db["subscribers"][chat_id] = {
            "plan": "free",
            "joined_at": datetime.utcnow().isoformat(),
            "max_watched": 1,
            "max_alerts_per_week": 3,
            "stripe_subscription_id": None,
            "stripe_customer_id": None,
            "subscription_status": None,
            "alerts_sent_this_week": 0,
            "alert_week_start": "",
            "limit_warning_sent_date": "",
        }
    return db["subscribers"][chat_id]


def get_total_watch_count(db, chat_id):
    chat_id = str(chat_id)
    individual = len(db["watched_companies"].get(chat_id, []))
    wl_count = sum(1 for wl_code, members in db["watchlist_subscribers"].items() if chat_id in members)
    return individual + wl_count


def get_user_watched_companies(db, chat_id):
    chat_id = str(chat_id)
    companies = set(db["watched_companies"].get(chat_id, []))
    for wl_code, members in db["watchlist_subscribers"].items():
        if chat_id in members and wl_code in db["watchlists"]:
            for entry in db["watchlists"][wl_code].get("companies", []):
                companies.add(entry["number"])
    return companies


def init_watchlists(db):
    for code, wl in WATCHLISTS.items():
        if code not in db["watchlists"]:
            db["watchlists"][code] = {
                "name": wl["name"],
                "icon": wl["icon"],
                "companies": [{"number": n, "name": name} for n, name in wl["companies"]],
            }
