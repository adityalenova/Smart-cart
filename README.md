# Agent Arena: SmartCart — Participant Starter Kit

Welcome to **Agent Arena: SmartCart**! This repository is your complete toolkit for building and benchmarking an autonomous shopping agent that monitors saved products, evaluates price drops, validates stackable bank and coupon offers, and detects genuine purchasing opportunities tailored to user constraints.

---

## 1. 5-Minute Quickstart

Get up and running locally against the offline Mock Simulator in under 5 minutes:

### Step 1: Create and Activate Virtual Environment
```bash
# Create a fresh virtual environment
python -m venv .venv

# Activate on Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Activate on Linux / macOS:
source .venv/bin/activate
```

### Step 2: Install All Dependencies
```bash
pip install -r requirements.txt
```
*(Installs both the participant runtime SDK and the local FastAPI/SQLite Mock Simulator).*

### Step 3: Configure Environment
```bash
cp .env.example .env
```
*(The default `.env` is preconfigured for offline local practice mode at `http://127.0.0.1:8001`).*

### Step 4: Launch Offline Mock Simulator (Terminal 1)
```bash
python mock_simulator/server.py --port 8001
```
Open your browser to the visual debugger dashboard:
👉 **`http://127.0.0.1:8001/dashboard`**

### Step 5: Test Your Agent in Practice Mode (Terminal 2)
```bash
# Run a single task with immediate ground-truth diff feedback:
python main.py --mode practice --once

# Or run multiple development tasks:
python main.py --mode practice --max-tasks 5
```

---

## 2. Architecture & File Responsibilities

The starter kit enforces a clean, modular boundary between the orchestration harness (`main.py`) and your AI agent (`agent.py`):

```text
┌─────────────────────────────────────────────────────────────┐
│                    main.py (Runtime Harness)                │
│  - Connects to Mock Simulator or Live Arena API             │
│  - Fetches assigned shopping tasks & configures ToolsClient │
│  - Handles LLM API key rotation & rate limit pacing         │
│  - Validates output contract schema & submits decisions     │
└──────────────────────────────┬──────────────────────────────┘
                               │ passes (task, tools)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    agent.py (Your AI Agent)                 │
│  ★ THE ONLY FILE PARTICIPANTS EDIT                          │
│  - Analyzes monitored product & user budget constraints    │
│  - Queries price history, active coupons & bank offers      │
│  - Calculates effective price & stackable discounts         │
│  - Triggers notifications/cart actions when eligible        │
│  - Returns structured Section 7 decision dictionary         │
└──────────────────────────────┬──────────────────────────────┘
                               │ returns Section 7 Dict
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    Arena API / Mock Simulator               │
│  - Verifies opportunity detection & arithmetic accuracy     │
│  - Scores submission across multi-dimensional metrics       │
└─────────────────────────────────────────────────────────────┘
```

### Repository Structure:
| File / Directory | Purpose | Participant Action |
|:---|:---|:---|
| **`agent.py`** | Your core agent logic (`solve(task, tools, ...)`). | **Edit this file only** |
| **`main.py`** | Orchestration runtime, CLI flags, and submission engine. | Do not modify |
| **`sdk/tools_client.py`** | HTTP client exposing domain tools and Arena endpoints. | Read-only SDK |
| **`mock_simulator/`** | Offline server with 30 dev tasks and visual web debugger. | Local testing |
| **`sample_data/`** | Exported CSV datasets with authoritative `expected_outputs.csv`. | Reference / analysis |
| **`.env.example`** | Environment variable configuration template. | Copy to `.env` |
| **`requirements.txt`** | Unified dependencies for runtime and simulator. | `pip install -r` |

---

## 3. Task Input Contract

When `main.py` dispatches a task to `agent.solve(task, tools)`, the `task` dictionary contains:

```python
{
    "task_id": "CART-D-0001",
    "user_id": "USR-D-0001",
    "product_id": "PROD-D-0001",
    "objective": "Determine whether a meaningful purchasing opportunity exists.",
    "trigger": {
        "event_type": "PRICE_DROP",
        "description": "Price changed from 4500.0 to 4200.0"
    }
}
```

