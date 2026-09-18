# Problem Statement — PS: SmartCart Agent Arena

> **Version:** 2.0 — Authoritative Reference **Domain:** Intelligent Shopping Assistant and Price Monitoring **Benchmark Tasks:** 30 tasks per submission **Tool Budget:** 40 tool calls per task

---

## 1\. Overview and Mission

You are building an **autonomous AI agent** that operates as an intelligent shopping monitoring assistant.

Users add products to their watchlist with personal budget and savings preferences. Your agent is triggered by real-time marketplace events (price drops, new coupons, stock changes, etc.) and must autonomously decide:

1. **Investigate** the trigger — look up the product, current price, available offers, offer eligibility, price history, user preferences, and product availability.  
2. **Evaluate** whether a genuine saving opportunity exists for this specific user given their budget and minimum savings constraints.  
3. **Decide** whether to notify the user (and send the notification) or take no action.  
4. **Submit** a structured response containing your opportunity assessment, evidence, and notification details.

The server enforces all notification eligibility rules server-side. Every `send_notification` call is validated. You cannot send notifications to users who have opted out, for products not in their watchlist, or for products that are out of stock.

---

## 2\. Task Input Format

Each task begins when `main.py` calls `agent.solve(task, tools)`.

> **Note:** Unlike PS1/PS3, `solve(task, tools)` has **no** `api_key`, `model`, or `base_url` parameters.

The `task` dict has the following structure:

```py
task = {
    "task_id": "TASK-SC-001",       # Unique identifier — required in submission
    "user_id": str,                  # ID of the watchlisting user (e.g., "USR-1001")
    "product_id": str,               # ID of the monitored product (e.g., "PROD-5001")
    "objective": str,                # High-level objective string
    "trigger": {                     # The event that activated the agent
        "type": str,                 # One of the TRIGGER_TYPES listed below
        "product_id": str,           # Product that triggered the event
        "offer_id": str | None,      # Offer ID if trigger is offer-related
    }
}
```

---

## 3\. Monitoring Trigger Types

The `trigger.type` field indicates what event activated your agent. A trigger does **not** by itself mean a notification should be sent — you must investigate and validate the opportunity first.

| Trigger Type | Meaning |
| :---- | :---- |
| `PRICE_CHANGED` | Current product price was updated |
| `OFFER_ADDED` | A new coupon or offer was added for this product |
| `OFFER_CHANGED` | An existing offer was modified |
| `OFFER_EXPIRED` | An offer has just expired |
| `BANK_OFFER_CHANGED` | A bank/card-specific offer changed |
| `BACK_IN_STOCK` | Product came back into stock |
| `OFFER_EXPIRING` | An offer is about to expire (within hours) |
| `PERIODIC_REEVALUATION` | Scheduled re-evaluation — no new event, agent re-checks |

---

## 4\. Tool Catalog — 9 Tools

Your `tools` object (a `ToolsClient` instance) provides exactly 9 methods.

### 4A. Read Tools (7 tools — retrieve information)

| Tool | Signature | Returns |
| :---- | :---- | :---- |
| `get_user_profile` | `(user_id: str)` | User profile: name, budget, min savings, preferred brands, bank cards, notification preferences |
| `get_saved_products` | `(user_id: str)` | List of product IDs the user has saved to watchlist |
| `get_product` | `(product_id: str)` | Product details: name, brand, current price, MRP, availability |
| `get_price_history` | `(product_id: str, days: int = 30)` | Historical price points over N days |
| `get_available_offers` | `(product_id: str, user_id: str)` | All offers applicable to this product for this user |
| `check_offer_eligibility` | `(product_id: str, user_id: str, offer_id: str)` | Whether a specific offer is currently eligible for this user+product |
| `get_availability` | `(product_id: str)` | Real-time stock/availability status |

### 4B. Action Tools (2 tools — execute decisions, server-enforced)

| Tool | Signature | Server Enforcement |
| :---- | :---- | :---- |
| `send_notification` | `(user_id: str, product_id: str, title: str, message: str)` | Product must be in user's watchlist; user must be opted in |
| `update_watch_status` | `(product_id: str, status: str = "monitoring")` | Accepted: `"monitoring"`, `"paused"`, `"stopped"` |

> **Tool Budget:** Maximum **40 tool calls per task**. Exceeding the budget scores Efficiency \= 0.0 for that task.

---

## 5\. Opportunity Evaluation Rules (Server-Side Enforced)

The server computes ground truth using the canonical `evaluate_opportunity` function. Your agent's job is to replicate this logic.

### 5.1 Pre-Conditions (all must pass before notifying)

