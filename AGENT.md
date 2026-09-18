# Agent Implementation Guide — SmartCart

> **File to Edit:** `agent.py`  
> **Goal:** Build an autonomous shopping monitoring agent that evaluates price events, checks offer eligibility, respects user budgets and preferences, and sends timely notifications.

---

## 1. Quick Start

In this challenge, **`agent.py` is the only file you modify**. The orchestration harness (`main.py`) calls your `solve()` function for every task.

```python
def solve(
    task: dict[str, Any],
    tools: ToolsClient,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    ...
```

---

## 2. What Your Agent Receives (`task`)

Each `task` dictionary contains:

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `task_id` | `str` | Unique ID for the current task | `"TASK-SC-001"` |
| `user_id` | `str` | User who saved the product | `"USR-1001"` |
| `product_id` | `str` | Monitored product ID | `"PROD-5001"` |
| `objective` | `str` | Natural language goal | `"Monitor price and notify if deal qualifies"` |
| `trigger` | `dict` | Event trigger details | `{"type": "PRICE_CHANGED", "product_id": "PROD-5001"}` |

### Trigger Types:
`PRICE_CHANGED`, `OFFER_ADDED`, `OFFER_CHANGED`, `OFFER_EXPIRED`, `BANK_OFFER_CHANGED`, `BACK_IN_STOCK`, `OFFER_EXPIRING`, `PERIODIC_REEVALUATION`

---

## 3. Available Tools (`tools`)

Your `tools` object provides **9 methods** (7 read tools, 2 action tools):

### Read Tools (Information Gathering)
```python
# User preferences (budget_limit, minimum_saving, preferred_brands, bank_cards, notification_preferences)
user = tools.get_user_profile(user_id="USR-1001")

# List of product IDs saved by user
saved = tools.get_saved_products(user_id="USR-1001")

# Product info (name, brand, current_price, mrp, saved_price)
product = tools.get_product(product_id="PROD-5001")

# Historical price history (timestamps and prices)
history = tools.get_price_history(product_id="PROD-5001", days=30)

# All available offers for this product and user
offers = tools.get_available_offers(product_id="PROD-5001", user_id="USR-1001")

# Verify if a specific offer is eligible for this user and product
eligibility = tools.check_offer_eligibility(product_id="PROD-5001", user_id="USR-1001", offer_id="OFFER-101")

# Check stock availability ("in_stock", "low_stock", "out_of_stock")
stock = tools.get_availability(product_id="PROD-5001")
```

### Action Tools (Server-Side Enforced)
```python
# Send notification to user (enforced: product must be saved, user opted in, in stock)
tools.send_notification(
    user_id="USR-1001",
    product_id="PROD-5001",
    title="Price Drop Alert!",
    message="Sony WH-1000XM5 is now $279, saving you $70 below your budget!"
)

# Update watch status for a product ("monitoring", "paused", "stopped")
tools.update_watch_status(product_id="PROD-5001", status="monitoring")
```

---

## 4. What Your Agent Must Return

Your `solve()` function must return a structured dictionary:

```python
return {
    "task_id": task["task_id"],           # Required: match task_id
    "decision": {
        "opportunity_detected": True,     # True if genuine deal qualifies
        "notify_user": True,              # True ONLY if you called send_notification()
    },
    "evidence": [                         # Entity IDs retrieved via tools
        "PROD-5001",
        "OFFER-101",
        "USR-1001"
    ],
    "reason": "Price dropped to $279 after applying stackable coupon OFFER-101, beating budget by $70.",
    "customer_notification": {            # Required if notify_user=True; None otherwise
        "title": "Price Drop Alert: Sony Headphones",
        "message": "Effective price is now $279 with coupon applied. Meets your minimum savings requirement!",
    },
    "confidence": 0.95,                   # Float between 0.0 and 1.0
}
```

---

## 5. Recommended Implementation Pattern

```python
def solve(task, tools, api_key=None, model=None, base_url=None):
    task_id = task.get("task_id", "")
    user_id = task.get("user_id", "")
    product_id = task.get("product_id", "")
    evidence = [product_id, user_id]

    # Step 1: Check basic pre-conditions
    user = tools.get_user_profile(user_id)
    saved_prods = tools.get_saved_products(user_id)
    stock = tools.get_availability(product_id)

    # Product must be in user's saved list and in stock
    if product_id not in saved_prods.get("products", []) or stock.get("availability") == "out_of_stock":
        return {
            "task_id": task_id,
            "decision": {"opportunity_detected": False, "notify_user": False},
            "evidence": evidence,
            "reason": "Product out of stock or not in watchlist",
            "customer_notification": None,
            "confidence": 0.9,
        }

    # Step 2: Compute effective price
    product = tools.get_product(product_id)
    offers = tools.get_available_offers(product_id, user_id)
    # Check eligibility of offers and calculate stackable vs exclusive discounts...

    # Step 3: Evaluate against user budget and savings thresholds
    # effective_price <= user["budget_limit"]
    # saving >= user["minimum_saving"] OR effective_price <= historical_low

    # Step 4: Check notification preferences
    # notify_on_price_drop, notify_on_coupon, notify_on_stock_change

    # Step 5: Send notification if qualified, then return response
    if qualified_deal:
        tools.send_notification(user_id, product_id, title="...", message="...")
        return {
            "task_id": task_id,
            "decision": {"opportunity_detected": True, "notify_user": True},
            "evidence": evidence,
            "reason": "Price drop qualifies within budget",
            "customer_notification": {"title": "...", "message": "..."},
            "confidence": 0.95,
        }
    return {
        "task_id": task_id,
        "decision": {"opportunity_detected": False, "notify_user": False},
        "evidence": evidence,
        "reason": "Deal does not meet user savings threshold",
        "customer_notification": None,
        "confidence": 0.9,
    }
```

---

## 6. Golden Rules for Maximum Score

1. **Always Check Stock First:** Never notify on out-of-stock products (`get_availability`).
2. **Respect Notification Opt-Ins:** If a user disabled `notify_on_price_drop`, do not notify on price drops even if it is a great deal.
3. **Verify Offer Eligibility:** Never assume an offer applies. Check with `check_offer_eligibility()` (checks bank card match, min spend, brand match).
4. **Calculate Effective Price Accurately:** Stackable offers add together; non-stackable offers only apply the single largest discount.
5. **Ground Evidence:** Include observed IDs (`PROD-xxx`, `OFFER-xxx`, `USR-xxx`) in the `evidence` list.
6. **Stay Under Budget:** 40 tool calls max per task.

---

## 7. How to Test Your Agent

```bash
# 1. Start mock simulator:
python mock_simulator/server.py

# 2. Run your agent:
python main.py
```

Inspect live results at: `http://127.0.0.1:8001/dashboard`.
