#!/usr/bin/env python3
"""
UK Company Watch — Filing Summary Kanban Worker.
Reads a task file, uses the LLM to write a human-readable summary,
and writes the result to a .summary file.

This is designed to be called by the kanban system or directly.
Usage: python3 summarize_filing.py <task_file.json>
"""

import json
import sys
import os
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def call_hermes_llm(system_prompt, user_prompt):
    """Call the Hermes LLM gateway to generate a summary."""
    # Try the Hermes gateway
    gateway_url = os.environ.get("HERMES_GATEWAY_URL", "http://localhost:11434")
    
    # Try Ollama-style API first (local)
    try:
        data = json.dumps({
            "model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
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
            return result.get("message", {}).get("content", "")
    except Exception:
        pass

    # Try OpenAI-compatible API
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
    # Map filing types to human-readable descriptions
    type_map = {
        "AA01": "Annual return filed",
        "AR01": "Annual return filed",
        "CH01": "Director appointed",
        "CH02": "Director resigned",
        "CH03": "Director's details changed",
        "CS01": "Confirmation statement filed",
        "AD01": "Registered address changed",
        "SH01": "New shares allotted",
        "MG01": "Mortgage or charge registered",
        "LIQ01": "Liquidation proceedings",
        "WUO1": "Winding up order",
        "NEWINC": "Company incorporated",
    }

    base = type_map.get(filing_type, f"Filing type: {filing_type}")

    # Flag concerning filings
    concerning = ["CH02", "LIQ01", "WUO1", "AD01"]
    flag = " ⚠️" if filing_type in concerning else ""

    return f"{company_name} ({company_number}): {base} on {filing_date}.{flag} {description or ''}"


def process_task(task_file):
    """Process a single filing summary task."""
    with open(task_file) as f:
        task = json.load(f)

    company_number = task["company_number"]
    company_name = task["company_name"]
    filing_date = task["filing_date"]
    filing_type = task["filing_type"]
    description = task.get("description", "")
    system_prompt = task.get("system_prompt", "")
    user_prompt = task.get("user_prompt", "")

    # Try LLM first
    summary = None
    if system_prompt and user_prompt:
        summary = call_hermes_llm(system_prompt, user_prompt)

    # Fallback
    if not summary:
        summary = generate_fallback_summary(company_name, company_number, filing_date, filing_type, description)

    # Write summary
    summary_file = Path(task_file).with_suffix(".summary")
    summary_file.write_text(summary)

    print(f"  Summary written for {company_name} ({company_number}) — {filing_type}")
    return summary


if __name__ == "__main__":
    import urllib.request

    if len(sys.argv) < 2:
        # Process all pending task files
        data_dir = Path(__file__).parent / "data" / "summaries"
        tasks = list(data_dir.glob("task_*.json"))
        if not tasks:
            print("No pending tasks.")
            sys.exit(0)

        print(f"Processing {len(tasks)} tasks...")
        for task_file in tasks:
            try:
                process_task(str(task_file))
            except Exception as e:
                print(f"  Error processing {task_file}: {e}")
        print("Done.")
    else:
        # Process specific task
        task_file = sys.argv[1]
        process_task(task_file)
