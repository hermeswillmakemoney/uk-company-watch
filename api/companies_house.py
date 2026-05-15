#!/usr/bin/env python3
"""Companies House API client."""

import urllib.request
import urllib.parse
import json
import os

CH_API_BASE = "https://api.companieshouse.gov.uk"

# Optional API key — without one, rate limit is 60 requests per 5 minutes
CH_API_KEY = os.environ.get("CH_API_KEY", "")


def ch_fetch(path):
    """Fetch from Companies House API."""
    url = f"{CH_API_BASE}{path}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if CH_API_KEY:
            import base64
            credentials = base64.b64encode(f"{CH_API_KEY}:".encode()).decode()
            req.add_header("Authorization", f"Basic {credentials}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"CH API error for {path}: {e}")
        return None


def search_companies(query, items_per_page=5):
    """Search companies by name."""
    return ch_fetch(f"/search/companies?q={urllib.parse.quote(query)}&items_per_page={items_per_page}")


def get_company(company_number):
    """Get company profile."""
    return ch_fetch(f"/company/{company_number}")


def get_filing_history(company_number, items_per_page=5):
    """Get filing history for a company."""
    return ch_fetch(f"/company/{company_number}/filing-history?items_per_page={items_per_page}")


def get_officers(company_number, items_per_page=10):
    """Get company officers (directors)."""
    return ch_fetch(f"/company/{company_number}/officers?items_per_page={items_per_page}")


def search_insolvencies(items_per_page=30):
    """Search for companies in insolvency."""
    return ch_fetch(f"/search/companies?q=&company_status=insolvency&items_per_page={items_per_page}")


def advanced_search(incorporated_from, items_per_page=50):
    """Advanced search for companies incorporated from a date."""
    return ch_fetch(f"/advanced-search/companies?incorporated_from={incorporated_from}&items_per_page={items_per_page}")
