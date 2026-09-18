"""Agent Arena: SmartCart — Your Autonomous Shopping Agent.

Implement your agent logic in the `solve` function below.

Available tools (via `tools` ToolsClient):
    Read tools:
        tools.get_user_profile(user_id)                        -> user preferences, budget, bank cards
        tools.get_saved_products(user_id)                      -> list of product IDs user is watching
        tools.get_product(product_id)                          -> product details, current price, MRP
        tools.get_price_history(product_id, days=365)          -> historical price entries + evidence_id
        tools.get_available_offers(product_id, user_id)        -> active offers for product/user
        tools.check_offer_eligibility(product_id, user_id, offer_id) -> eligible: bool, reason

    Action tools:
        tools.get_availability(product_id)                     -> availability status (in_stock / out_of_stock)
        tools.send_notification(user_id, product_id, title, message) -> sends alert to user
        tools.update_watch_status(product_id, status)          -> status: "monitoring" | "paused" | "stopped"

Task input fields:
    task["task_id"]    - unique task ID to include in your return value
    task["user_id"]    - user being monitored
    task["product_id"] - product being watched
    task["objective"]  - natural language description of the goal
    task["trigger"]    - dict with "type" key, e.g. {"type": "PRICE_CHANGED", "product_id": "PROD-xxx"}

Trigger types:
    PRICE_CHANGED, OFFER_ADDED, OFFER_CHANGED, OFFER_EXPIRED, BANK_OFFER_CHANGED,
    BACK_IN_STOCK, OFFER_EXPIRING, PERIODIC_REEVALUATION

Required return format:
    {
        "task_id": str,
        "decision": {
            "opportunity_detected": bool,
            "notify_user": bool,
        },
        "price_analysis": {                # optional but recommended
            "current_price": float,
            "effective_price": float,
            "total_saving": float,
            "historical_low": float,
        },
        "offer_analysis": {                # optional but recommended
            "valid_offers": list[str],     # offer IDs eligible for user
            "invalid_offers": list[str],
        },
        "evidence": list[str],             # IDs of entities retrieved via tool calls
        "reason": str,
        "customer_notification": {         # required when notify_user=True; None when False
            "title": str,
            "message": str,
        },
        "confidence": float,               # 0.0 – 1.0
    }
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sdk.tools_client import ToolsClient

CENT = Decimal("0.01")


def to_decimal(val: Any) -> Decimal:
    """Converts numeric or string value to 2-decimal Decimal using standard half-up rounding."""
    if isinstance(val, Decimal):
        return val.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(val if val is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def solve(
    task: dict[str, Any],
    tools: ToolsClient,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Autonomous SmartCart shopping monitoring assistant.

    Investigates trigger events, verifies watchlist, checks stock, computes effective price
    accounting for stackable vs exclusive offers, evaluates user budget and savings preferences,
    and selectively sends notifications.
    """
    _ = api_key, model, base_url

    task_id = str(task.get("task_id", ""))
    user_id = str(task.get("user_id", ""))
    product_id = str(task.get("product_id", ""))
    trigger = task.get("trigger") or {}
    trigger_type = str(trigger.get("type", "")) if isinstance(trigger, dict) else str(trigger or "")

    # Set active task ID for scoped tool calls if supported
    if hasattr(tools, "set_active_task") and task_id:
        tools.set_active_task(task_id)

    # 1. Retrieve User Profile
    user: dict[str, Any] = {}
    try:
        user_resp = tools.get_user_profile(user_id=user_id)
        if isinstance(user_resp, dict):
            user = user_resp.get("user") or user_resp
    except Exception:
        user = {}

    # 2. Retrieve Saved Products (Watchlist)
    saved_products: list[str] = []
    try:
        saved_resp = tools.get_saved_products(user_id=user_id)
        if isinstance(saved_resp, dict):
            saved_products = saved_resp.get("products", [])
    except Exception:
        saved_products = []

    # 3. Check Stock Availability
    availability = "unknown"
    try:
        avail_resp = tools.get_availability(product_id=product_id)
        if isinstance(avail_resp, dict):
            availability = avail_resp.get("availability", "unknown")
    except Exception:
        availability = "unknown"

    # 4. Retrieve Product Details
    product: dict[str, Any] = {}
    try:
        prod_resp = tools.get_product(product_id=product_id)
        if isinstance(prod_resp, dict):
            product = prod_resp.get("product") or prod_resp
    except Exception:
        product = {}

    # 5. Retrieve Price History
    history_entries: list[dict[str, Any]] = []
    price_history_evidence_id = f"PRICE-HISTORY-{product_id}"
    try:
        hist_resp = tools.get_price_history(product_id=product_id, days=365)
        if isinstance(hist_resp, dict):
            price_history_evidence_id = hist_resp.get("evidence_id") or price_history_evidence_id
            history_entries = hist_resp.get("history", [])
    except Exception:
        history_entries = []

    # Historical low price computation
    hist_prices = [
        to_decimal(h.get("price"))
        for h in history_entries
        if isinstance(h, dict) and h.get("price") is not None
    ]
    historical_low_val = min(hist_prices) if hist_prices else None

    # 6. Retrieve and Verify Offers
    raw_offers: list[dict[str, Any]] = []
    try:
        offers_resp = tools.get_available_offers(product_id=product_id, user_id=user_id)
        if isinstance(offers_resp, dict):
            raw_offers = offers_resp.get("offers", [])
    except Exception:
        raw_offers = []

    valid_offers: list[str] = []
    invalid_offers: list[str] = []
    stackable_discount = Decimal("0.00")
    exclusive_discounts: list[Decimal] = []

    for offer in raw_offers:
        if not isinstance(offer, dict):
            continue
        offer_id = offer.get("offer_id")
        if not offer_id:
            continue

        is_eligible = False
        try:
            elig_resp = tools.check_offer_eligibility(
                product_id=product_id, user_id=user_id, offer_id=offer_id
            )
            if isinstance(elig_resp, dict):
                is_eligible = bool(elig_resp.get("eligible", False))
        except Exception:
            is_eligible = False

        if is_eligible:
            valid_offers.append(offer_id)
            val = to_decimal(offer.get("value", 0))
            if offer.get("stackable", True):
                stackable_discount += val
            else:
                exclusive_discounts.append(val)
        else:
            invalid_offers.append(offer_id)

    max_exclusive = max(exclusive_discounts) if exclusive_discounts else Decimal("0.00")
    total_discount = stackable_discount + max_exclusive

    # 7. Price & Savings Calculations
    current_price = to_decimal(product.get("current_price", 0))
    effective_price = max(Decimal("0.00"), current_price - total_discount)
    saved_price = to_decimal(product.get("saved_price", current_price))
    total_saving = max(Decimal("0.00"), saved_price - effective_price)
    mrp = to_decimal(product.get("mrp", current_price))

    budget = to_decimal(user.get("budget_limit", 0))
    min_saving = to_decimal(user.get("minimum_saving", 0))
    prefs = user.get("notification_preferences") or {}

    # 8. Evidence Construction
    evidence: list[str] = [
        f"USER-{user_id}",
        product_id,
        price_history_evidence_id,
    ]
    for oid in valid_offers:
        if oid not in evidence:
            evidence.append(oid)

    # 9. Evaluate Pre-conditions
    in_stock = availability in ("in_stock", "low_stock")
    is_saved = product_id in saved_products
    opted_in = bool(
        prefs.get("notify_on_price_drop", True)
        or prefs.get("notify_on_deal", True)
        or prefs.get("notify_on_coupon", True)
    )

    price_analysis = {
        "current_price": float(current_price),
        "effective_price": float(effective_price),
        "total_saving": float(total_saving),
        "historical_low": float(historical_low_val) if historical_low_val is not None else float(current_price),
    }
    offer_analysis = {
        "valid_offers": valid_offers,
        "invalid_offers": invalid_offers,
    }

    # If missing required data, not saved, or out of stock
    if not user or not product or not is_saved:
        return {
            "task_id": task_id,
            "decision": {
                "opportunity_detected": False,
                "notify_user": False,
            },
            "price_analysis": price_analysis,
            "offer_analysis": offer_analysis,
            "evidence": evidence,
            "reason": "Product not in user saved watchlist.",
            "customer_notification": None,
            "confidence": 0.95,
        }

    if not in_stock:
        return {
            "task_id": task_id,
            "decision": {
                "opportunity_detected": False,
                "notify_user": False,
            },
            "price_analysis": price_analysis,
            "offer_analysis": offer_analysis,
            "evidence": evidence,
            "reason": f"Product is currently out of stock ({availability}).",
            "customer_notification": None,
            "confidence": 0.95,
        }

    # 10. Check Misleading MRP Discount
    mrp_cut = mrp - current_price
    vs_history = (historical_low_val - effective_price) if historical_low_val is not None else Decimal("0.00")
    misleading = (mrp_cut >= Decimal("500")) and (vs_history <= Decimal("50"))

    within_budget = effective_price <= budget
    meaningful_saving = total_saving >= min_saving
    at_or_below_hist_low = (historical_low_val is not None) and (effective_price <= historical_low_val)

    # 11. Trigger & Opportunity Evaluation
    opportunity = False
    notify = False
    reasons: list[str] = []

    # Stock Triggers
    if trigger_type in ("BACK_IN_STOCK", "STOCK_CHANGED") and prefs.get("notify_on_stock_change", False):
        if within_budget:
            opportunity = True
            notify = True
            reasons.append("back_in_stock")
            if meaningful_saving:
                reasons.append("meets_minimum_saving_threshold")
            if at_or_below_hist_low:
                reasons.append("historical_low")
        else:
            opportunity = False
            notify = False
            reasons.append("back_in_stock_but_above_budget")
    elif trigger_type == "OFFER_EXPIRED":
        opportunity = False
        notify = False
        reasons.append("offer_expired")
    elif misleading and not meaningful_saving and not at_or_below_hist_low:
        opportunity = False
        notify = False
        reasons.append("misleading_mrp_discount")
    elif within_budget and (meaningful_saving or at_or_below_hist_low):
        opportunity = True
        reasons.append("effective_price_meets_user_constraints")
        if at_or_below_hist_low:
            reasons.append("historical_low")

        notify_allowed = True
        if trigger_type == "PRICE_CHANGED" and not prefs.get("notify_on_price_drop", True):
            notify_allowed = False
            reasons.append("price_drop_notifications_disabled")
        if trigger_type in ("OFFER_ADDED", "OFFER_CHANGED", "COUPON_CHANGED", "BANK_OFFER_CHANGED") and not prefs.get("notify_on_coupon", True):
            notify_allowed = False
            reasons.append("coupon_notifications_disabled")
        if not opted_in:
            notify_allowed = False
            reasons.append("user_opted_out_of_notifications")

        notify = notify_allowed
    elif not within_budget:
        opportunity = False
        notify = False
        reasons.append("above_budget")
    else:
        opportunity = False
        notify = False
        reasons.append("saving_below_user_threshold")

    # 12. Execute Action if Notification Qualified
    customer_notification = None
    if notify:
        prod_name = product.get("name", product_id)
        notification_title = f"SmartCart Price Drop: {prod_name[:40]}"
        notification_message = (
            f"Good news! {prod_name} is now available at an effective price of ${float(effective_price):.2f}, "
            f"saving you ${float(total_saving):.2f}. In stock and within your budget!"
        )
        try:
            tools.send_notification(
                user_id=user_id,
                product_id=product_id,
                title=notification_title,
                message=notification_message,
            )
            customer_notification = {
                "title": notification_title,
                "message": notification_message,
            }
        except Exception:
            # If server rejects the notification, respect server decision
            notify = False
            customer_notification = None

    reason_str = "; ".join(reasons) if reasons else "opportunity evaluated"

    return {
        "task_id": task_id,
        "decision": {
            "opportunity_detected": opportunity,
            "notify_user": notify,
        },
        "price_analysis": price_analysis,
        "offer_analysis": offer_analysis,
        "evidence": evidence,
        "reason": reason_str,
        "customer_notification": customer_notification,
        "confidence": 0.95 if (opportunity or not within_budget or not in_stock) else 0.9,
    }

