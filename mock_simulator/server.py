
import copy
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

MOCK_DIR = Path(__file__).resolve().parent
DATA_DIR = MOCK_DIR / "data"
DB_PATH = os.getenv("MOCK_DATABASE_PATH", str(MOCK_DIR / "mock_arena.db"))
REVEAL_GROUND_TRUTH = os.getenv("REVEAL_GROUND_TRUTH", "true").lower() in ("1", "true", "yes")
CENT = Decimal("0.01")

# =============================================================================
# GENERATED FROM src/agent_arena/domain/rules.py & models.py
# DO NOT EDIT MANUALLY - Run `python scripts/export_starter_kit.py` to regenerate
# =============================================================================


from dataclasses import dataclass


@dataclass
class EligibilityResult:
    is_eligible: bool
    status: str = "success"
    reason: str | None = None
    policy_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.is_eligible:
            return {"status": self.status, "eligible": True}
        res = {"eligible": False, "error": self.error or "INELIGIBLE"}
        if self.reason:
            res["reason"] = self.reason
        if self.policy_ref:
            res["policy_ref"] = self.policy_ref
        return res


CENT = Decimal("0.01")


def to_decimal(val: Any) -> Decimal:
    """Converts numeric or string value to 2-decimal Decimal using standard half-up rounding."""
    if isinstance(val, Decimal):
        return val.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(val if val is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_iso(dt_str: str) -> datetime:
    """Parses ISO timestamp string to timezone-aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime(2026, 1, 1, tzinfo=UTC)


# --- SmartCart monitoring triggers -------------------------------------------------
# A trigger only wakes/activates the agent. It must never by itself cause a
# notification: every trigger is re-investigated against live data and the
# user's own constraints before a decision is reached.
TRIGGER_TYPES: tuple[str, ...] = (
    "PRICE_CHANGED",
    "OFFER_ADDED",
    "OFFER_CHANGED",
    "OFFER_EXPIRED",
    "BANK_OFFER_CHANGED",
    "BACK_IN_STOCK",
    "OFFER_EXPIRING",
    "PERIODIC_REEVALUATION",
)

# "STOCK_CHANGED" and "COUPON_CHANGED" are retained as legacy aliases so older
# task rows keep evaluating identically.
PRICE_TRIGGERS: frozenset[str] = frozenset({"PRICE_CHANGED"})
COUPON_TRIGGERS: frozenset[str] = frozenset(
    {"OFFER_ADDED", "OFFER_CHANGED", "COUPON_CHANGED", "BANK_OFFER_CHANGED"}
)
STOCK_TRIGGERS: frozenset[str] = frozenset({"BACK_IN_STOCK", "STOCK_CHANGED"})
EXPIRY_TRIGGERS: frozenset[str] = frozenset({"OFFER_EXPIRED", "OFFER_EXPIRING"})


def trigger_type_of(world_state: dict[str, Any]) -> str:
    """Returns the canonical trigger type that activated the agent, if any."""
    trigger = world_state.get("monitoring_trigger") or {}
    if isinstance(trigger, str):
        return trigger
    if isinstance(trigger, dict):
        return str(trigger.get("type") or "")
    return ""


def trigger_offer_id(world_state: dict[str, Any]) -> str | None:
    trigger = world_state.get("monitoring_trigger") or {}
    if isinstance(trigger, dict):
        value = trigger.get("offer_id")
        return str(value) if value else None
    return None


def find_user(world_state: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    for user in world_state.get("users", []):
        if isinstance(user, dict) and user.get("user_id") == user_id:
            return user
    return None


def find_product(world_state: dict[str, Any], product_id: str) -> dict[str, Any] | None:
    for product in world_state.get("products", []):
        if isinstance(product, dict) and product.get("product_id") == product_id:
            return product
    return None


def find_offer(world_state: dict[str, Any], offer_id: str) -> dict[str, Any] | None:
    for offer in world_state.get("offers", []):
        if isinstance(offer, dict) and offer.get("offer_id") == offer_id:
            return offer
    return None


def user_saved_product_ids(world_state: dict[str, Any], user_id: str) -> list[str]:
    for row in world_state.get("saved_products", []):
        if isinstance(row, dict) and row.get("user_id") == user_id:
            products = row.get("products", [])
            return [p for p in products if isinstance(p, str)]
    return []


def historical_prices(world_state: dict[str, Any], product_id: str) -> list[dict[str, Any]]:
    for row in world_state.get("price_history", []):
        if isinstance(row, dict) and row.get("product_id") == product_id:
            history = row.get("history", [])
            return [h for h in history if isinstance(h, dict)]
    return []


def historical_low(world_state: dict[str, Any], product_id: str) -> float | None:
    prices = [to_decimal(h.get("price", 0)) for h in historical_prices(world_state, product_id)]
    if not prices:
        return None
    return float(min(prices))


def check_offer_eligibility(
    world_state: dict[str, Any],
    product_id: str,
    user_id: str,
    offer_id: str,
) -> EligibilityResult:
    """Canonical SmartCart offer eligibility check.

    An offer is eligible only when it exists, applies to the product (or is global),
    has not expired, and the user satisfies the offer's eligibility constraint.
    """
    offer = find_offer(world_state, offer_id)
    if not offer:
        return EligibilityResult(
            is_eligible=False,
            error="OFFER_NOT_FOUND",
            reason=f"Offer '{offer_id}' does not exist.",
        )

    offer_product = offer.get("product_id")
    if offer_product and offer_product not in ("*", product_id):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="offer_product_mismatch",
        )

    current_date_str = world_state.get("current_date", "2026-09-17T10:00:00Z")
    valid_until = offer.get("valid_until")
    if valid_until and parse_iso(valid_until) < parse_iso(current_date_str):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="offer_expired",
        )

    valid_from = offer.get("valid_from")
    if valid_from and parse_iso(valid_from) > parse_iso(current_date_str):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="offer_not_yet_valid",
        )

    user = find_user(world_state, user_id)
    if not user:
        return EligibilityResult(
            is_eligible=False,
            error="USER_NOT_FOUND",
            reason=f"User '{user_id}' not found.",
        )

    product = find_product(world_state, product_id)
    if not product:
        return EligibilityResult(
            is_eligible=False,
            error="PRODUCT_NOT_FOUND",
            reason=f"Product '{product_id}' not found.",
        )

    eligibility = offer.get("eligibility", "all_users")
    if eligibility == "all_users":
        pass
    elif eligibility == "preferred_brand":
        preferred = user.get("preferred_brands", [])
        if product.get("brand") not in preferred:
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="brand_not_preferred",
            )
    elif eligibility == "bank_card":
        required_card = offer.get("required_card")
        cards = user.get("bank_cards", [])
        if required_card and required_card not in cards:
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="bank_card_ineligible",
            )
    elif eligibility == "min_spend":
        min_spend = to_decimal(offer.get("min_spend", 0))
        if to_decimal(product.get("current_price", 0)) < min_spend:
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="min_spend_not_met",
            )
    elif eligibility == "specific_users":
        allowed = offer.get("user_ids", [])
        if user_id not in allowed:
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="user_not_in_offer_audience",
            )
    else:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="unknown_eligibility_rule",
        )

    return EligibilityResult(is_eligible=True, status="eligible")


def eligible_offers_for_product(
    world_state: dict[str, Any],
    product_id: str,
    user_id: str,
) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for offer in world_state.get("offers", []):
        if not isinstance(offer, dict):
            continue
        offer_id = offer.get("offer_id")
        if not offer_id:
            continue
        result = check_offer_eligibility(world_state, product_id, user_id, offer_id)
        if result.is_eligible:
            eligible.append(offer)
    return eligible


def compute_effective_price(
    world_state: dict[str, Any],
    product_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Effective Price = current price - valid applicable discounts for this user."""
    product = find_product(world_state, product_id)
    if not product:
        return {
            "current_price": None,
            "effective_price": None,
            "total_discount": 0.0,
            "valid_offers": [],
            "invalid_offers": [],
        }

    current = to_decimal(product.get("current_price", 0))
    valid: list[str] = []
    invalid: list[str] = []
    stackable_discount = Decimal("0.00")
    exclusive_discounts: list[Decimal] = []

    for offer in world_state.get("offers", []):
        if not isinstance(offer, dict) or not offer.get("offer_id"):
            continue
        offer_product = offer.get("product_id")
        if offer_product and offer_product not in ("*", product_id):
            continue
        result = check_offer_eligibility(world_state, product_id, user_id, offer["offer_id"])
        if not result.is_eligible:
            invalid.append(offer["offer_id"])
            continue
        valid.append(offer["offer_id"])
        value = to_decimal(offer.get("value", 0))
        if offer.get("stackable", True):
            stackable_discount += value
        else:
            exclusive_discounts.append(value)

    exclusive = max(exclusive_discounts) if exclusive_discounts else Decimal("0.00")
    total_discount = stackable_discount + exclusive
    effective = current - total_discount
    if effective < Decimal("0.00"):
        effective = Decimal("0.00")

    saved_price = to_decimal(product.get("saved_price", current))
    total_saving = saved_price - effective
    if total_saving < Decimal("0.00"):
        total_saving = Decimal("0.00")

    return {
        "current_price": float(current),
        "effective_price": float(effective),
        "total_discount": float(total_discount),
        "total_saving": float(total_saving),
        "valid_offers": valid,
        "invalid_offers": invalid,
        "historical_low": historical_low(world_state, product_id),
        "mrp": float(to_decimal(product.get("mrp", current))),
    }


def is_misleading_mrp_discount(world_state: dict[str, Any], product_id: str, effective_price: Decimal) -> bool:
    """True when the listed MRP cut looks large but selling price is not historically exceptional."""
    product = find_product(world_state, product_id)
    if not product:
        return False
    mrp = to_decimal(product.get("mrp", 0))
    current = to_decimal(product.get("current_price", 0))
    hist_low = historical_low(world_state, product_id)
    if hist_low is None:
        return False
    mrp_cut = mrp - current
    vs_history = to_decimal(hist_low) - effective_price
    return mrp_cut >= Decimal("500") and vs_history <= Decimal("50")


def evaluate_opportunity(
    world_state: dict[str, Any],
    user_id: str,
    product_id: str,
) -> dict[str, Any]:
    """Canonical opportunity classifier used by scoring and dataset generation."""
    user = find_user(world_state, user_id)
    product = find_product(world_state, product_id)
    analysis = compute_effective_price(world_state, product_id, user_id)
    prefs = (user or {}).get("notification_preferences") or {}

    reasons: list[str] = []
    opportunity = False
    notify = False

    if not user or not product:
        return {
            "opportunity_detected": False,
            "notify_user": False,
            "reason": "missing_user_or_product",
            **analysis,
        }

    if product_id not in user_saved_product_ids(world_state, user_id):
        return {
            "opportunity_detected": False,
            "notify_user": False,
            "reason": "product_not_saved",
            **analysis,
        }

    availability = product.get("availability", "in_stock")
    effective = to_decimal(analysis["effective_price"])
    current = to_decimal(analysis["current_price"])
    saving = to_decimal(analysis["total_saving"])
    budget = to_decimal(user.get("budget_limit", 0))
    min_saving = to_decimal(user.get("minimum_saving", 0))
    hist_low = analysis.get("historical_low")
    trigger_type = trigger_type_of(world_state)

    in_stock = availability in ("in_stock", "low_stock")
    within_budget = effective <= budget
    meaningful_saving = saving >= min_saving
    at_or_below_hist_low = hist_low is not None and effective <= to_decimal(hist_low)
    misleading = is_misleading_mrp_discount(world_state, product_id, effective)

    if not in_stock:
        return {
            "opportunity_detected": False,
            "notify_user": False,
            "reason": "out_of_stock",
            "classification": "out_of_stock",
            **analysis,
        }

    # An explicit availability trigger is a first-class SmartCart event, yet it
    # still requires the user to have opted into stock alerts and to be within budget.
    if trigger_type in STOCK_TRIGGERS and prefs.get("notify_on_stock_change", False):
        if within_budget:
            reasons.append("back_in_stock")
            if meaningful_saving:
                reasons.append("meets_minimum_saving_threshold")
            if at_or_below_hist_low:
                reasons.append("historical_low")
            return {
                "opportunity_detected": True,
                "notify_user": True,
                "reason": ";".join(reasons),
                "classification": "back_in_stock",
                **analysis,
            }
        return {
            "opportunity_detected": False,
            "notify_user": False,
            "reason": "back_in_stock_but_above_budget",
            "classification": "above_budget",
            **analysis,
        }

    if misleading and not meaningful_saving and not at_or_below_hist_low:
        return {
            "opportunity_detected": False,
            "notify_user": False,
            "reason": "misleading_mrp_discount",
            "classification": "misleading_discount",
            **analysis,
        }

    if within_budget and (meaningful_saving or at_or_below_hist_low):
        opportunity = True
        reasons.append("effective_price_meets_user_constraints")
        if at_or_below_hist_low:
            reasons.append("historical_low")
        notify_allowed = True
        if trigger_type in PRICE_TRIGGERS and not prefs.get("notify_on_price_drop", True):
            notify_allowed = False
            reasons.append("price_drop_notifications_disabled")
        if trigger_type in COUPON_TRIGGERS and not prefs.get("notify_on_coupon", True):
            notify_allowed = False
            reasons.append("coupon_notifications_disabled")
        notify = notify_allowed
    elif within_budget and current < to_decimal(product.get("saved_price", current)) and saving < min_saving:
        reasons.append("saving_below_user_threshold")
    elif not within_budget:
        reasons.append("above_budget")
    else:
        reasons.append("not_meaningful")

    if trigger_type == "OFFER_EXPIRING" and not notify:
        reasons.append("expiring_offer_not_worth_notifying")
    elif trigger_type == "OFFER_EXPIRED" and not notify:
        reasons.append("offer_expired")
    elif trigger_type == "PERIODIC_REEVALUATION" and not notify:
        reasons.append("periodic_reevaluation_no_action")
    elif trigger_type == "BANK_OFFER_CHANGED" and not notify:
        reasons.append("bank_offer_not_advantageous")

    return {
        "opportunity_detected": opportunity,
        "notify_user": notify,
        "reason": ";".join(reasons) or "not_meaningful",
        "classification": classify_opportunity(
            world_state,
            user_id,
            product_id,
            {
                "opportunity_detected": opportunity,
                "notify_user": notify,
                "in_stock": in_stock,
                "within_budget": within_budget,
                "meaningful_saving": meaningful_saving,
                "at_or_below_hist_low": at_or_below_hist_low,
                "misleading": misleading,
            },
        ),
        **analysis,
    }


def classify_opportunity(
    world_state: dict[str, Any],
    user_id: str,
    product_id: str,
    verdict: dict[str, Any] | None = None,
) -> str:
    """Canonical SmartCart classification label for an investigated trigger.

    Ground truth, scoring and the reference agent share this single vocabulary so
    that every notification decision is explainable and gradeable.
    """
    analysis = compute_effective_price(world_state, product_id, user_id)
    user = find_user(world_state, user_id)
    product = find_product(world_state, product_id)
    trigger_type = trigger_type_of(world_state)

    if verdict is None:
        effective = to_decimal(analysis.get("effective_price") or 0)
        budget = to_decimal((user or {}).get("budget_limit", 0))
        minimum_saving = to_decimal((user or {}).get("minimum_saving", 0))
        saving = to_decimal(analysis.get("total_saving") or 0)
        hist_low = analysis.get("historical_low")
        at_or_below = hist_low is not None and effective <= to_decimal(hist_low)
        within_budget = effective <= budget
        meaningful = saving >= minimum_saving
        verdict = {
            "in_stock": (product or {}).get("availability", "in_stock") in ("in_stock", "low_stock"),
            "within_budget": within_budget,
            "meaningful_saving": meaningful,
            "at_or_below_hist_low": at_or_below,
            "misleading": is_misleading_mrp_discount(world_state, product_id, effective),
        }
        verdict["opportunity_detected"] = bool(within_budget and (meaningful or at_or_below))
        verdict["notify_user"] = verdict["opportunity_detected"]

    if not verdict.get("in_stock", True):
        return "out_of_stock"
    if verdict.get("misleading"):
        return "misleading_discount"
    if verdict.get("notify_user"):
        if trigger_type in STOCK_TRIGGERS:
            return "back_in_stock"
        if trigger_type == "OFFER_EXPIRING":
            return "expiring_offer"
        if trigger_type == "BANK_OFFER_CHANGED":
            return "bank_offer"
        if verdict.get("at_or_below_hist_low"):
            return "historical_low"
        return "meaningful_opportunity"
    if not verdict.get("within_budget", True):
        return "above_budget"
    invalid_offers = analysis.get("invalid_offers") or []
    if invalid_offers and not analysis.get("valid_offers"):
        return "ineligible_offer"
    if trigger_type == "OFFER_EXPIRED":
        return "offer_expired"
    if verdict.get("opportunity_detected"):
        return "insignificant_saving"
    return "no_notification"


def check_notification_eligibility(
    world_state: dict[str, Any],
    user_id: str,
    product_id: str,
) -> EligibilityResult:
    user = find_user(world_state, user_id)
    if not user:
        return EligibilityResult(
            is_eligible=False,
            error="USER_NOT_FOUND",
            reason=f"User '{user_id}' not found.",
        )
    if not find_product(world_state, product_id):
        return EligibilityResult(
            is_eligible=False,
            error="PRODUCT_NOT_FOUND",
            reason=f"Product '{product_id}' not found.",
        )
    if product_id not in user_saved_product_ids(world_state, user_id):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="product_not_saved",
        )
    # Check user notification preferences
    prefs = user.get("notification_preferences") or {}
    opted_in = (
        prefs.get("notify_on_price_drop", True)
        or prefs.get("notify_on_deal", True)
        or prefs.get("notify_on_coupon", True)
    )
    if not opted_in:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="user_opted_out_of_notifications",
        )
    return EligibilityResult(is_eligible=True, status="notified")