1. **Product must be in user's watchlist** — `get_saved_products(user_id)` must include `product_id`.  
2. **Product must be in stock** — `availability` must be `"in_stock"` or `"low_stock"`. Never notify if `"out_of_stock"`.  
3. **User must have opted in** — At least one of `notify_on_price_drop`, `notify_on_coupon`, `notify_on_deal` must be `True`.

### 5.2 Effective Price Calculation

```
effective_price = current_price
                - sum(all valid stackable offer discounts)
                - max(largest valid non-stackable exclusive discount)
```

- **Stackable offers** (`stackable: True`): All valid stackable discounts are summed.  
- **Non-stackable offers** (`stackable: False`): Only the single largest exclusive discount applies.  
- **Offer eligibility** requires: not expired, not future-dated, product match, user eligibility type.

### 5.3 Opportunity Detection Logic

A notification opportunity exists **only when ALL** are true:

1. **Within budget** — `effective_price <= user.budget_limit`  
2. **Meaningful saving** — `(saved_price - effective_price) >= user.minimum_saving` OR `effective_price <= historical_low`  
3. **Not misleading discount** — not flagged as misleading MRP discount (large MRP cut but price not genuinely exceptional vs history)

### 5.4 Special Trigger Handling

| Trigger | Special Rule |
| :---- | :---- |
| `BACK_IN_STOCK` | Notify if `notify_on_stock_change = True` AND within budget (savings threshold not required) |
| `OFFER_EXPIRED` | Never notify (offer is gone) |
| `PERIODIC_REEVALUATION` | Standard evaluation — notify only if meaningful opportunity exists |

### 5.5 Notification Preference Gating

Even if an opportunity exists, notification may be suppressed:

- `PRICE_CHANGED` trigger \+ `notify_on_price_drop = False` → suppress notification  
- `OFFER_ADDED`/`OFFER_CHANGED` trigger \+ `notify_on_coupon = False` → suppress notification

### 5.6 Offer Eligibility Types

| Eligibility Value | Rule |
| :---- | :---- |
| `"all_users"` | Any user qualifies |
| `"preferred_brand"` | User's `preferred_brands` must include the product's `brand` |
| `"bank_card"` | User's `bank_cards` must include the offer's `required_card` |
| `"min_spend"` | Product's `current_price` must be \>= offer's `min_spend` |
| `"specific_users"` | User's `user_id` must be in offer's `user_ids` list |

---

## 6\. Output Contract — Required Submission Format

Call `tools.submit_task(...)` with the following fields:

```py
tools.submit_task(
    task_id=task["task_id"],           # str — REQUIRED

    decision={
        "opportunity_detected": bool,  # REQUIRED — True if a genuine opportunity was found
        "notify_user": bool,           # REQUIRED — True if you sent a notification
    },

    evidence=[ str, ... ],             # REQUIRED — list of offer/product/user IDs you retrieved

    reason=str,                        # RECOMMENDED — brief explanation of the decision

    customer_notification={            # REQUIRED if notify_user = True
        "title": str,                  # Notification title (1-200 chars)
        "message": str,                # Notification message (1-4000 chars)
    },

    confidence=float,                  # REQUIRED — 0.0 to 1.0
)
```

### Field Validation Rules

| Field | Type | Constraints |
| :---- | :---- | :---- |
| `task_id` | `str` | Must match the current task |
| `decision.opportunity_detected` | `bool` | `True` or `False` |
| `decision.notify_user` | `bool` | `True` only if `send_notification` was successfully called |
| `evidence` | `list[str]` | Max 100 items. Only cite IDs you actually retrieved. |
| `customer_notification.title` | `str` | 1-200 characters, required if `notify_user = True` |
| `customer_notification.message` | `str` | 1-4,000 characters, required if `notify_user = True` |
| `confidence` | `float` | `0.0 <= confidence <= 1.0` |

---

## 7\. Scoring — 7 Dimensions

```
S_total = 0.35 * S_task + 0.15 * S_policy + 0.15 * S_evidence +
          0.10 * S_calibration + 0.10 * S_efficiency +
          0.10 * S_communication + 0.05 * S_robustness
```

### 7.1 Task Success — 35%

**Binary: 1.0 or 0.0. No partial credit.**

Your submitted `decision.notify_user` must exactly match the ground truth expected notification decision, AND if a notification was sent, it must have been successfully delivered (server confirmed).

### 7.2 Policy Adherence — 15%

```
S_policy = max(0.0, 1.0 - 0.25 * N_rejections) * C_truthfulness
```

- **N\_rejections**: Number of action tool calls rejected by the server. Each costs 0.25. 4 or more \= 0\.  
- **C\_truthfulness**: 1.0 if your `notify_user` matches the actual notification state; 0.0 otherwise.

