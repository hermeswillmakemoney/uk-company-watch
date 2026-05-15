#!/usr/bin/env python3
"""Set the Telegram bot webhook URL."""
import os
import sys
import urllib.request
import urllib.parse
import json

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

if not TELEGRAM_BOT_TOKEN:
    print("ERROR: Set TELEGRAM_BOT_TOKEN env var")
    sys.exit(1)

if not WEBHOOK_URL:
    print("ERROR: Set WEBHOOK_URL env var (e.g. https://your-domain.com/api/webhook)")
    sys.exit(1)

api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
data = urllib.parse.urlencode({"url": WEBHOOK_URL}).encode()

try:
    req = urllib.request.urlopen(urllib.request.Request(f"{api}/setWebhook", data=data), timeout=15)
    result = json.loads(req.read().decode())
    print(json.dumps(result, indent=2))
    if result.get("ok"):
        print(f"\n✅ Webhook set to: {WEBHOOK_URL}")
    else:
        print(f"\n❌ Failed: {result}")
except Exception as e:
    print(f"Error: {e}")