def apply_timeline_event(world_state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Applies one purchasing-condition change to a copy of world state."""
    state = copy.deepcopy(world_state)
    event_type = event.get("type")
    product_id = event.get("product_id")
    if event.get("current_date"):
        state["current_date"] = event["current_date"]

    if event_type == "PRICE_CHANGED" and product_id:
        for product in state.get("products", []):
            if product.get("product_id") == product_id and "price" in event:
                product["current_price"] = event["price"]
        history_row = next(
            (h for h in state.get("price_history", []) if h.get("product_id") == product_id),
            None,
        )
        if history_row is None:
            history_row = {"product_id": product_id, "history": []}
            state.setdefault("price_history", []).append(history_row)
        history_row.setdefault("history", []).append(
            {
                "timestamp": state.get("current_date", "2026-09-17T10:00:00Z"),
                "price": event.get("price"),
            }
        )
    elif event_type in ("OFFER_ADDED", "OFFER_CHANGED", "COUPON_CHANGED", "BANK_OFFER_CHANGED") and event.get("offer"):
        offers = state.setdefault("offers", [])
        incoming = copy.deepcopy(event["offer"])
        existing = next((o for o in offers if o.get("offer_id") == incoming.get("offer_id")), None)
        if existing:
            existing.update(incoming)
        else:
            offers.append(incoming)
    elif event_type == "OFFER_EXPIRING" and event.get("offer_id"):
        # The offer is still valid but about to lapse: keep it active, shorten its window.
        current = parse_iso(state.get("current_date", "2026-09-17T10:00:00Z"))
        default_until = (current + timedelta(hours=6)).isoformat()
        for offer in state.get("offers", []):
            if offer.get("offer_id") == event["offer_id"]:
                offer["valid_until"] = event.get("valid_until") or default_until
    elif event_type == "OFFER_EXPIRED" and event.get("offer_id"):
        for offer in state.get("offers", []):
            if offer.get("offer_id") == event["offer_id"]:
                offer["valid_until"] = event.get("valid_until") or state.get("current_date")
    elif event_type in ("STOCK_CHANGED", "BACK_IN_STOCK") and product_id:
        availability = event.get("availability") or ("in_stock" if event_type == "BACK_IN_STOCK" else None)
        if availability:
            for product in state.get("products", []):
                if product.get("product_id") == product_id:
                    product["availability"] = availability
            rows = state.setdefault("availability", [])
            row = next((r for r in rows if r.get("product_id") == product_id), None)
            if row is None:
                rows.append(
                    {
                        "product_id": product_id,
                        "availability": availability,
                        "updated_at": state.get("current_date"),
                    }
                )
            else:
                row["availability"] = availability
                row["updated_at"] = state.get("current_date")
    elif event_type == "PERIODIC_REEVALUATION":
        # Scheduled re-evaluation: no world mutation, the agent only re-investigates.
        pass

    trigger = {
        "type": event_type,
        "product_id": product_id,
        "offer_id": event.get("offer_id"),
    }
    state["monitoring_trigger"] = trigger
    return state


def apply_action_to_world(
    world_state: dict[str, Any],
    action_type: str,
    params: dict[str, Any],
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> tuple[dict[str, Any], EligibilityResult]:
    """Applies an action to a working copy of world state.

    Returns (mutated_world_state, eligibility_result).
    If ineligible, world state remains completely unmodified.
    """
    _ = retrieved_evidence_ids
    state_copy = copy.deepcopy(world_state)

    if action_type == "send_notification":
        user_id = params.get("user_id", "")
        product_id = params.get("product_id", "")
        result = check_notification_eligibility(state_copy, user_id, product_id)
        if result.is_eligible:
            notifications = state_copy.setdefault("notifications", [])
            notifications.append(
                {
                    "user_id": user_id,
                    "product_id": product_id,
                    "title": params.get("title", ""),
                    "message": params.get("message", ""),
                    "timestamp": state_copy.get("current_date", "2026-09-17T10:00:00Z"),
                }
            )
        return state_copy if result.is_eligible else world_state, result

    if action_type == "update_watch_status":
        product_id = params.get("product_id", "")
        status_value = params.get("status", "monitoring")
        if not find_product(state_copy, product_id):
            return world_state, EligibilityResult(
                is_eligible=False,
                error="PRODUCT_NOT_FOUND",
                reason=f"Product '{product_id}' not found.",
            )
        watch = state_copy.setdefault("watch_status", {})
        watch[product_id] = status_value
        timeline = state_copy.get("timeline") or []
        idx = int(state_copy.get("timeline_index", 0) or 0)
        if status_value == "monitoring" and idx < len(timeline):
            state_copy = apply_timeline_event(state_copy, timeline[idx])
            state_copy["timeline_index"] = idx + 1
            state_copy.setdefault("watch_status", {})[product_id] = status_value
        return state_copy, EligibilityResult(is_eligible=True, status="watch_updated")

    return world_state, EligibilityResult(is_eligible=True, status="no_action")


# =============================================================================
# END GENERATED DOMAIN SECTION
# =============================================================================

# =============================================================================
# Request / Response schemas
# =============================================================================


class GetUserProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: str = Field(..., min_length=1, max_length=100)


class GetSavedProductsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: str = Field(..., min_length=1, max_length=100)


class GetProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(..., min_length=1, max_length=100)


class GetPriceHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(..., min_length=1, max_length=100)
    days: int = Field(default=30, ge=1, le=365)


class GetAvailableOffersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(..., min_length=1, max_length=100)
    user_id: str = Field(..., min_length=1, max_length=100)


class CheckOfferEligibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(..., min_length=1, max_length=100)
    user_id: str = Field(..., min_length=1, max_length=100)
    offer_id: str = Field(..., min_length=1, max_length=100)


class GetAvailabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(..., min_length=1, max_length=100)


class SendNotificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: str = Field(..., min_length=1, max_length=100)
    product_id: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=4000)


class UpdateWatchStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(..., min_length=1, max_length=100)
    status: Literal["monitoring", "paused", "stopped"] = "monitoring"


class ShoppingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunity_detected: bool
    notify_user: bool


class CustomerNotification(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = ""
    message: str = ""


class TaskSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    task_id: str
    decision: ShoppingDecision
    price_analysis: dict[str, Any] | None = None
    offer_analysis: dict[str, Any] | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    customer_notification: CustomerNotification | None = None
    confidence: float = 0.5


class TaskStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    user_id: str
    product_id: str
    objective: str
    trigger: dict[str, Any] = Field(default_factory=dict)


class TaskSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    received: bool = True
    task_id: str


class TaskSubmitPracticeResponse(TaskSubmitResponse):
    correct: bool
    expected_resolution: str | None = None
    expected_evidence: list[str] = Field(default_factory=list)
    your_evidence: list[str] = Field(default_factory=list)
    diff_explanation: str


class SubmissionStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    attempt_number: int
    tasks_total: int
    tasks: list[TaskStartResponse] = Field(default_factory=list)


class SubmissionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    tasks_completed: int
    tasks_total: int
    time_remaining_seconds: int


class SubmissionFinalizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    status: str = "completed"


class BatchTaskSubmitItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: str
    decision: ShoppingDecision
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    customer_notification: CustomerNotification | None = None
    confidence: float | None = None
    price_analysis: dict[str, Any] | None = None
    offer_analysis: dict[str, Any] | None = None


class BatchSubmissionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    answers: list[BatchTaskSubmitItem]


class BatchSubmissionSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    status: str
    tasks_submitted: int
    tasks_total: int
    score_pct: float | None = None
    passed: bool | None = None
    inter_task_durations: list[float] = Field(default_factory=list)


class SubmissionAbortResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    status: str = "interrupted"
    message: str


def merge_entities_by_key(base_list: list[dict], override_list: list[dict], key: str = "id") -> list[dict]:
    res = copy.deepcopy(base_list)
    lookup = {item[key]: i for i, item in enumerate(res) if key in item}
    for item in override_list:
        if key in item and item[key] in lookup:
            res[lookup[item[key]]] = copy.deepcopy(item)
        else:
            res.append(copy.deepcopy(item))
            if key in item:
                lookup[item[key]] = len(res) - 1
    return res


def load_tasks_from_data_dir(data_dir: Path) -> list[dict]:
    tasks_file = data_dir / "tasks.json"
    gt_file = data_dir / "ground_truth.json"
    if not tasks_file.exists() or not gt_file.exists():
        return []
    with open(tasks_file, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    with open(gt_file, "r", encoding="utf-8") as f:
        gts = json.load(f)
    gt_map = {g["task_id"]: g for g in gts}

    def _read_json(fname: str) -> list[dict]:
        p = data_dir / fname
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    catalogs = {
        "users": _read_json("users.json"),
        "products": _read_json("products.json"),
        "price_history": _read_json("price_history.json"),
        "offers": _read_json("offers.json"),
        "saved_products": _read_json("saved_products.json"),
        "availability": _read_json("availability.json"),
    }
    compiled = []
    for t in tasks:
        tid = t["task_id"]
        inp = t.get("input_payload", {})
        overrides = t.get("task_overrides", {})
        world = {
            "seed": 1000,
            "current_date": overrides.get("current_date", "2026-09-17T10:00:00+00:00"),
            "users": copy.deepcopy(catalogs["users"]),
            "products": copy.deepcopy(catalogs["products"]),
            "price_history": copy.deepcopy(catalogs["price_history"]),
            "offers": copy.deepcopy(catalogs["offers"]),
            "saved_products": copy.deepcopy(catalogs["saved_products"]),
            "availability": copy.deepcopy(catalogs["availability"]),
            "notifications": [],
            "watch_status": {},
            "timeline": copy.deepcopy(overrides.get("timeline", [])),
            "timeline_index": 0,
            "target_user_id": inp.get("user_id", ""),
            "target_product_id": inp.get("product_id", ""),
            "monitoring_trigger": inp.get("trigger", {}),
        }
        for key, pk in (
            ("users", "user_id"),
            ("products", "product_id"),
            ("price_history", "product_id"),
            ("offers", "offer_id"),
            ("saved_products", "user_id"),
            ("availability", "product_id"),
        ):
            if key in overrides:
                world[key] = merge_entities_by_key(world[key], overrides[key], pk)
        compiled.append({
            "task_id": tid,
            "dataset": t.get("dataset", "dev"),
            "input_payload": inp,
            "world_state_seed": world,
            "ground_truth": gt_map.get(tid, {}),
        })
    return compiled


def init_db(conn: sqlite3.Connection | None = None) -> None:
    close_when_done = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_when_done = True
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS mock_tasks (
                task_id TEXT PRIMARY KEY, dataset TEXT DEFAULT 'dev', input_payload TEXT NOT NULL,
                world_state_seed TEXT NOT NULL, ground_truth_privileged TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS mock_task_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES mock_tasks(task_id), submission_id TEXT,
                assigned_at TIMESTAMP NOT NULL, world_runtime_state TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS mock_tool_call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                submission_id TEXT, tool_name TEXT NOT NULL, request_payload TEXT NOT NULL,
                response_payload TEXT NOT NULL, was_enforcement_rejection INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS mock_submissions (
                submission_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL, started_at TIMESTAMP NOT NULL, completed_at TIMESTAMP,
                per_task_results TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS mock_task_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                customer_id TEXT, customer_message TEXT, correct INTEGER NOT NULL,
                actual_resolution TEXT, expected_resolution TEXT, actual_escalation INTEGER,
                expected_escalation INTEGER, actual_evidence TEXT, expected_evidence TEXT,
                missing_evidence TEXT, diff_explanation TEXT, tool_calls TEXT,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mock_tasks")
            if cursor.fetchone()[0] == 0 and DATA_DIR.exists():
                for t in load_tasks_from_data_dir(DATA_DIR):
                    conn.execute(
                        "INSERT OR REPLACE INTO mock_tasks (task_id, dataset, input_payload, world_state_seed, ground_truth_privileged) VALUES (?, ?, ?, ?, ?)",
                        (t["task_id"], t.get("dataset", "dev"), json.dumps(t["input_payload"]), json.dumps(t["world_state_seed"]), json.dumps(t["ground_truth"])),
                    )
    finally:
        if close_when_done:
            conn.close()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mock_tasks'")
    if not cursor.fetchone():
        init_db(conn)
    return conn


def get_session_id(authorization: str | None) -> str:
    if not authorization:
        return "dev-default-session"
    token = authorization.replace("Bearer ", "").strip()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def get_active_assignment(conn: sqlite3.Connection, session_id: str, task_id: str | None = None):
    cursor = conn.cursor()
    if task_id:
        cursor.execute("SELECT id, task_id, world_runtime_state, submission_id FROM mock_task_assignments WHERE session_id = ? AND task_id = ? ORDER BY id DESC LIMIT 1", (session_id, task_id))
        row = cursor.fetchone()
        if row:
            return row["id"], row["task_id"], json.loads(row["world_runtime_state"]), row["submission_id"]
    cursor.execute("SELECT id, task_id, world_runtime_state, submission_id FROM mock_task_assignments WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "NO_ACTIVE_TASK", "message": "No active task assignment found for team. Start a task before using tools."})
    return row["id"], row["task_id"], json.loads(row["world_runtime_state"]), row["submission_id"]


def update_world_state(conn: sqlite3.Connection, assignment_id: int, new_state: dict[str, Any]) -> None:
    with conn:
        conn.execute("UPDATE mock_task_assignments SET world_runtime_state = ? WHERE id = ?", (json.dumps(new_state), assignment_id))


def log_tool_call(conn, session_id, task_id, submission_id, tool_name, request_payload, response_payload, was_rejection, latency_ms):
    with conn:
        conn.execute(
            "INSERT INTO mock_tool_call_logs (session_id, task_id, submission_id, tool_name, request_payload, response_payload, was_enforcement_rejection, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, task_id, submission_id, tool_name, json.dumps(request_payload), json.dumps(response_payload), 1 if was_rejection else 0, latency_ms),
        )


def _not_found(code: str, msg: str) -> None:
    raise HTTPException(status_code=404, detail={"error": code, "message": msg})


def run_read_tool(tool_name: str, world_state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "get_user_profile":
        user = find_user(world_state, payload["user_id"])
        if not user:
            _not_found("USER_NOT_FOUND", f"User '{payload['user_id']}' not found.")
        return {"user": user}
    if tool_name == "get_saved_products":
        if not find_user(world_state, payload["user_id"]):
            _not_found("USER_NOT_FOUND", f"User '{payload['user_id']}' not found.")
        return {"user_id": payload["user_id"], "products": user_saved_product_ids(world_state, payload["user_id"])}
    if tool_name == "get_product":
        product = find_product(world_state, payload["product_id"])
        if not product:
            _not_found("PRODUCT_NOT_FOUND", f"Product '{payload['product_id']}' not found.")
        return {"product": product}
    if tool_name == "get_price_history":
        pid = payload["product_id"]
        if not find_product(world_state, pid):
            _not_found("PRODUCT_NOT_FOUND", f"Product '{pid}' not found.")
        days = payload.get("days", 30)
        current = parse_iso(world_state.get("current_date", "2026-09-17T10:00:00Z"))
        history = []
        for point in historical_prices(world_state, pid):
            ts = parse_iso(point.get("timestamp", "1970-01-01T00:00:00Z"))
            if (current - ts).days <= days:
                history.append(point)
        evidence_id = f"PRICE-HISTORY-{pid}"
        for row in world_state.get("price_history", []):
            if row.get("product_id") == pid and row.get("evidence_id"):
                evidence_id = row["evidence_id"]
        return {"product_id": pid, "evidence_id": evidence_id, "history": history}
    if tool_name == "get_available_offers":
        if not find_user(world_state, payload["user_id"]):
            _not_found("USER_NOT_FOUND", f"User '{payload['user_id']}' not found.")
        if not find_product(world_state, payload["product_id"]):
            _not_found("PRODUCT_NOT_FOUND", f"Product '{payload['product_id']}' not found.")
        offers = []
        for offer in world_state.get("offers", []):
            op = offer.get("product_id")
            if op and op not in ("*", payload["product_id"]):
                continue
            offers.append(offer)
        return {"offers": offers}
    if tool_name == "check_offer_eligibility":
        if not find_user(world_state, payload["user_id"]):
            _not_found("USER_NOT_FOUND", f"User '{payload['user_id']}' not found.")
        if not find_product(world_state, payload["product_id"]):
            _not_found("PRODUCT_NOT_FOUND", f"Product '{payload['product_id']}' not found.")
        result = check_offer_eligibility(world_state, payload["product_id"], payload["user_id"], payload["offer_id"])
        if result.error == "OFFER_NOT_FOUND":
            _not_found("OFFER_NOT_FOUND", result.reason or "Offer not found.")
        return {"offer_id": payload["offer_id"], "eligible": result.is_eligible, "reason": result.reason}
    if tool_name == "get_availability":
        product = find_product(world_state, payload["product_id"])
        if not product:
            _not_found("PRODUCT_NOT_FOUND", f"Product '{payload['product_id']}' not found.")
        availability = product.get("availability", "unknown")
        updated_at = world_state.get("current_date")
        for row in world_state.get("availability", []):
            if row.get("product_id") == payload["product_id"]:
                availability = row.get("availability", availability)
                updated_at = row.get("updated_at", updated_at)
        return {"product_id": payload["product_id"], "availability": availability, "updated_at": updated_at}
    raise HTTPException(status_code=400, detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"})


def run_action_tool(tool_name: str, world_state: dict[str, Any], payload: dict[str, Any], retrieved_evidence_ids: set[str]):
    _ = retrieved_evidence_ids
    if tool_name == "send_notification":
        result = check_notification_eligibility(world_state, payload["user_id"], payload["product_id"])
        if result.error == "USER_NOT_FOUND":
            _not_found("USER_NOT_FOUND", result.reason or "")
        if result.error == "PRODUCT_NOT_FOUND":
            _not_found("PRODUCT_NOT_FOUND", result.reason or "")
        if not result.is_eligible:
            return None, {"error": "INELIGIBLE", "reason": result.reason or "ineligible_action"}, True
        mutated, _r = apply_action_to_world(world_state, "send_notification", payload)
        return mutated, {"status": "notified", "notification": mutated["notifications"][-1]}, False
    if tool_name == "update_watch_status":
        if not find_product(world_state, payload["product_id"]):
            _not_found("PRODUCT_NOT_FOUND", f"Product '{payload['product_id']}' not found.")
        mutated, _r = apply_action_to_world(world_state, "update_watch_status", payload)
        return mutated, {"status": "watch_updated", "product_id": payload["product_id"], "watch_status": payload.get("status", "monitoring")}, False
    raise HTTPException(status_code=400, detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Agent Arena SmartCart Mock Simulator", version="1.0.0", lifespan=lifespan)


def _tool_headers(authorization: str | None, x_task_id: str | None, is_action: bool, tool_name: str, payload: dict[str, Any]):
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        if is_action:
            mutated, resp, was_rejection = run_action_tool(tool_name, world_state, payload, set())
            if mutated is not None and not was_rejection:
                update_world_state(conn, assignment_id, mutated)
        else:
            resp = run_read_tool(tool_name, world_state, payload)
            was_rejection = False
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(conn, session_id, task_id, submission_id, tool_name, payload, resp, was_rejection, latency_ms)
        return resp
    finally:
        conn.close()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "mode": "mock", "reveal_ground_truth": REVEAL_GROUND_TRUTH}


@app.post("/dev/reset")
def dev_reset(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        with conn:
            if not authorization:
                conn.execute("DELETE FROM mock_task_assignments")
                conn.execute("DELETE FROM mock_tool_call_logs")
                conn.execute("DELETE FROM mock_submissions")
                conn.execute("DELETE FROM mock_task_evaluations")
            else:
                conn.execute("DELETE FROM mock_task_assignments WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM mock_tool_call_logs WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM mock_submissions WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM mock_task_evaluations WHERE session_id = ?", (session_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mock_tasks")
            count = cursor.fetchone()[0]
        return {"reset": True, "tasks_available": count}
    finally:
        conn.close()


def get_dashboard_data(conn: sqlite3.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, session_id, task_id, customer_id, customer_message,
               correct, actual_resolution, expected_resolution, actual_escalation,
               expected_escalation, actual_evidence, expected_evidence, missing_evidence,
               diff_explanation, tool_calls, submitted_at
        FROM mock_task_evaluations
        ORDER BY id DESC
    """)
    rows = cursor.fetchall()
    evaluations = []
    for r in rows:
        evaluations.append(
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "task_id": r["task_id"],
                "customer_id": r["customer_id"] or "",
                "customer_message": r["customer_message"] or "",
                "correct": bool(r["correct"]),
                "actual_resolution": r["actual_resolution"] or "",
                "expected_resolution": r["expected_resolution"] or "",
                "actual_escalation": bool(r["actual_escalation"]),
                "expected_escalation": bool(r["expected_escalation"]),
                "actual_evidence": json.loads(r["actual_evidence"]) if r["actual_evidence"] else [],
                "expected_evidence": json.loads(r["expected_evidence"]) if r["expected_evidence"] else [],
                "missing_evidence": json.loads(r["missing_evidence"]) if r["missing_evidence"] else [],
                "diff_explanation": r["diff_explanation"] or "",
                "tool_calls": json.loads(r["tool_calls"]) if r["tool_calls"] else [],
                "submitted_at": r["submitted_at"],
            }
        )

    cursor.execute("SELECT COUNT(*) FROM mock_tool_call_logs")
    total_tool_calls = cursor.fetchone()[0]

    total = len(evaluations)
    passed = sum(1 for e in evaluations if e["correct"])
    failed = total - passed
    accuracy = round((passed / total * 100), 1) if total > 0 else 0.0

    return {
        "summary": {
            "total_evaluated": total,
            "passed": passed,
            "failed": failed,
            "accuracy_percent": accuracy,
            "total_tool_calls": total_tool_calls,
        },
        "failed_tasks": [e for e in evaluations if not e["correct"]],
        "passed_tasks": [e for e in evaluations if e["correct"]],
        "all_evaluations": evaluations,
    }


