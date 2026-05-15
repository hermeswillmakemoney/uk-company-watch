#!/usr/bin/env python3
"""
UK Company Watch — Stripe Webhook Handler.
Deployed as Vercel serverless function at /api/stripe-webhook.

Handles:
- checkout.session.completed → upgrade user to paid plan
- customer.subscription.deleted → downgrade user to free
- invoice.payment_failed → flag account (optional)
"""

import json
import os
import sqlite3
import hashlib
import hmac
from pathlib import Path

# Stripe secret for webhook verification
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# Plan mapping: Stripe price ID → (plan_name, max_watched, max_alerts_per_day)
PLAN_MAP = {
    # These will be filled in with actual Stripe price IDs
    "price_xxx_pro": ("pro", 10, 50),
    "price_xxx_business": ("business", 999, 9999),
}

DB_PATH = Path("/tmp/uk_company_watch.db")  # Vercel is read-only except /tmp


def get_db():
    """Get SQLite connection. On Vercel, we use /tmp. Locally, use local path."""
    db_path = DB_PATH if DB_PATH.exists() else Path("data/uk_company_watch.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            plan TEXT DEFAULT 'free',
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            subscription_status TEXT,
            current_period_end TEXT,
            max_watched INTEGER DEFAULT 1,
            max_alerts_per_day INTEGER DEFAULT 3
        )
    """)
    conn.commit()
    return conn


def verify_stripe_signature(payload, sig_header):
    """Verify Stripe webhook signature."""
    if not STRIPE_WEBHOOK_SECRET:
        return True  # Skip verification in dev

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        return True
    except Exception:
        return False


def handle_checkout_completed(session):
    """User completed checkout → upgrade their plan."""
    conn = get_db()
    c = conn.cursor()

    # Get chat_id from session metadata
    chat_id = session.get("metadata", {}).get("chat_id", "")
    customer_id = session.get("customer", "")
    subscription_id = session.get("subscription", "")

    if not chat_id:
        print("No chat_id in session metadata")
        return

    # Determine plan from the line items
    # For subscriptions, we need to fetch the subscription to get the price
    plan_name = "pro"  # default
    max_watched = 10
    max_alerts = 50

    if subscription_id and STRIPE_SECRET_KEY:
        try:
            import stripe
            stripe.api_key = STRIPE_SECRET_KEY
            sub = stripe.Subscription.retrieve(subscription_id)
            price_id = sub["items"]["data"][0]["price"]["id"]
            if price_id in PLAN_MAP:
                plan_name, max_watched, max_alerts = PLAN_MAP[price_id]
        except Exception as e:
            print(f"Error fetching subscription: {e}")

    # Update subscriber
    c.execute("""
        UPDATE subscribers SET
            plan = ?,
            stripe_customer_id = ?,
            stripe_subscription_id = ?,
            subscription_status = 'active',
            max_watched = ?,
            max_alerts_per_day = ?
        WHERE chat_id = ?
    """, (plan_name, customer_id, subscription_id, max_watched, max_alerts, chat_id))
    conn.commit()
    conn.close()

    print(f"Upgraded {chat_id} to {plan_name}")


def handle_subscription_deleted(subscription):
    """User cancelled → downgrade to free."""
    conn = get_db()
    c = conn.cursor()

    subscription_id = subscription.get("id", "")
    customer_id = subscription.get("customer", "")

    # Find subscriber by subscription or customer ID
    c.execute("""
        UPDATE subscribers SET
            plan = 'free',
            subscription_status = 'cancelled',
            max_watched = 1,
            max_alerts_per_day = 3
        WHERE stripe_subscription_id = ? OR stripe_customer_id = ?
    """, (subscription_id, customer_id))

    if c.rowcount > 0:
        conn.commit()
        print(f"Downgraded subscription {subscription_id} to free")

    conn.close()


def handle_payment_failed(invoice):
    """Payment failed — could notify user, but don't downgrade yet."""
    customer_id = invoice.get("customer", "")
    print(f"Payment failed for customer {customer_id}")
    # Don't downgrade — Stripe will retry. Only downgrade on subscription.deleted.


def handler(request):
    """Vercel serverless function entry point."""
    if request.method != "POST":
        return {"statusCode": 405, "body": "Method not allowed"}

    body = request.body if hasattr(request, 'body') else request.get("body", "")
    sig_header = request.headers.get("stripe-signature", "") if hasattr(request, 'headers') else request.get("headers", {}).get("stripe-signature", "")

    # Parse the event
    try:
        if isinstance(body, str):
            event = json.loads(body)
        else:
            event = json.loads(body.decode() if isinstance(body, bytes) else body)
    except Exception:
        return {"statusCode": 400, "body": "Invalid JSON"}

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    print(f"Stripe webhook: {event_type}")

    if event_type == "checkout.session.completed":
        handle_checkout_completed(data)
    elif event_type == "customer.subscription.deleted":
        handle_subscription_deleted(data)
    elif event_type == "invoice.payment_failed":
        handle_payment_failed(data)

    return {"statusCode": 200, "body": "ok"}
