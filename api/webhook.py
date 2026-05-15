#!/usr/bin/env python3
"""
UK Company Watch — Telegram Webhook Handler.
Vercel serverless function at /api/webhook.

Vercel Python runtime: the file must expose a WSGI-compatible `app` callable
or a `handler` that takes (event, context).
We use the `handler` approach with raw dict input.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def handler(event, context=None):
    """Vercel serverless function.

    For Python, Vercel passes:
      event: dict with 'method', 'body', 'headers', 'path', 'queryStringParameters'
    Must return: dict with 'statusCode', 'headers' (optional), 'body'
    """
    from api import store
    from api.telegram_bot import send_telegram, process_message

    method = event.get("method", "POST")
    body_str = event.get("body", "") or ""

    if method == "GET":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": "UK Company Watch webhook is running",
        }

    try:
        body = json.loads(body_str) if body_str else {}
    except Exception:
        return {"statusCode": 400, "body": "Invalid JSON"}

    if "message" in body:
        msg = body["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        if text:
            response = process_message(chat_id, text, store)
            if response:
                send_telegram(chat_id, response)

    return {"statusCode": 200, "body": "ok"}