def render_dashboard_html(data: dict[str, Any]) -> str:
    summary = data["summary"]
    failed_tasks = data["failed_tasks"]
    passed_tasks = data["passed_tasks"]

    total = summary["total_evaluated"]
    passed = summary["passed"]
    failed = summary["failed"]
    accuracy = summary["accuracy_percent"]
    tool_calls_count = summary["total_tool_calls"]

    failed_html = ""
    if not failed_tasks:
        failed_html = """
        <div class="card-empty">
            <p>✨ <strong>No failed tasks!</strong> Either your agent passed all evaluated tasks, or no tasks have been run yet.</p>
            <p style="margin-top: 8px; font-size: 13px; color: #8b949e;">Run <code>python main.py</code> in your participant workspace to process tasks.</p>
        </div>
        """
    else:
        for f in failed_tasks:
            tools_pills = ""
            for tc in f.get("tool_calls", []):
                tname = tc.get("tool_name", "unknown")
                lat = tc.get("latency_ms", 0)
                is_rej = tc.get("was_enforcement_rejection", 0) == 1
                badge_cls = "tool-pill rejection" if is_rej else "tool-pill"
                rej_label = " [REJECTED]" if is_rej else ""
                tools_pills += f'<span class="{badge_cls}">{tname}{rej_label} ({lat}ms)</span> '
            if not tools_pills:
                tools_pills = '<span style="color: #8b949e; font-size: 12px;">(No tools called)</span>'

            res_match = f["actual_resolution"] == f["expected_resolution"]
            res_status = (
                '<span class="match">✓ Matched</span>' if res_match else '<span class="mismatch">✗ Mismatch</span>'
            )

            esc_match = f["actual_escalation"] == f["expected_escalation"]
            esc_status = (
                '<span class="match">✓ Matched</span>' if esc_match else '<span class="mismatch">✗ Mismatch</span>'
            )

            missing_ev = f.get("missing_evidence", [])
            ev_status = (
                '<span class="match">✓ Complete</span>'
                if not missing_ev
                else f'<span class="mismatch">✗ Missing: {", ".join(missing_ev)}</span>'
            )

            submitted_display = f.get("submitted_at", "").replace("T", " ")[:19]

            failed_html += f"""
            <div class="fail-card">
                <div class="fail-header">
                    <div>
                        <span class="badge badge-fail">[FAIL]</span>
                        <span class="task-tag">{f["task_id"]}</span>
                        <span class="meta-tag">• User: {f["customer_id"]}</span>
                    </div>
                    <div class="meta-tag">Submitted: {submitted_display} UTC</div>
                </div>

                <div class="diff-alert">
                    <strong>Diff Explanation:</strong> {f["diff_explanation"]}
                </div>

                <div class="inquiry-box">
                    <strong>Objective / Trigger ({f["customer_id"]}):</strong> "{f["customer_message"]}"
                </div>

                <table class="diff-table">
                    <thead>
                        <tr>
                            <th style="width: 25%;">Field</th>
                            <th style="width: 30%;">Expected (Ground Truth)</th>
                            <th style="width: 30%;">Agent Output</th>
                            <th style="width: 15%;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>Decision (Action / Notify)</td>
                            <td><code>{f["expected_resolution"]}</code></td>
                            <td><code>{f["actual_resolution"]}</code></td>
                            <td>{res_status}</td>
                        </tr>
                        <tr>
                            <td>Notify / Alert Flag</td>
                            <td><code>{f["expected_escalation"]}</code></td>
                            <td><code>{f["actual_escalation"]}</code></td>
                            <td>{esc_status}</td>
                        </tr>
                        <tr>
                            <td>Evidence Citations</td>
                            <td><code>{", ".join(f.get("expected_evidence", [])) or "[]"}</code></td>
                            <td><code>{", ".join(f.get("actual_evidence", [])) or "[]"}</code></td>
                            <td>{ev_status}</td>
                        </tr>
                    </tbody>
                </table>

                <div class="tools-trace">
                    <span class="trace-label">Tools Executed:</span>
                    {tools_pills}
                </div>
            </div>
            """

    passed_cards = ""
    for p in passed_tasks:
        sub_time = p.get("submitted_at", "").replace("T", " ")[:19]
        ev_count = len(p.get("actual_evidence", []))
        tool_count = len(p.get("tool_calls", []))

        tools_pills = ""
        for tc in p.get("tool_calls", []):
            tname = tc.get("tool_name", "unknown")
            lat = tc.get("latency_ms", 0)
            is_rej = tc.get("was_enforcement_rejection", 0) == 1
            badge_cls = "tool-pill rejection" if is_rej else "tool-pill"
            rej_label = " [REJECTED]" if is_rej else ""
            tools_pills += f'<span class="{badge_cls}">{tname}{rej_label} ({lat}ms)</span> '
        if not tools_pills:
            tools_pills = '<span style="color: #8b949e; font-size: 12px;">(No tools called)</span>'

        passed_cards += f"""
        <div class="pass-card">
            <div class="pass-header">
                <div>
                    <span class="badge badge-pass">[PASS]</span>
                    <span class="task-tag">{p["task_id"]}</span>
                    <span class="meta-tag">• User: {p["customer_id"]}</span>
                </div>
                <div class="meta-tag">Submitted: {sub_time} UTC</div>
            </div>

            <div class="inquiry-box">
                <strong>Objective / Trigger ({p["customer_id"]}):</strong> "{p["customer_message"]}"
            </div>

            <table class="diff-table">
                <thead>
                    <tr>
                        <th style="width: 25%;">Field</th>
                        <th style="width: 35%;">Ground Truth</th>
                        <th style="width: 35%;">Agent Output</th>
                        <th style="width: 15%;">Status</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Decision</td>
                        <td><code>{p["expected_resolution"]}</code></td>
                        <td><code>{p["actual_resolution"]}</code></td>
                        <td><span class="match">✓ Matched</span></td>
                    </tr>
                    <tr>
                        <td>Notify Flag</td>
                        <td><code>{p["expected_escalation"]}</code></td>
                        <td><code>{p["actual_escalation"]}</code></td>
                        <td><span class="match">✓ Matched</span></td>
                    </tr>
                    <tr>
                        <td>Evidence Citations</td>
                        <td><code>{", ".join(p.get("expected_evidence", [])) or "[]"}</code></td>
                        <td><code>{", ".join(p.get("actual_evidence", [])) or "[]"}</code></td>
                        <td><span class="match">✓ Complete ({ev_count} items)</span></td>
                    </tr>
                </tbody>
            </table>

            <div class="tools-trace">
                <span class="trace-label">Tools Executed ({tool_count} calls):</span>
                {tools_pills}
            </div>
        </div>
        """

    if not passed_cards:
        passed_cards = '<div class="card-empty"><p>No passed tasks recorded yet.</p></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Arena SmartCart — Mock Debugger</title>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --surface-hover: #1c2128;
    --border: #30363d;
    --text: #c9d1d9;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --pass: #3fb950;
    --pass-bg: rgba(63, 185, 80, 0.15);
    --fail: #f85149;
    --fail-bg: rgba(248, 81, 73, 0.15);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background-color: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 24px;
    line-height: 1.5;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }}
  .title-group h1 {{ font-size: 22px; font-weight: 600; color: #f0f6fc; display: flex; align-items: center; gap: 8px; }}
  .title-group p {{ font-size: 13px; color: var(--text-muted); margin-top: 4px; }}
  .badge {{
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 4px;
    border: 1px solid var(--border);
  }}
  .badge-online {{ background: var(--pass-bg); color: var(--pass); border-color: rgba(63, 185, 80, 0.3); }}
  .badge-fail {{ background: var(--fail-bg); color: var(--fail); border-color: rgba(248, 81, 73, 0.3); }}
  .badge-pass {{ background: var(--pass-bg); color: var(--pass); border-color: rgba(63, 185, 80, 0.3); }}
  .controls {{ display: flex; align-items: center; gap: 10px; }}
  button {{
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 500;
  }}
  button:hover {{ background: var(--surface-hover); border-color: #8b949e; }}
  .btn-danger {{ color: #f85149; border-color: rgba(248, 81, 73, 0.4); }}
  .btn-danger:hover {{ background: var(--fail-bg); }}

  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }}
  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }}
  .stat-card .label {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }}
  .stat-card .value {{ font-size: 26px; font-weight: 700; color: #f0f6fc; margin-top: 4px; }}

  .section-title {{ font-size: 17px; font-weight: 600; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; color: #f0f6fc; }}

  .card-empty {{
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 32px;
    text-align: center;
    color: var(--text-muted);
    font-size: 14px;
    margin-bottom: 24px;
  }}

  .fail-card {{
    background: var(--surface);
    border: 1px solid rgba(248, 81, 73, 0.35);
    border-left: 4px solid var(--fail);
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 16px;
  }}
  .fail-header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }}
  .pass-card {{
    background: var(--surface);
    border: 1px solid rgba(63, 185, 80, 0.35);
    border-left: 4px solid var(--pass);
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 16px;
  }}
  .pass-header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }}
  .task-tag {{ font-family: monospace; font-size: 14px; font-weight: 600; color: #f0f6fc; margin-left: 4px; }}
  .meta-tag {{ font-size: 12px; color: var(--text-muted); }}

  .diff-alert {{
    background: var(--fail-bg);
    border: 1px solid rgba(248, 81, 73, 0.3);
    border-radius: 6px;
    padding: 10px 14px;
    color: #ff7b72;
    font-size: 13px;
    margin-bottom: 12px;
  }}

  .inquiry-box {{
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    font-size: 13px;
    color: #8b949e;
    margin-bottom: 12px;
  }}
  .inquiry-box strong {{ color: var(--text); }}

  .diff-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin-bottom: 12px;
  }}
  .diff-table th, .diff-table td {{
    padding: 7px 10px;
    border: 1px solid var(--border);
    text-align: left;
  }}
  .diff-table th {{ background: #161b22; color: var(--text-muted); font-weight: 500; }}
  .mismatch {{ color: #f85149; font-weight: 600; }}
  .match {{ color: #3fb950; font-weight: 600; }}

  .tools-trace {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    margin-top: 10px;
  }}
  .trace-label {{ font-size: 12px; color: var(--text-muted); font-weight: 500; }}
  .tool-pill {{
    font-family: monospace;
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 4px;
    background: #21262d;
    border: 1px solid var(--border);
    color: var(--accent);
  }}
  .tool-pill.rejection {{ background: var(--fail-bg); color: #f85149; border-color: rgba(248, 81, 73, 0.4); }}

  details {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 18px;
    margin-top: 24px;
  }}
  details summary {{
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    color: var(--text);
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title-group">
      <h1>🛒 SmartCart Mock Simulator — Agent Debugger</h1>
      <p>Local Ground Truth Verification & Evaluation Debugger • No Auth Token Required</p>
    </div>
    <div class="controls">
      <span class="badge badge-online">🟢 Online (Practice Mode)</span>
      <button id="toggle-auto">⚡ Auto-Refresh: ON</button>
      <button onclick="location.reload()">🔄 Refresh</button>
      <button class="btn-danger" onclick="resetHistory()">🗑️ Reset History</button>
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="label">Evaluations Attempted</div>
      <div class="value">{total} / 30</div>
    </div>
    <div class="stat-card">
      <div class="label">Tasks Passed</div>
      <div class="value" style="color: var(--pass);">{passed}</div>
    </div>
    <div class="stat-card">
      <div class="label">Tasks Failed</div>
      <div class="value" style="color: var(--fail);">{failed}</div>
    </div>
    <div class="stat-card">
      <div class="label">Score Report</div>
      <div class="value" style="font-size: 22px;">{passed}/{total} ({accuracy}%)</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Tool Calls</div>
      <div class="value">{tool_calls_count}</div>
    </div>
  </div>

  <div class="section-title">
    <span>🚨 Failed Tasks ({failed}) — Debug Trace & Ground Truth Mismatches</span>
  </div>

  {failed_html}

  <details open style="margin-top: 24px;">
    <summary style="font-size: 16px; font-weight: 600; cursor: pointer; color: var(--text);">
      ✅ Passed Tasks ({passed}) — Inspect Successful Resolutions & Traces
    </summary>
    <div style="margin-top: 14px;">
      {passed_cards}
    </div>
  </details>
</div>

<script>
  let autoRefresh = localStorage.getItem('mock_auto_refresh') !== 'false';
  const toggleBtn = document.getElementById('toggle-auto');
  function updateToggle() {{
    toggleBtn.textContent = autoRefresh ? '⚡ Auto-Refresh: ON' : '⏸️ Auto-Refresh: OFF';
    toggleBtn.style.color = autoRefresh ? '#3fb950' : '#8b949e';
  }}
  toggleBtn.onclick = () => {{
    autoRefresh = !autoRefresh;
    localStorage.setItem('mock_auto_refresh', autoRefresh);
    updateToggle();
  }};
  updateToggle();
  setInterval(() => {{
    if (autoRefresh) location.reload();
  }}, 3000);

  function resetHistory() {{
    if (confirm("Reset all task evaluations and tool call history in the mock simulator?")) {{
      fetch("/dev/reset", {{method: "POST"}}).then(() => location.reload());
    }}
  }}
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard() -> HTMLResponse:
    """Exposes the standalone visual debug dashboard for evaluating local agent performance."""
    conn = get_db_connection()
    try:
        data = get_dashboard_data(conn)
        html_content = render_dashboard_html(data)
        return HTMLResponse(content=html_content)
    finally:
        conn.close()


@app.get("/api/dashboard")
def get_dashboard_api() -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_dashboard_data(conn)
    finally:
        conn.close()


@app.get("/", response_class=RedirectResponse)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.post("/tools/get_user_profile")
def get_user_profile(req: GetUserProfileRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "get_user_profile", req.model_dump())

@app.post("/tools/get_saved_products")
def get_saved_products(req: GetSavedProductsRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "get_saved_products", req.model_dump())

@app.post("/tools/get_product")
def get_product(req: GetProductRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "get_product", req.model_dump())

@app.post("/tools/get_price_history")
def get_price_history(req: GetPriceHistoryRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "get_price_history", req.model_dump())

@app.post("/tools/get_available_offers")
def get_available_offers(req: GetAvailableOffersRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "get_available_offers", req.model_dump())

@app.post("/tools/check_offer_eligibility")
def check_offer_eligibility_route(req: CheckOfferEligibilityRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "check_offer_eligibility", req.model_dump())

@app.post("/tools/get_availability")
def get_availability(req: GetAvailabilityRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, False, "get_availability", req.model_dump())

@app.post("/tools/send_notification")
def send_notification(req: SendNotificationRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, True, "send_notification", req.model_dump())

@app.post("/tools/update_watch_status")
def update_watch_status(req: UpdateWatchStatusRequest, authorization: str | None = Header(default=None), x_task_id: str | None = Header(default=None)):
    return _tool_headers(authorization, x_task_id, True, "update_watch_status", req.model_dump())


def _task_start_payload(inp: dict[str, Any], task_id: str) -> TaskStartResponse:
    return TaskStartResponse(
        task_id=task_id,
        user_id=inp.get("user_id", ""),
        product_id=inp.get("product_id", ""),
        objective=inp.get("objective", "Determine whether a meaningful purchasing opportunity exists."),
        trigger=inp.get("trigger", {}),
    )


@app.post("/task/start", response_model=TaskStartResponse)
def start_task(authorization: str | None = Header(default=None)) -> TaskStartResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT submission_id FROM mock_submissions WHERE session_id = ? AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1", (session_id,))
        sub_row = cursor.fetchone()
        active_sub_id = sub_row["submission_id"] if sub_row else None
        if active_sub_id:
            cursor.execute("SELECT t.task_id, t.input_payload, t.world_state_seed FROM mock_tasks t WHERE t.task_id NOT IN (SELECT task_id FROM mock_task_assignments WHERE submission_id = ?) ORDER BY t.task_id ASC LIMIT 1", (active_sub_id,))
        else:
            cursor.execute("SELECT t.task_id, t.input_payload, t.world_state_seed FROM mock_tasks t WHERE t.task_id NOT IN (SELECT task_id FROM mock_task_assignments WHERE session_id = ? AND submission_id IS NULL) ORDER BY t.task_id ASC LIMIT 1", (session_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "NO_MORE_TASKS", "message": "All tasks have been assigned. Finalize submission or call /dev/reset."})
        task_id = row["task_id"]
        input_payload = json.loads(row["input_payload"])
        world_seed = json.loads(row["world_state_seed"])
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute("INSERT INTO mock_task_assignments (session_id, task_id, submission_id, assigned_at, world_runtime_state) VALUES (?, ?, ?, ?, ?)", (session_id, task_id, active_sub_id, now_iso, json.dumps(world_seed)))
        return _task_start_payload(input_payload, task_id)
    finally:
        conn.close()


def _grade(decision: ShoppingDecision, evidence: list[str], gt: dict[str, Any]) -> tuple[bool, str, str, list[str], list[str]]:
    expected = gt.get("expected_decision") or {}
    exp_notify = bool(expected.get("notify_user", False))
    exp_opp = bool(expected.get("opportunity_detected", False))
    req_ev = gt.get("required_evidence") or gt.get("expected_evidence_ids") or []
    missing = [e for e in req_ev if e not in set(evidence)]
    notify_ok = decision.notify_user == exp_notify
    opp_ok = decision.opportunity_detected == exp_opp
    correct = notify_ok and opp_ok and len(missing) == 0
    parts = []
    if not opp_ok:
        parts.append(f"Opportunity mismatch: expected {exp_opp}, got {decision.opportunity_detected}.")
    if not notify_ok:
        parts.append(f"Notify mismatch: expected {exp_notify}, got {decision.notify_user}.")
    if missing:
        parts.append(f"Missing required evidence IDs: {missing}.")
    if correct:
        parts.append("Decision and required evidence match ground truth.")
    label = "notify" if exp_notify else "watch"
    return correct, label, " ".join(parts), req_ev, missing


@app.post("/task/submit", response_model_exclude_none=True)
def submit_task(req: TaskSubmitRequest, authorization: str | None = Header(default=None)):
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _aid, active_task_id, _state, submission_id = get_active_assignment(conn, session_id, task_id=req.task_id)
        if active_task_id != req.task_id:
            raise HTTPException(status_code=409, detail={"error": "TASK_ID_MISMATCH", "message": f"Active task is '{active_task_id}', cannot submit '{req.task_id}'."})
        if not REVEAL_GROUND_TRUTH:
            return TaskSubmitResponse(received=True, task_id=req.task_id)
        cursor = conn.cursor()
        cursor.execute("SELECT ground_truth_privileged, input_payload FROM mock_tasks WHERE task_id = ?", (req.task_id,))
        row = cursor.fetchone()
        if not row:
            return TaskSubmitResponse(received=True, task_id=req.task_id)
        gt = json.loads(row["ground_truth_privileged"])
        inp = json.loads(row["input_payload"])
        correct, label, diff_str, req_ev, missing = _grade(req.decision, req.evidence, gt)
        cursor.execute(
            """
            SELECT tool_name, was_enforcement_rejection, latency_ms
            FROM mock_tool_call_logs
            WHERE session_id = ? AND task_id = ?
            ORDER BY id ASC
            """,
            (session_id, req.task_id),
        )
        tool_logs = [dict(r) for r in cursor.fetchall()]

        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                """INSERT INTO mock_task_evaluations (session_id, task_id, customer_id, customer_message, correct, actual_resolution, expected_resolution, actual_escalation, expected_escalation, actual_evidence, expected_evidence, missing_evidence, diff_explanation, tool_calls, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, req.task_id, inp.get("user_id", ""), inp.get("objective", ""), 1 if correct else 0, "notify" if req.decision.notify_user else "watch", label, 1 if req.decision.notify_user else 0, 1 if label == "notify" else 0, json.dumps(req.evidence), json.dumps(req_ev), json.dumps(missing), diff_str, json.dumps(tool_logs), now_iso),
            )
            if submission_id:
                cursor.execute("SELECT per_task_results FROM mock_submissions WHERE submission_id = ?", (submission_id,))
                sub_row = cursor.fetchone()
                if sub_row:
                    current_results = json.loads(sub_row["per_task_results"]) if sub_row["per_task_results"] else []
                    current_results.append({"task_id": req.task_id, "correct": correct})
                    conn.execute("UPDATE mock_submissions SET per_task_results = ? WHERE submission_id = ?", (json.dumps(current_results), submission_id))
        return TaskSubmitPracticeResponse(received=True, task_id=req.task_id, correct=correct, expected_resolution=label, expected_evidence=req_ev, your_evidence=req.evidence, diff_explanation=diff_str)
    finally:
        conn.close()


@app.post("/submission/start", response_model=SubmissionStartResponse)
def start_submission(authorization: str | None = Header(default=None)) -> SubmissionStartResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT submission_id FROM mock_submissions WHERE session_id = ? AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1", (session_id,))
        existing = cursor.fetchone()
        if existing:
            raise HTTPException(status_code=409, detail={"error": "ACTIVE_SUBMISSION_EXISTS", "message": f"Active submission '{existing['submission_id']}' is already in progress. Finalize it first.", "submission_id": existing["submission_id"]})
        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM mock_submissions WHERE session_id = ?", (session_id,))
        sub_count = cursor.fetchone()[0]
        cursor.execute("SELECT task_id, input_payload, world_state_seed FROM mock_tasks ORDER BY task_id ASC")
        task_rows = cursor.fetchall()
        sub_id = hashlib.sha256(f"{session_id}-{sub_count + 1}-{time.time()}".encode("utf-8")).hexdigest()[:32]
        now_iso = datetime.now(UTC).isoformat()
        task_items = []
        with conn:
            conn.execute("INSERT INTO mock_submissions (submission_id, session_id, attempt_number, status, started_at, per_task_results) VALUES (?, ?, ?, ?, ?, ?)", (sub_id, session_id, sub_count + 1, "in_progress", now_iso, json.dumps([])))
            for row in task_rows:
                tid = row["task_id"]
                inp = json.loads(row["input_payload"])
                w_seed = json.loads(row["world_state_seed"])
                conn.execute("INSERT INTO mock_task_assignments (session_id, task_id, submission_id, assigned_at, world_runtime_state) VALUES (?, ?, ?, ?, ?)", (session_id, tid, sub_id, now_iso, json.dumps(w_seed)))
                task_items.append(_task_start_payload(inp, tid))
        return SubmissionStartResponse(submission_id=sub_id, attempt_number=sub_count + 1, tasks_total=total_tasks, tasks=task_items)
    finally:
        conn.close()


@app.post("/submission/{submission_id}/submit", response_model=BatchSubmissionSubmitResponse)
@app.post("/submission/{submission_id}/submit_batch", response_model=BatchSubmissionSubmitResponse)
def submit_submission_batch(submission_id: str, req: BatchSubmissionSubmitRequest, authorization: str | None = Header(default=None)):
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM mock_submissions WHERE submission_id = ? AND session_id = ?", (submission_id, session_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."})
        if row["status"] != "in_progress":
            raise HTTPException(status_code=409, detail={"error": "SUBMISSION_CLOSED", "message": f"Submission is already {row['status']}."})
        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        cursor.execute("SELECT task_id, input_payload, ground_truth_privileged FROM mock_tasks")
        gt_map = {r["task_id"]: (json.loads(r["input_payload"]), json.loads(r["ground_truth_privileged"])) for r in cursor.fetchall()}
        results = []
        passed_count = 0
        now_iso = datetime.now(UTC).isoformat()
        for item in req.answers:
            meta = gt_map.get(item.task_id)
            if not meta:
                continue
            inp, gt = meta
            correct, label, diff_str, req_ev, missing = _grade(item.decision, item.evidence, gt)
            if correct:
                passed_count += 1
            results.append({"task_id": item.task_id, "correct": correct, "diff": diff_str})
            cursor.execute(
                """
                SELECT tool_name, was_enforcement_rejection, latency_ms
                FROM mock_tool_call_logs
                WHERE session_id = ? AND task_id = ?
                ORDER BY id ASC
                """,
                (session_id, item.task_id),
            )
            item_tool_logs = [dict(r) for r in cursor.fetchall()]

            conn.execute(
                """INSERT INTO mock_task_evaluations (session_id, task_id, customer_id, customer_message, correct, actual_resolution, expected_resolution, actual_escalation, expected_escalation, actual_evidence, expected_evidence, missing_evidence, diff_explanation, tool_calls, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, item.task_id, inp.get("user_id", ""), inp.get("objective", ""), 1 if correct else 0, "notify" if item.decision.notify_user else "watch", label, 1 if item.decision.notify_user else 0, 1 if label == "notify" else 0, json.dumps(item.evidence), json.dumps(req_ev), json.dumps(missing), diff_str, json.dumps(item_tool_logs), now_iso),
            )
        score_pct = round((passed_count / len(results)) * 100.0, 2) if results else 0.0
        with conn:
            conn.execute("UPDATE mock_submissions SET status = 'completed', completed_at = ?, per_task_results = ? WHERE submission_id = ?", (now_iso, json.dumps(results), submission_id))
        return BatchSubmissionSubmitResponse(submission_id=submission_id, status="completed", tasks_submitted=len(results), tasks_total=total_tasks, score_pct=score_pct, passed=score_pct >= 70.0, inter_task_durations=[])
    finally:
        conn.close()


@app.post("/submission/{submission_id}/abort", response_model=SubmissionAbortResponse)
def abort_submission(submission_id: str, authorization: str | None = Header(default=None)):
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute("UPDATE mock_submissions SET status = 'interrupted', completed_at = ? WHERE submission_id = ? AND session_id = ?", (now_iso, submission_id, session_id))
        return SubmissionAbortResponse(submission_id=submission_id, status="interrupted", message=f"Submission '{submission_id}' was aborted and will not count against submission limit.")
    finally:
        conn.close()


@app.get("/submission/{submission_id}/status", response_model=SubmissionStatusResponse)
def get_submission_status(submission_id: str, authorization: str | None = Header(default=None)):
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status, per_task_results FROM mock_submissions WHERE submission_id = ? AND session_id = ?", (submission_id, session_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."})
        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        results = json.loads(row["per_task_results"])
        return SubmissionStatusResponse(status=row["status"], tasks_completed=len(results), tasks_total=total_tasks, time_remaining_seconds=1800)
    finally:
        conn.close()


@app.post("/submission/{submission_id}/finalize", response_model=SubmissionFinalizeResponse)
def finalize_submission(submission_id: str, authorization: str | None = Header(default=None)):
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute("UPDATE mock_submissions SET status = 'completed', completed_at = ? WHERE submission_id = ? AND session_id = ?", (now_iso, submission_id, session_id))
        return SubmissionFinalizeResponse(submission_id=submission_id, status="completed")
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent Arena SmartCart Mock Simulator")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8001")), help="Port to listen on (default: 8001)")
    parser.add_argument("--host", type=str, default=os.getenv("HOST", "127.0.0.1"), help="Host to listen on (default: 127.0.0.1)")
    args, _ = parser.parse_known_args()
    port = args.port
    host = args.host
    print(f"Starting Agent Arena SmartCart Mock Simulator on http://{host}:{port} (REVEAL_GROUND_TRUTH={REVEAL_GROUND_TRUTH})")
    uvicorn.run(app, host=host, port=port)
