"""
Payment processing via the node's assigned payment ServiceObject.

The billing endpoints resolve the payment processor for a tenant (Stripe / PayPal)
from the ServiceObject assigned to the tenant's node through a configuration
profile (``services.tenant_payment_service``), then call the matching adapter
here. Adapters exchange the entered card for the processor's own tokens; only
PCI-safe fields (brand, last4, expiry) and the opaque processor references are
returned for storage. The full PAN / CVC is never persisted.

When a processor has no live secret configured (e.g. a staging node), the adapter
runs in deterministic TEST mode so the billing flow is exercisable end-to-end
without a live gateway — swap in the real secret and the same call goes live.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("cv.payments")


class PaymentError(Exception):
    """A card/processor error safe to surface to the customer."""


# Card brand detection by IIN/prefix — enough to label the stored method.
_BRAND_RULES = [
    ("amex", re.compile(r"^3[47]")),
    ("diners", re.compile(r"^3(?:0[0-5]|[68])")),
    ("discover", re.compile(r"^6(?:011|5|4[4-9])")),
    ("jcb", re.compile(r"^35")),
    ("mastercard", re.compile(r"^(5[1-5]|2[2-7])")),
    ("visa", re.compile(r"^4")),
]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def brand_for(number: str) -> str:
    d = _digits(number)
    for name, rule in _BRAND_RULES:
        if rule.match(d):
            return name
    return "card"


def _luhn_ok(number: str) -> bool:
    d = _digits(number)
    if not (12 <= len(d) <= 19):
        return False
    total, alt = 0, False
    for ch in reversed(d):
        n = ord(ch) - 48
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def validate_card(card: dict) -> dict:
    """Validate the entered card and return normalized {number, brand, last4,
    exp_month, exp_year, cvc, holder_name}. Raises PaymentError on bad input."""
    number = _digits(card.get("number"))
    if not _luhn_ok(number):
        raise PaymentError("That card number doesn't look valid.")
    try:
        exp_month = int(card.get("exp_month") or 0)
        exp_year = int(card.get("exp_year") or 0)
    except (TypeError, ValueError):
        raise PaymentError("Enter a valid expiry date.")
    if exp_year < 100:
        exp_year += 2000
    if not (1 <= exp_month <= 12):
        raise PaymentError("Enter a valid expiry month.")
    now = datetime.now(timezone.utc)
    if (exp_year, exp_month) < (now.year, now.month):
        raise PaymentError("That card has expired.")
    cvc = _digits(card.get("cvc"))
    if not (3 <= len(cvc) <= 4):
        raise PaymentError("Enter the card's security code.")
    return {
        "number": number, "brand": brand_for(number), "last4": number[-4:],
        "exp_month": exp_month, "exp_year": exp_year, "cvc": cvc,
        "holder_name": (card.get("holder_name") or "").strip(),
    }


def add_card(service: dict | None, card: dict) -> dict:
    """Tokenize + vault an entered card with the assigned processor. Returns the
    PCI-safe stored fields + processor references. ``service`` is the resolved
    payment ServiceObject ({"kind","name","config"}) or None (→ test mode)."""
    v = validate_card(card)
    kind = (service or {}).get("kind", "")
    cfg = (service or {}).get("config", {}) or {}
    if kind == "payment-stripe" and cfg.get("secret_key"):
        return _stripe_add_card(cfg, v)
    if kind == "payment-paypal" and cfg.get("client_id") and cfg.get("client_secret"):
        return _paypal_add_card(cfg, v)
    return _test_add_card(kind or "test", v)


def _base_result(processor: str, v: dict, customer: str, token: str) -> dict:
    return {
        "processor": processor, "type": "card",
        "brand": v["brand"], "last4": v["last4"],
        "exp_month": v["exp_month"], "exp_year": v["exp_year"],
        "holder_name": v["holder_name"],
        "processor_customer": customer, "processor_token": token,
    }


def _test_add_card(processor: str, v: dict) -> dict:
    """Deterministic offline vault: no PAN leaves the process; a synthetic token
    stands in for the processor reference so the flow is fully exercisable."""
    token = "test_pm_" + v["last4"] + f"{v['exp_month']:02d}{v['exp_year'] % 100:02d}"
    return _base_result(processor.replace("payment-", "") or "test", v,
                        customer="test_cus_local", token=token)


def _stripe_add_card(cfg: dict, v: dict) -> dict:
    """Create a Stripe PaymentMethod for the card and attach it to a (new)
    customer, returning the customer + payment-method ids. Server-side card entry
    requires the account's raw-card API access; production should tokenize with
    Stripe.js and pass the resulting token instead of the PAN."""
    secret = cfg["secret_key"]
    auth = (secret, "")
    base = "https://api.stripe.com/v1"
    try:
        with httpx.Client(timeout=20) as client:
            pm = client.post(f"{base}/payment_methods", auth=auth, data={
                "type": "card",
                "card[number]": v["number"], "card[exp_month]": v["exp_month"],
                "card[exp_year]": v["exp_year"], "card[cvc]": v["cvc"],
                "billing_details[name]": v["holder_name"],
            })
            if pm.status_code >= 400:
                raise PaymentError(_stripe_error(pm))
            pm_id = pm.json()["id"]
            cust = client.post(f"{base}/customers", auth=auth,
                               data={"name": v["holder_name"]})
            if cust.status_code >= 400:
                raise PaymentError(_stripe_error(cust))
            cust_id = cust.json()["id"]
            att = client.post(f"{base}/payment_methods/{pm_id}/attach", auth=auth,
                              data={"customer": cust_id})
            if att.status_code >= 400:
                raise PaymentError(_stripe_error(att))
            card = att.json().get("card", {})
    except PaymentError:
        raise
    except Exception as exc:  # noqa: BLE001 — network/gateway
        logger.warning("stripe add_card failed: %s", exc)
        raise PaymentError("Could not reach the payment processor. Try again.")
    v["brand"] = card.get("brand") or v["brand"]
    v["last4"] = card.get("last4") or v["last4"]
    return _base_result("stripe", v, customer=cust_id, token=pm_id)


def _stripe_error(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message") or "Card was declined."
    except Exception:  # noqa: BLE001
        return "Card was declined."


def _paypal_add_card(cfg: dict, v: dict) -> dict:
    """Vault a card as a PayPal payment token (Advanced Card Payments)."""
    env = (cfg.get("environment") or "live").lower()
    host = "https://api-m.paypal.com" if env == "live" else "https://api-m.sandbox.paypal.com"
    try:
        with httpx.Client(timeout=20) as client:
            tok = client.post(f"{host}/v1/oauth2/token",
                              auth=(cfg["client_id"], cfg["client_secret"]),
                              data={"grant_type": "client_credentials"})
            if tok.status_code >= 400:
                raise PaymentError("PayPal authentication failed.")
            access = tok.json()["access_token"]
            res = client.post(f"{host}/v3/vault/payment-tokens",
                              headers={"Authorization": f"Bearer {access}"},
                              json={"payment_source": {"card": {
                                  "number": v["number"],
                                  "expiry": f"{v['exp_year']}-{v['exp_month']:02d}",
                                  "security_code": v["cvc"], "name": v["holder_name"]}}})
            if res.status_code >= 400:
                raise PaymentError("PayPal could not vault that card.")
            body = res.json()
    except PaymentError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("paypal add_card failed: %s", exc)
        raise PaymentError("Could not reach the payment processor. Try again.")
    token = body.get("id", "")
    src = (body.get("payment_source") or {}).get("card", {})
    v["brand"] = (src.get("brand") or v["brand"]).lower()
    v["last4"] = src.get("last_digits") or v["last4"]
    return _base_result("paypal", v, customer=body.get("customer", {}).get("id", ""), token=token)


# --------------------------------------------------------------------------- #
# Recurring subscriptions + one-off charges                                     #
# --------------------------------------------------------------------------- #

def _processor_name(service: dict | None) -> str:
    return ((service or {}).get("kind", "").replace("payment-", "")) or "test"


def start_subscription(service: dict | None, *, customer: str, token: str,
                       amount_cents: int, currency: str, interval: str,
                       plan_name: str) -> dict:
    """Create a recurring subscription at the processor for ``amount_cents`` per
    ``interval``. Returns {subscription_id, status, current_period_end(ISO|None)}.
    Falls back to a deterministic test subscription when no live secret."""
    kind = (service or {}).get("kind", "")
    cfg = (service or {}).get("config", {}) or {}
    if kind == "payment-stripe" and cfg.get("secret_key"):
        return _stripe_start_subscription(cfg, customer, token, amount_cents,
                                          currency, interval, plan_name)
    # Test / unconfigured: synthesize an active subscription.
    end = datetime.now(timezone.utc) + (timedelta(days=365) if interval == "year" else timedelta(days=30))
    return {"subscription_id": f"test_sub_{customer or 'local'}", "status": "active",
            "current_period_end": end.isoformat()}


def _stripe_start_subscription(cfg: dict, customer: str, token: str, amount_cents: int,
                               currency: str, interval: str, plan_name: str) -> dict:
    secret = cfg["secret_key"]
    auth = (secret, "")
    base = "https://api.stripe.com/v1"
    data = {
        "customer": customer,
        "items[0][price_data][currency]": currency.lower(),
        "items[0][price_data][product_data][name]": plan_name or "Arkive protection",
        "items[0][price_data][unit_amount]": int(amount_cents),
        "items[0][price_data][recurring][interval]": "year" if interval == "year" else "month",
        "payment_behavior": "allow_incomplete",
    }
    if token:
        data["default_payment_method"] = token
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(f"{base}/subscriptions", auth=auth, data=data)
            if r.status_code >= 400:
                raise PaymentError(_stripe_error(r))
            body = r.json()
    except PaymentError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("stripe start_subscription failed: %s", exc)
        raise PaymentError("Could not reach the payment processor. Try again.")
    cpe = body.get("current_period_end")
    return {"subscription_id": body.get("id", ""), "status": body.get("status", "active"),
            "current_period_end": (datetime.fromtimestamp(cpe, timezone.utc).isoformat()
                                   if cpe else None)}


def set_subscription_paused(service: dict | None, subscription_id: str, paused: bool) -> dict:
    """Pause or resume collection on a subscription (admin disable/enable)."""
    kind = (service or {}).get("kind", "")
    cfg = (service or {}).get("config", {}) or {}
    if kind == "payment-stripe" and cfg.get("secret_key") and subscription_id.startswith("sub_"):
        auth = (cfg["secret_key"], "")
        data = ({"pause_collection[behavior]": "void"} if paused else {"pause_collection": ""})
        try:
            with httpx.Client(timeout=20) as client:
                r = client.post(f"https://api.stripe.com/v1/subscriptions/{subscription_id}",
                                auth=auth, data=data)
                if r.status_code >= 400:
                    raise PaymentError(_stripe_error(r))
                body = r.json()
        except PaymentError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("stripe pause failed: %s", exc)
            raise PaymentError("Could not reach the payment processor.")
        return {"status": "paused" if body.get("pause_collection") else body.get("status", "active")}
    return {"status": "paused" if paused else "active"}


def cancel_subscription(service: dict | None, subscription_id: str) -> dict:
    kind = (service or {}).get("kind", "")
    cfg = (service or {}).get("config", {}) or {}
    if kind == "payment-stripe" and cfg.get("secret_key") and subscription_id.startswith("sub_"):
        try:
            with httpx.Client(timeout=20) as client:
                r = client.delete(f"https://api.stripe.com/v1/subscriptions/{subscription_id}",
                                  auth=(cfg["secret_key"], ""))
                if r.status_code >= 400 and r.status_code != 404:
                    raise PaymentError(_stripe_error(r))
        except PaymentError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("stripe cancel failed: %s", exc)
    return {"status": "canceled"}


def charge_once(service: dict | None, *, customer: str, token: str, amount_cents: int,
                currency: str, description: str) -> dict:
    """Charge the saved method now (off-session). Returns {charge_id, status
    ('succeeded'|'failed'), error}."""
    kind = (service or {}).get("kind", "")
    cfg = (service or {}).get("config", {}) or {}
    if kind == "payment-stripe" and cfg.get("secret_key"):
        auth = (cfg["secret_key"], "")
        data = {"amount": int(amount_cents), "currency": currency.lower(),
                "customer": customer, "payment_method": token,
                "off_session": "true", "confirm": "true",
                "description": description or "Arkive charge"}
        try:
            with httpx.Client(timeout=25) as client:
                r = client.post("https://api.stripe.com/v1/payment_intents", auth=auth, data=data)
                body = r.json()
            if r.status_code >= 400:
                return {"charge_id": (body.get("error", {}) or {}).get("payment_intent", {}).get("id", ""),
                        "status": "failed", "error": _stripe_error(r)}
            return {"charge_id": body.get("id", ""),
                    "status": "succeeded" if body.get("status") == "succeeded" else "failed",
                    "error": "" if body.get("status") == "succeeded" else f"status: {body.get('status')}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("stripe charge_once failed: %s", exc)
            return {"charge_id": "", "status": "failed", "error": "processor unreachable"}
    # Test / unconfigured: succeed deterministically.
    return {"charge_id": f"test_ch_{int(datetime.now(timezone.utc).timestamp())}",
            "status": "succeeded", "error": ""}

