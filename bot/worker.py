#!/usr/bin/env python3
"""
UK Company Watch — Kanban Filing Summary Worker.
Processes pending task files in data/summaries/, writes .summary files.
Designed to be called by the kanban system or run directly.

Usage:
  python3 worker.py              — process all pending tasks
  python3 worker.py <task_file>  — process specific task
"""

import json
import sys
import os
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = Path(__file__).parent / "data" / "summaries"
DATA_DIR.mkdir(parents=True, exist_ok=True)

KANBAN_SYSTEM_PROMPT = """You are a filing analyst for UK Company Watch. Your job: take a Companies House filing and write a concise, human-readable summary that tells a business professional why this filing matters.

RULES:
- One paragraph, 2-4 sentences max
- Explain what the filing means in plain English — not just the technical type
- Flag anything concerning: director resignations, overdue accounts, insolvency-related filings, large charges
- For routine filings (annual returns, confirmation statements), keep it brief and reassuring
- If the filing type is obscure, explain what it means
- Use a professional but conversational tone
- No bullet points, no headers — just a clean paragraph

FILING TYPE CONTEXT:
- AA01 / AR01: Annual return — routine yearly filing confirming company details
- CH01: Director appointed; CH02: Director resigned (flag as potentially significant); CH03: Director details changed
- CS01: Confirmation statement — routine, but overdue ones are a red flag
- AD01: Registered address changed — usually routine
- SH01: New shares allotted — could indicate fundraising
- MG01: Mortgage or charge — company took on secured debt
- LIQ01 / WUO1: Liquidation / winding up — critical, company is closing
- GAZ1 / GAZ2: Gazette notices — often relate to strikes-off or insolvency
- DS01: Dissolution application — company is being dissolved
- TM01: Termination of appointment — director/officer left
- AP01: Appointment of director — new director joined
- MR01/MR04: Mortgage-related filings
- PSC01-PSC9: Persons with Significant Control changes
- RESOLUTIONS: Company passed a special resolution — could be anything from name change to winding up
- CERTNM / CERT10: Certificate of incorporation or name change
- NEWINC: New company incorporated
- MA: Merger or acquisition activity
- RP01: Return of purchase of own shares

OUTPUT: Just the summary paragraph, nothing else."""


def call_llm(system_prompt, user_prompt):
    """Try multiple LLM backends."""
    # Try Ollama/local first
    gateway_url = os.environ.get("OLLAMA_GATEWAY_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")

    try:
        data = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            f"{gateway_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            content = result.get("message", {}).get("content", "")
            if content:
                return content
    except Exception:
        pass

    # Try OpenAI
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        try:
            data = json.dumps({
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 200,
                "temperature": 0.5,
            }).encode()

            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                return result["choices"][0]["message"]["content"]
        except Exception:
            pass

    return None


def generate_fallback_summary(company_name, company_number, filing_date, filing_type, description):
    """Generate a basic summary without LLM."""
    type_map = {
        "AA01": "Annual return filed", "AR01": "Annual return filed",
        "CH01": "New director appointed", "CH02": "Director resigned ⚠️",
        "CH03": "Director details changed", "CS01": "Confirmation statement filed",
        "AD01": "Registered address changed", "SH01": "New shares allotted",
        "MG01": "Mortgage or charge registered ⚠️", "LIQ01": "Liquidation proceedings ⚠️",
        "WUO1": "Winding up order ⚠️", "DS01": "Dissolution application filed ⚠️",
        "GAZ1": "Gazette notice published", "GAZ2": "Gazette notice published",
        "TM01": "Director/officer appointment terminated", "AP01": "New director appointed",
        "NEWINC": "Company incorporated", "CERTNM": "Company name changed",
        "CERT10": "Certificate of incorporation", "MA": "Merger/acquisition activity",
        "RESOLUTIONS": "Special resolution passed", "MR01": "Mortgage registered",
        "MR04": "Mortgage satisfied", "PSC01": "PSC information updated",
        "PSC02": "PSC information updated", "PSC04": "PSC information updated",
        "PSC05": "PSC information updated", "PSC07": "PSC information updated",
        "PSC08": "PSC information updated", "PSC09": "PSC information updated",
        "RP01": "Return of purchase of own shares", "SH06": "Allotment of shares",
        "AAMD": "Accounts amended", "AM10": "Amended accounts filed",
        "AM19": "Amended accounts filed", "AM23": "Amended accounts filed",
        "LLAA01": "Limited liability partnership annual return",
        "LLIN01": "LLP incorporated", "LLTM01": "LLP termination",
        "LP6": "Limited partnership filing", "SLPCS01": "Scottish LP filing",
    }

    base = type_map.get(filing_type, f"Filing: {filing_type}")
    flag = " ⚠️" if any(t in filing_type for t in ["CH02", "LIQ", "WUO", "DS01", "MG01", "GAZ"]) else ""
    desc = f" — {description}" if description and description != "No description available" else ""

    return f"{company_name} ({company_number}): {base} on {filing_date}.{flag}{desc}"


def process_task(task_file):
    """Process a single filing summary task."""
    with open(task_file) as f:
        task = json.load(f)

    company_number = task["company_number"]
    company_name = task["company_name"]
    filing_date = task["filing_date"]
    filing_type = task["filing_type"]
    description = task.get("description", "")

    user_prompt = f"Company: {company_name} (UK company #{company_number})\nFiling date: {filing_date}\nFiling type: {filing_type}\nDescription: {description or 'No description'}\n\nWrite a concise summary of what this filing means."

    # Try LLM
    summary = call_llm(KANBAN_SYSTEM_PROMPT, user_prompt)

    # Fallback
    if not summary:
        summary = generate_fallback_summary(company_name, company_number, filing_date, filing_type, description)

    # Write summary
    summary_file = Path(task_file).with_suffix(".summary")
    summary_file.write_text(summary)
    print(f"  ✓ {company_name} ({company_number}) — {filing_type}")
    return summary


def main():
    if len(sys.argv) > 1:
        task_file = sys.argv[1]
        process_task(task_file)
    else:
        tasks = sorted(DATA_DIR.glob("task_*.json"))
        if not tasks:
            print("No pending tasks.")
            return

        print(f"Processing {len(tasks)} tasks...")
        done = 0
        for task_file in tasks:
            try:
                process_task(task_file)
                done += 1
            except Exception as e:
                print(f"  ✗ Error: {task_file.name}: {e}")
        print(f"Done: {done}/{len(tasks)} tasks processed.")


if __name__ == "__main__":
    main()
