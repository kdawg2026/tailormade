"""Stripe billing: credit packs (one-time) and a monthly plan (subscription).

  python billing.py setup                  # create the products and prices (idempotent)
  python billing.py webhook https://HOST   # register the webhook endpoint; prints its secret

Credits are granted only from verified webhook events, never from the
checkout success page, so a closed tab or a forged redirect cannot add or
lose credits. Each event is processed once (stripe_events), and each pack
purchase is credited once (pack_purchases).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import stripe

from db import connect

# One Product per plan, as Stripe recommends, so receipts name what was bought.
PLANS = {
    "pack_15": {"name": "Tailormade 15-tailor pack", "amount": 1000, "credits": 15},
    "pack_45": {"name": "Tailormade 45-tailor pack", "amount": 2200, "credits": 45},
    "monthly": {"name": "Tailormade Monthly (60 tailors/month)", "amount": 900, "interval": "month"},
}
INTEGRATION_ID = "tailormade-checkout-qmvhzrta"
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    "charge.refunded",
    "charge.dispute.created",
    "radar.early_fraud_warning.created",
]


def _env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    env_file = Path(__file__).resolve().parent / ".env.local"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() == name:
                return val.strip().strip("\"'")
    raise RuntimeError(f"{name} is not set")


def client() -> stripe.StripeClient:
    return stripe.StripeClient(_env("STRIPE_SECRET_KEY"))


_price_ids: dict[str, str] = {}


def price_id(plan: str) -> str:
    if plan not in _price_ids:
        prices = client().v1.prices.list(params={"lookup_keys": [plan], "active": True})
        if not prices.data:
            raise RuntimeError(f"No Stripe price for {plan!r}; run `python billing.py setup`")
        _price_ids[plan] = prices.data[0].id
    return _price_ids[plan]


def setup() -> None:
    c = client()
    for key, plan in PLANS.items():
        if c.v1.prices.list(params={"lookup_keys": [key], "active": True}).data:
            print(f"{key}: exists")
            continue
        product = c.v1.products.create(params={"name": plan["name"]})
        params = {
            "product": product.id,
            "currency": "usd",
            "unit_amount": plan["amount"],
            "lookup_key": key,
        }
        if "interval" in plan:
            params["recurring"] = {"interval": plan["interval"]}
        c.v1.prices.create(params=params)
        print(f"{key}: created")


def register_webhook(base_url: str) -> None:
    endpoint = client().v1.webhook_endpoints.create(params={
        "url": base_url.rstrip("/") + "/api/stripe/webhook",
        "enabled_events": WEBHOOK_EVENTS,
    })
    print(f"Endpoint {endpoint.id} created. Signing secret (store as STRIPE_WEBHOOK_SECRET):")
    print(endpoint.secret)


# ---- checkout --------------------------------------------------------------

def _customer_id(user: dict) -> str:
    if user["stripe_customer_id"]:
        return user["stripe_customer_id"]
    customer = client().v1.customers.create(params={
        "email": user["email"], "metadata": {"user_id": str(user["id"])},
    })
    with connect() as conn:
        row = conn.execute(
            "UPDATE users SET stripe_customer_id = %s WHERE id = %s AND stripe_customer_id IS NULL "
            "RETURNING stripe_customer_id",
            (customer.id, user["id"]),
        ).fetchone()
        if row is None:  # a parallel request won the race; use its customer
            row = conn.execute(
                "SELECT stripe_customer_id FROM users WHERE id = %s", (user["id"],)
            ).fetchone()
    return row["stripe_customer_id"]


def checkout_url(user: dict, plan: str, base_url: str) -> str:
    if plan not in PLANS:
        raise ValueError("Unknown plan.")
    subscription = "interval" in PLANS[plan]
    if subscription and user["sub_status"] in ("active", "trialing", "past_due"):
        raise ValueError("You already have the monthly plan. Use Manage subscription to change it.")
    session = client().v1.checkout.sessions.create(params={
        "mode": "subscription" if subscription else "payment",
        "customer": _customer_id(user),
        "line_items": [{"price": price_id(plan), "quantity": 1}],
        "success_url": f"{base_url}/?checkout=success",
        "cancel_url": f"{base_url}/?checkout=cancel",
        "integration_identifier": INTEGRATION_ID,
    })
    return session.url


def portal_url(user: dict, base_url: str) -> str:
    if not user["stripe_customer_id"]:
        raise ValueError("No billing account yet.")
    return client().v1.billing_portal.sessions.create(params={
        "customer": user["stripe_customer_id"], "return_url": base_url + "/",
    }).url


def set_cancel_at_period_end(user: dict, cancel: bool) -> None:
    """Cancel at the end of the paid period (keeps this month's tailors), or undo that."""
    if not user["sub_id"] or user["sub_status"] in (None, "canceled", "incomplete_expired"):
        raise ValueError("You don't have a monthly plan.")
    sub = client().v1.subscriptions.update(
        user["sub_id"], params={"cancel_at_period_end": cancel}
    ).to_dict()
    with connect() as conn:  # the webhook will say the same; don't wait for it
        _sync_subscription(conn, sub)


def delete_customer(user: dict) -> None:
    """Delete the Stripe customer, which also ends any subscription immediately.

    Stripe keeps its own payment records, as card networks and tax law require.
    """
    if not user["stripe_customer_id"]:
        return
    try:
        client().v1.customers.delete(user["stripe_customer_id"])
    except stripe.InvalidRequestError as exc:
        if exc.code != "resource_missing":
            raise


# ---- webhooks --------------------------------------------------------------

def _period_end(sub: dict) -> datetime | None:
    # Newer API versions keep the billing period on each subscription item.
    end = sub.get("current_period_end")
    if end is None:
        items = (sub.get("items") or {}).get("data") or []
        end = items[0].get("current_period_end") if items else None
    return datetime.fromtimestamp(end, timezone.utc) if end else None


def _sync_subscription(conn, sub: dict) -> None:
    period_end = _period_end(sub)
    cancelling = bool(sub.get("cancel_at_period_end") or sub.get("cancel_at"))
    # A new billing period (renewal) restarts the monthly allowance.
    conn.execute(
        "UPDATE users SET sub_id = %s, sub_status = %s, sub_period_end = %s, "
        "sub_cancel_at_period_end = %s, "
        "sub_used = CASE WHEN sub_period_end IS DISTINCT FROM %s THEN 0 ELSE sub_used END "
        "WHERE stripe_customer_id = %s",
        (sub["id"], sub["status"], period_end, cancelling, period_end, sub["customer"]),
    )


def _sync_customer_subscription(conn, customer_id: str) -> None:
    subs = client().v1.subscriptions.list(
        params={"customer": customer_id, "status": "all", "limit": 1}
    )
    if subs.data:
        _sync_subscription(conn, subs.data[0].to_dict())


def _fulfill_checkout(conn, session: dict) -> None:
    if session.get("payment_status") == "unpaid":
        return  # an async payment method; async_payment_succeeded follows
    if session["mode"] == "subscription":
        _sync_customer_subscription(conn, session["customer"])
        return
    full = client().v1.checkout.sessions.retrieve(
        session["id"], params={"expand": ["line_items.data.price"]}
    ).to_dict()
    credits = sum(
        PLANS[item["price"]["lookup_key"]]["credits"] * item["quantity"]
        for item in full["line_items"]["data"]
        if item["price"].get("lookup_key") in PLANS and "credits" in PLANS[item["price"]["lookup_key"]]
    )
    user = conn.execute(
        "SELECT id FROM users WHERE stripe_customer_id = %s", (session["customer"],)
    ).fetchone()
    if not credits or user is None:
        return
    inserted = conn.execute(
        "INSERT INTO pack_purchases (checkout_session_id, user_id, payment_intent_id, credits) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (session["id"], user["id"], session.get("payment_intent"), credits),
    ).rowcount
    if inserted:
        conn.execute(
            "UPDATE users SET pack_credits = pack_credits + %s WHERE id = %s",
            (credits, user["id"]),
        )


def _hold_customer(conn, customer_id: str | None) -> None:
    if customer_id:
        conn.execute(
            "UPDATE users SET billing_hold = true WHERE stripe_customer_id = %s", (customer_id,)
        )


def handle_webhook(payload: bytes, signature: str | None) -> None:
    """Verify and apply one Stripe event. Raises ValueError on a bad signature."""
    try:
        event = client().construct_event(payload, signature, _env("STRIPE_WEBHOOK_SECRET"))
    except stripe.SignatureVerificationError as exc:
        raise ValueError("Invalid Stripe signature") from exc
    obj = event.data.object.to_dict()
    with connect() as conn:
        first_time = conn.execute(
            "INSERT INTO stripe_events (id) VALUES (%s) ON CONFLICT DO NOTHING", (event.id,)
        ).rowcount
        if not first_time:
            return
        kind = event.type
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            _fulfill_checkout(conn, obj)
        elif kind.startswith("customer.subscription."):
            _sync_subscription(conn, obj)
        elif kind in ("invoice.paid", "invoice.payment_failed"):
            if obj.get("customer"):
                _sync_customer_subscription(conn, obj["customer"])
        elif kind == "charge.refunded":
            # Take back the credits of a refunded pack (never below zero).
            purchase = conn.execute(
                "UPDATE pack_purchases SET refunded = true "
                "WHERE payment_intent_id = %s AND NOT refunded RETURNING user_id, credits",
                (obj.get("payment_intent"),),
            ).fetchone()
            if purchase:
                conn.execute(
                    "UPDATE users SET pack_credits = greatest(pack_credits - %s, 0) WHERE id = %s",
                    (purchase["credits"], purchase["user_id"]),
                )
        elif kind == "charge.dispute.created":
            charge = client().v1.charges.retrieve(obj["charge"])
            _hold_customer(conn, charge.customer)
        elif kind == "radar.early_fraud_warning.created":
            charge = client().v1.charges.retrieve(obj["charge"])
            _hold_customer(conn, charge.customer)
        # checkout.session.async_payment_failed: nothing was granted, nothing to undo.


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif len(sys.argv) == 3 and sys.argv[1] == "webhook":
        register_webhook(sys.argv[2])
    else:
        sys.exit("usage: python billing.py setup | webhook https://HOST")