### Key Attributes:
- **`task_id`** (`str`): Unique identifier for this evaluation opportunity.
- **`user_id`** (`str`): User ID used to query budget constraints, minimum desired savings, and notification preferences.
- **`product_id`** (`str`): Target product ID to investigate for current pricing, discounts, and inventory.
- **`objective`** (`str`): High-level evaluation goal.
- **`trigger`** (`dict`): The specific incoming trigger event that prompted this investigation (e.g. `PRICE_DROP`, `OFFER_ADDED`, `BANK_OFFER_CHANGED`, `STOCK_CHANGED`).

> **Note on Triggers:** A trigger wakes the agent to investigate. The agent must verify eligibility, price history, and user preferences before deciding whether to notify.

---

## 4. Tools Catalog (`tools: ToolsClient`)

The `tools` client exposes **9 participant tools** (7 read tools + 2 action tools). Each task has a budget of **100 tool calls** and resets automatically on every task.

### A. Read Tools (Analysis & Pricing Investigation)
Read tools inspect state without making permanent changes:

1. **`tools.get_user_profile(user_id: str) -> dict[str, Any]`**
   - Retrieves user budget limit, minimum required saving, preferred brands, and notification channel.
   - *Evidence Collected*: `USER-...`
2. **`tools.get_saved_products(user_id: str) -> dict[str, Any]`**
   - Returns the list of product IDs actively monitored on the user's watchlist.
3. **`tools.get_product(product_id: str) -> dict[str, Any]`**
   - Retrieves product title, brand, category, MRP, current listed base price, and stock status.
   - *Evidence Collected*: `PROD-...`
4. **`tools.get_price_history(product_id: str, days: int = 30) -> dict[str, Any]`**
   - Retrieves 30-day historical prices to calculate average pricing and all-time low.
   - *Evidence Collected*: `PRICE-HISTORY-...`
5. **`tools.get_available_offers(product_id: str, user_id: str) -> dict[str, Any]`**
   - Discovers candidate promotions, coupons, and instant bank card discounts.
   - *Evidence Collected*: `OFFER-...`
6. **`tools.check_offer_eligibility(product_id: str, user_id: str, offer_id: str) -> dict[str, Any]`**
   - Validates whether an offer is unexpired and matched to the user's payment cards or tier.
7. **`tools.get_availability(product_id: str) -> dict[str, Any]`**
   - Checks inventory levels and warehouse fulfillment availability (`in_stock`, `low_stock`, `out_of_stock`).

### B. Action Tools (User Engagement & Watch State)
Action tools represent outward decisions taken by the agent:

8. **`tools.send_notification(user_id: str, product_id: str, title: str, message: str) -> dict[str, Any]`**
   - Dispatches a deal alert to the user. Should only be called when all savings and budget criteria are met.
9. **`tools.update_watch_status(product_id: str, status: str = "monitoring") -> dict[str, Any]`**
   - Updates monitoring state for the product (`"monitoring"`, `"paused"`, `"stopped"`).

---

## 5. Output Contract (Section 7)

Your `agent.solve(task, tools)` function must return a structured dictionary conforming to the standard Section 7 contract:

```python
{
    "task_id": "CART-D-0001",
    "decision": {
        "opportunity_detected": True,        # bool: True if a genuine deal exists
        "notify_user": True                 # bool: True if user should be alerted
    },
    "price_analysis": {
        "current_price": 4200.0,            # Listed product base price
        "effective_price": 3700.0,          # Net price after applying valid offers
        "total_saving": 1300.0,             # MRP minus effective price
        "historical_low": 3900.0            # Lowest recorded price in 30-day history
    },
    "offer_analysis": {
        "valid_offers": ["OFFER-C101", "OFFER-B202"],   # Eligible, stackable offers
        "invalid_offers": ["OFFER-X303"]                 # Expired or non-stackable offers
    },
    "evidence": [                           # List of observed entity IDs
        "USER-USR-D-0001",
        "PROD-D-0001",
        "PRICE-HISTORY-PROD-D-0001",
        "OFFER-C101"
    ],
    "reason": (                             # Cohesive reasoning narrative
        "Effective price of ₹3700 is below user budget ₹4000 and savings exceed threshold."
    ),
    "customer_notification": {              # Customer-facing alert payload
        "title": "Opportunity: Wireless Headphones price drop",
        "message": "Effective price is now ₹3700 after coupon and bank offer, saving you ₹1300."
    },
    "confidence": 0.95                      # Calibrated confidence score (float: 0.0 to 1.0)
}
```