### 7.3 Evidence Grounding — 15%

```
S_evidence = F1 = (2 * Precision * Recall) / (Precision + Recall)
```

- Only IDs you actually retrieved from tool responses count as observed.  
- Fabricated IDs (cited but never returned by tools) \= False Positives \= penalty.

### 7.4 Calibration — 10%

```
S_calibration = C_align
  where C_align = confidence              (when task correct)
              or = 1.0 - confidence       (when task wrong — appropriate uncertainty rewarded)
```

### 7.5 Efficiency — 10%

```
E_budget = 1.0                      if U <= 1
         = max(0, 1 - (U-1) / B)   if 1 < U <= B
         = 0.0                      if U > B

S_efficiency = E_budget * (1 - R/U)
```

Where: U \= total tool calls, B \= 40, R \= duplicate calls.

### 7.6 Communication — 10%

4 criteria at **0.25 each** applied to `reason` and `customer_notification.message`:

| Criterion | Passes when |
| :---- | :---- |
| Structure and Length | Content is 20-5,000 characters |
| Clarity and Grounding | Contains product/offer/domain entity IDs or specific product names |
| No Unsupported Promises | Doesn't claim actions that were not executed |
| Decision Consistency | Message matches `notify_user` outcome |

### 7.7 Robustness — 5%

```
S_robustness = S_task_success
```

Passthrough of Task Success.

---

## 8\. What Participants MUST Do

- Call `get_user_profile` to understand user budget and savings preferences before deciding.  
- Call `check_offer_eligibility` to verify each offer — never assume eligibility.  
- Call `get_price_history` to compare against historical low when evaluating opportunity quality.  
- Call `get_availability` — never notify for out-of-stock products.  
- Call `send_notification` **before** setting `notify_user: True` in submission.  
- Set `confidence` honestly. If unsure whether this is a real opportunity, express lower confidence.  
- Cite only offer/product/user IDs returned in tool responses.

---

## 9\. What Participants MUST NOT Do

- **Do NOT notify users who have opted out** of all notification types — server will reject.  
- **Do NOT notify for products not in the user's watchlist** — server will reject.  
- **Do NOT send notifications for out-of-stock products** — always check availability.  
- **Do NOT hardcode notification decisions** — the server evaluates live world state.  
- **Do NOT fabricate offer IDs** — only cite IDs you actually saw in tool responses.  
- **Do NOT ignore the user's budget constraint** — notifying above-budget \= grading failure.  
- **Do NOT ignore the minimum savings threshold** — this is a core user preference.  
- **Do NOT submit `notify_user: True` without having called `send_notification`** — truthfulness check will fail.  
- **Do NOT exceed 40 tool calls per task.**  
- **Do NOT modify `main.py`, `sdk/tools_client.py`, or `mock_simulator/server.py`.**

---

## 10\. Offline Development Dataset

The `sample_data/` directory contains representative JSON datasets for local testing:

| File | Contents |
| :---- | :---- |
| `users.json` | Sample user profiles with budgets and preferences |
| `products.json` | Sample product catalog with prices and MRP |
| `price_history.json` | Historical price data per product |
| `offers.json` | Sample coupons, bank offers, and deals |
| `saved_products.json` | User watchlist associations |
| `availability.json` | Current stock status per product |
| `tasks.json` | 30 dev benchmark tasks |
| `ground_truth.json` | Ground truth for dev tasks (for local scoring) |

Run the mock simulator: `python mock_simulator/server.py` Debug dashboard: `http://127.0.0.1:8001/dashboard`

---

## 11\. Submission Mode

When `MODE=submission` in `.env`, your agent runs against the live Arena server at `SUBMISSION_ARENA_URL`.

- **30 benchmark tasks** presented sequentially.  
- No ground truth revealed during submission mode.

**Arena URL:** `https://excretory-clustered-anointer.ngrok-free.dev`

For submission setup, token generation, and how to run in submission mode, see [README.md](http://README.md).

---

## 12\. Competition Rules

1. **One submission attempt at a time.** You may not start a new submission while one is `in_progress`.  
2. **All 30 tasks must be attempted.** Missing tasks score 0\.  
3. **No oracle access.** Ground truth is never revealed during submission mode.  
4. **Server enforcement is final.** If `send_notification` is rejected, the notification was not sent.  
5. **Only `agent.py` may be modified.** All other starter-kit files are read-only.  
6. **Valid submission fields required.** Missing required fields result in task-level rejection.  
7. **Rate limits apply.** Aggressive or looping API usage may trigger throttling.

&nbsp;