---

## 6. Offline Public Dataset (`sample_data/`)

The `sample_data/` directory contains CSV extracts for offline analysis, model training, and local validation:
- **`users.csv`**: Monitored user preferences (budget limits, minimum savings, preferred brands).
- **`products.csv`**: Products catalog with MRP and base pricing.
- **`price_history.csv`**: Historical pricing records for the past 30 days.
- **`offers.csv`**: Active coupons and bank promotions with stacking rules.
- **`saved_products.csv`**: User watchlist mappings.
- **`tasks.csv`**: 30 development tasks with objectives and triggers.
- **`expected_outputs.csv`**: Authoritative expected results for each development task (`opportunity_detected`, `notify_user`, `effective_price`, `total_saving`, `classification`).

> **CRITICAL COMPETITION NOTE:**
> - The mock dataset (Seed 1000) and live competition dataset (Seed 50000+) are **completely disjoint**.
> - Live evaluation features new products, dynamic offer combinations, and unseen budget thresholds.
> - **Do NOT hardcode answers or static lookups.** Your agent must compute discounts dynamically via `tools`.

---

## 7. Execution Modes

### Mode A: Practice Mode (Local Iteration)
Ideal for developing, debugging, and benchmarking locally:
```bash
# Process a single task and exit with diff analysis:
python main.py --mode practice --once

# Process first N tasks:
python main.py --mode practice --max-tasks 5

# Process all 30 development tasks:
python main.py --mode practice
```
Check `http://127.0.0.1:8001/dashboard` for real-time visual score tracking and failure diffs.

### Mode B: Submission Mode (Live Arena Platform)
When you are ready to compete on the official Arena platform:
1. In `.env`, set:
   ```env
   MODE=submission
   SUBMISSION_ARENA_URL=https://excretory-clustered-anointer.ngrok-free.dev
   SUBMISSION_BEARER_TOKEN=your-bearer-token-assigned-at-registration
   ```
2. Run official submission:
   ```bash
   python main.py --mode submission
   ```
In submission mode:
- All 30 competition tasks are fetched upfront in randomized order.
- Your agent executes tasks sequentially in memory (rate-limit safe).
- Solutions are submitted in a single atomic batch (`POST /submission/{id}/submit_batch`).
- Your verified multi-dimensional score card is rendered upon completion.

---

## 8. Macro Scoring Dimensions

Submissions are evaluated across 7 orthogonal dimensions (0% – 100%):

1. **Task Success (35% weight)**: Correctness of `opportunity_detected`, `notify_user`, and opportunity classification.
2. **Policy Adherence (15% weight)**: Strict adherence to user constraints (never notify if effective price exceeds budget or minimum savings).
3. **Evidence Grounding (15% weight)**: Precision and recall of cited evidence IDs (`USER-*`, `PROD-*`, `PRICE-HISTORY-*`, `OFFER-*`).
4. **Calibration (10% weight)**: Statistical alignment between predicted `confidence` and actual outcome correctness.
5. **Efficiency (10% weight)**: Achieving optimal accuracy well within the 100 tool-call budget.
6. **Communication (10% weight)**: Clarity, actionability, and accuracy of the generated notification message.
7. **Robustness (5% weight)**: Handling missing data, invalid coupons, and contradictory triggers gracefully.

---

## 9. Pro-Tips for Winning

- **Validate Stacking Rules**: Multiple offers may exist, but not all offers stack! Use `check_offer_eligibility` to verify validity.
- **Compute Real Historical Low**: Compare current price against `PRICE-HISTORY-*` entries to determine if this is a genuine 30-day low.
- **Automatic Key Rotation**: Configure `GOOGLE_API_KEY_1` through `GOOGLE_API_KEY_5` in `.env`. `main.py` rotates keys sequentially across tasks.
- **Cite Only Observed Evidence**: Only include IDs in `evidence: [...]` that actually returned from tool calls.

