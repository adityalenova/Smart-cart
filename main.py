"""Agent Arena SmartCart — Main Participant Runtime.

Orchestrates the lifecycle around the participant's agent:
1. Loads .env configuration (BASE_URL, BEARER_TOKEN, MODE).
2. Connects to the Arena API (Mock Simulator or Live Platform).
3. Executes in either:
   - Practice Mode: ad-hoc testing with --once or --max-tasks, detailed practice feedback, and poll intervals.
   - Submission Mode: full competition epoch running all assigned tasks sequentially without artificial gaps, collecting answers in memory, and submitting all solutions in a single batch. Truncation flags (--max-tasks, --once) are strictly forbidden in submission mode.
4. Invokes agent.solve(task, tools) strictly sequentially to prevent rate limit bottlenecks.
5. In Mock Simulator, tracks and displays accuracy (e.g. 25/30 tasks correct) and links to the visual debugger at /dashboard.
"""

import argparse
import inspect
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import agent
from sdk.tools_client import ApiError, ArenaClient, TransportError


def get_google_api_keys() -> list[str]:
    """Collects configured Google API keys from .env for round-robin rotation across tasks.

    Checks GOOGLE_API_KEY_1 through GOOGLE_API_KEY_5 (supports up to 10 keys).
    If no numbered keys are found, falls back to GOOGLE_API_KEY or GEMINI_API_KEY.
    """
    keys: list[str] = []
    for i in range(1, 11):
        val = os.getenv(f"GOOGLE_API_KEY_{i}", "").strip()
        if val and not val.lower().startswith("your_"):
            keys.append(val)
    if not keys:
        single = os.getenv("GOOGLE_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
        if single and not single.lower().startswith("your_"):
            keys.append(single)
    return keys


def run_agent_solve(
    task: dict[str, Any],
    tools: Any,
    api_key: str | None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Invokes agent.solve(task, tools, ...) with backwards-compatibility support."""
    sig_params = inspect.signature(agent.solve).parameters
    kwargs: dict[str, Any] = {}
    if "api_key" in sig_params:
        kwargs["api_key"] = api_key
    if "model" in sig_params and model:
        kwargs["model"] = model
    if "base_url" in sig_params and base_url:
        kwargs["base_url"] = base_url
    return agent.solve(task, tools, **kwargs)


def validate_output_contract(output: Any) -> list[str]:
    """Validates returned agent answer dictionary against the SmartCart output contract."""
    errors = []
    if not isinstance(output, dict):
        return ["Output must be a dictionary."]

    # decision
    dec = output.get("decision")
    if not isinstance(dec, dict):
        errors.append("Missing or invalid 'decision' (must be dict).")
    else:
        if not isinstance(dec.get("opportunity_detected"), bool):
            errors.append("decision.opportunity_detected must be a boolean.")
        if not isinstance(dec.get("notify_user"), bool):
            errors.append("decision.notify_user must be a boolean.")

    # price_analysis (optional but must be dict if present)
    pa = output.get("price_analysis")
    if pa is not None and not isinstance(pa, dict):
        errors.append("price_analysis must be a dict when provided.")

    # offer_analysis (optional but must be dict if present)
    oa = output.get("offer_analysis")
    if oa is not None and not isinstance(oa, dict):
        errors.append("offer_analysis must be a dict when provided.")

    # evidence
    ev = output.get("evidence")
    if not isinstance(ev, list):
        errors.append("Missing or invalid 'evidence' (must be list of string IDs).")

    # customer_notification required when notify_user is True
    note = output.get("customer_notification")
    if dec and dec.get("notify_user") is True:
        if not isinstance(note, dict) or not note.get("title") or not note.get("message"):
            errors.append("customer_notification.title and message are required when notify_user is true.")

    # reason (optional string)
    reason = output.get("reason")
    if reason is not None and not isinstance(reason, str):
        errors.append("reason must be a string.")

    # confidence
    conf = output.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        errors.append("confidence must be a float between 0.0 and 1.0.")

    return errors


def main(
    mode: str = "practice",
    once: bool = False,
    max_tasks: int | None = None,
    poll_interval: float | None = None,
    model: str | None = None,
    gemini_base_url: str | None = None,
) -> None:
    if model:
        os.environ["GEMINI_MODEL"] = model
    if gemini_base_url:
        os.environ["GEMINI_BASE_URL"] = gemini_base_url

    if mode == "submission":
        if max_tasks is not None or once:
            print("[-] Error: '--max-tasks' and '--once' are strictly forbidden in submission mode.")
            print("    Official submission mode requires evaluating all assigned tasks without truncation.")
            print("    For quick iteration or testing with limited tasks, run against Mock Simulator in practice mode:")
            print("        python main.py --mode practice --max-tasks 3")
            sys.exit(1)

    if mode == "practice":
        base_url = os.getenv("PRACTICE_ARENA_URL", os.getenv("BASE_URL", "http://127.0.0.1:8001"))
        token = os.getenv("PRACTICE_BEARER_TOKEN", os.getenv("BEARER_TOKEN", "dev-practice-token"))
    else:
        base_url = os.getenv("SUBMISSION_ARENA_URL", os.getenv("BASE_URL", "http://localhost:8000"))
        token = os.getenv("SUBMISSION_BEARER_TOKEN", os.getenv("BEARER_TOKEN", ""))
    is_mock = ":8001" in base_url or "localhost:8001" in base_url or "127.0.0.1:8001" in base_url

    # Default poll intervals: 0.0 for submission (sequential throughput), 1.0 for practice
    if poll_interval is None:
        poll_interval = 0.0 if mode == "submission" else 1.0

    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    active_gemini_base_url = os.getenv("GEMINI_BASE_URL", "").strip() or os.getenv("GOOGLE_BASE_URL", "").strip()

    print("=" * 65)
    print("  Agent Arena SmartCart — Participant Runtime (main.py)")
    print("=" * 65)
    print(f"Target Arena : {base_url}")
    print(f"Auth Token   : {token[:6]}***")
    print(f"Mode         : {mode.upper()}{' (Single Task)' if once and mode == 'practice' else ''}")
    print("Sequential   : Yes (Protected against concurrency rate limits)")
    if is_mock:
        print(f"Debug UI     : {base_url.rstrip('/')}/dashboard")
    google_keys = get_google_api_keys()
    if google_keys:
        print(f"LLM API Keys : {len(google_keys)} key(s) loaded for round-robin rotation")
        for k_idx, k_val in enumerate(google_keys, 1):
            masked = k_val[:6] + "..." + k_val[-4:] if len(k_val) > 12 else k_val[:4] + "..."
            print(f"               Key #{k_idx}: {masked}")
    else:
        print("LLM API Keys : None configured (set GOOGLE_API_KEY_1..5 in .env)")
    print(f"LLM Model    : {model_name}")
    if active_gemini_base_url:
        print(f"LLM Base URL : {active_gemini_base_url}")
    print("=" * 65)

    try:
        with ArenaClient(base_url=base_url, token=token) as client:
            sub_id = None
            tasks_list: list[dict[str, Any]] = []
            # 1. Initialize or connect to active submission
            try:
                sub = client.start_submission()
                sub_id = sub.get("submission_id")
                tasks_list = sub.get("tasks") or []
                total_expected = sub.get("tasks_total") or len(tasks_list)
                print(f"[+] Started new submission: {sub_id} (Expected tasks: {total_expected or 'N/A'})")
            except ApiError as e:
                if "ACTIVE_SUBMISSION_EXISTS" in str(e):
                    old_sub_id = None
                    if isinstance(e.detail, dict):
                        old_sub_id = e.detail.get("submission_id")
                        if not old_sub_id and isinstance(e.detail.get("detail"), dict):
                            old_sub_id = e.detail["detail"].get("submission_id")
                    print(
                        f"[*] Found previous interrupted submission ({old_sub_id}). Aborting and starting fresh run..."
                    )
                    if old_sub_id:
                        try:
                            client.abort_submission(old_sub_id)
                        except Exception:
                            pass
                    # Retry fresh start
                    sub = client.start_submission()
                    sub_id = sub.get("submission_id")
                    tasks_list = sub.get("tasks") or []
                    total_expected = sub.get("tasks_total") or len(tasks_list)
                    print(f"[+] Started new submission: {sub_id} (Expected tasks: {total_expected or 'N/A'})")
                else:
                    print(f"[*] Submission notice: {e.detail}")

            tasks_completed = 0
            passed_count = 0
            failed_count = 0
            completed_in_memory: list[dict[str, Any]] = []
            total_start_time = time.time()

            if mode == "submission" and tasks_list:
                # -----------------------------------------------------------------
                # BATCH SUBMISSION MODE: All tasks delivered upfront, solved
                # sequentially locally, and submitted in a single atomic batch.
                # -----------------------------------------------------------------
                batch_answers: list[dict[str, Any]] = []
                print(f"[+] Received all {len(tasks_list)} tasks upfront for this submission.")
                print("[+] Executing tasks sequentially and collecting answers in memory...")

                try:
                    for idx, task in enumerate(tasks_list, 1):
                        task_id = task.get("task_id", "UNKNOWN")
                        client.set_active_task(task_id)

                        print(f"\n[+] Processing Task #{idx}/{len(tasks_list)}: {task_id}...")

                        current_api_key = google_keys[(idx - 1) % len(google_keys)] if google_keys else None
                        key_num = ((idx - 1) % len(google_keys)) + 1 if google_keys else None
                        if current_api_key and key_num:
                            masked_key = (
                                current_api_key[:6] + "..." + current_api_key[-4:]
                                if len(current_api_key) > 12
                                else current_api_key[:4] + "..."
                            )
                            print(f"Active LLM Key: Key #{key_num} of {len(google_keys)} ({masked_key})")
                        t_start_iso = datetime.now(timezone.utc).isoformat()
                        t0 = time.time()
                        try:
                            answer = run_agent_solve(
                                task,
                                client.tools,
                                current_api_key,
                                model=model_name,
                                base_url=active_gemini_base_url or None,
                            )
                        except NotImplementedError:
                            print("\n" + "!" * 65)
                            print("  [!] agent.solve() raised NotImplementedError.")
                            print("  To solve tasks, implement your agent logic inside:")
                            print("      agent.py -> def solve(task, tools, api_key=None, model=None, base_url=None)")
                            print("!" * 65)
                            print("=" * 65)
                            print("Run completed successfully.")
                            if sub_id:
                                try:
                                    client.abort_submission(sub_id)
                                except (ApiError, TransportError):
                                    pass
                            return
                        except Exception as e:
                            print(f"\n[-] Unhandled exception in agent.solve() on {task_id}: {e}")
                            raise

                        t_end_iso = datetime.now(timezone.utc).isoformat()
                        duration = time.time() - t0

                        # Validate Section 7 contract compliance
                        validation_errors = validate_output_contract(answer)
                        if validation_errors:
                            print(f"[!] Output validation warnings for {task_id}:")
                            for err in validation_errors:
                                print(f"    - {err}")

                        opp_choice = answer.get("decision", {}).get("opportunity_detected")
                        notif_choice = answer.get("decision", {}).get("notify_user")
                        print(f"Opportunity   : {opp_choice} (Notify: {notif_choice})")
                        print(f"Evidence      : {answer.get('evidence')}")
                        print(f"Solve Time    : {duration:.2f}s")

                        tasks_completed += 1
                        ans_dict = dict(answer)
                        ans_dict.setdefault("task_id", task_id)
                        ans_dict["started_at"] = t_start_iso
                        ans_dict["completed_at"] = t_end_iso
                        ans_dict["task_started_at"] = t_start_iso
                        ans_dict["task_completed_at"] = t_end_iso
                        batch_answers.append(ans_dict)

                        completed_in_memory.append(
                            {
                                "task_id": task_id,
                                "opportunity_detected": opp_choice,
                                "notify_user": notif_choice,
                                "duration": duration,
                                "correct": None,
                            }
                        )

                        if poll_interval > 0 and idx < len(tasks_list):
                            time.sleep(poll_interval)

                    # All tasks solved! Submit in a single atomic batch
                    print("\n" + "=" * 65)
                    print(f"Submitting all {len(batch_answers)} task solutions in a single batch to Arena API...")
                    if not sub_id:
                        raise RuntimeError("Cannot submit batch: missing active submission_id")
                    batch_res = client.submit_batch(sub_id, batch_answers)
                    print(
                        f"[+] Batch submission completed successfully! (Status: {batch_res.get('status', 'completed')})"
                    )

                except Exception as e:
                    print(f"\n[-] Critical error encountered during submission run: {e}")
                    if sub_id:
                        print(
                            f"[*] Aborting submission '{sub_id}' so it is not scored or counted against attempt limit..."
                        )
                        try:
                            client.abort_submission(sub_id)
                            print("[+] Submission successfully aborted as interrupted.")
                        except (ApiError, TransportError) as abort_err:
                            print(f"[-] Could not abort cleanly: {abort_err}")
                    raise

            else:
                # -----------------------------------------------------------------
                # PRACTICE MODE / FALLBACK TASK-BY-TASK LOOP
                # -----------------------------------------------------------------
                task_iter_list = list(tasks_list) if tasks_list else None

                while True:
                    if max_tasks and tasks_completed >= max_tasks:
                        print(f"\n[*] Reached maximum tasks limit ({max_tasks}). Exiting.")
                        break

                    if task_iter_list is not None:
                        if not task_iter_list:
                            print("[+] All available tasks completed for this epoch!")
                            break
                        task = task_iter_list.pop(0)
                    else:
                        print(f"\n--- Requesting Task #{tasks_completed + 1} ---")
                        try:
                            task = client.get_task()
                        except ApiError as e:
                            err_str = str(e)
                            if "NO_MORE_TASKS" in err_str or "SUBMISSION_COMPLETED" in err_str or "NO_TASKS" in err_str:
                                print("[+] All available tasks completed for this epoch!")
                                break
                            print(f"[-] Could not acquire task: {e.detail}")
                            break

                    task_id = task.get("task_id", "UNKNOWN")
                    client.set_active_task(task_id)

                    print(f"\n--- Processing Task #{tasks_completed + 1} ---")
                    print(f"Assigned Task : {task_id}")
                    print(f"User ID       : {task.get('user_id')}")
                    print(f"Product ID    : {task.get('product_id')}")
                    print(f"Objective     : {task.get('objective')}")
                    print("-" * 65)

                    # Invoke participant agent solve(task, tools, api_key)
                    current_api_key = google_keys[tasks_completed % len(google_keys)] if google_keys else None
                    key_num = (tasks_completed % len(google_keys)) + 1 if google_keys else None
                    if current_api_key and key_num:
                        masked_key = (
                            current_api_key[:6] + "..." + current_api_key[-4:]
                            if len(current_api_key) > 12
                            else current_api_key[:4] + "..."
                        )
                        print(f"Active LLM Key: Key #{key_num} of {len(google_keys)} ({masked_key})")
                    t0 = time.time()
                    try:
                        answer = run_agent_solve(
                            task,
                            client.tools,
                            current_api_key,
                            model=model_name,
                            base_url=active_gemini_base_url or None,
                        )
                    except NotImplementedError:
                        print("\n" + "!" * 65)
                        print("  [!] agent.solve() raised NotImplementedError.")
                        print("  To solve tasks, implement your agent logic inside:")
                        print("      agent.py -> def solve(task, tools, api_key=None, model=None, base_url=None)")
                        print("!" * 65)
                        print("=" * 65)
                        print("Run completed successfully.")
                        return
                    except Exception as e:
                        print(f"\n[-] Unhandled exception in agent.solve() on {task_id}: {e}")
                        raise

                    duration = time.time() - t0

                    # Validate Section 7 contract compliance
                    validation_errors = validate_output_contract(answer)
                    if validation_errors:
                        print(f"[!] Output validation warnings for {task_id}:")
                        for err in validation_errors:
                            print(f"    - {err}")

                    # Submit answer to API
                    opp_choice = answer.get("decision", {}).get("opportunity_detected")
                    notif_choice = answer.get("decision", {}).get("notify_user")
                    print(f"Opportunity   : {opp_choice} (Notify: {notif_choice})")
                    print(f"Evidence      : {answer.get('evidence')}")
                    print(f"Solve Time    : {duration:.2f}s")
                    print("Submitting decision to API...")

                    result = client.submit_task(task_id=task_id, payload=answer)
                    tasks_completed += 1

                    # Evaluation feedback
                    is_correct = None
                    if "correct" in result:
                        is_correct = bool(result.get("correct", False))
                        if is_correct:
                            passed_count += 1
                            print("Mock Evaluation : [PASS] Ground truth matched perfectly!")
                        else:
                            failed_count += 1
                            print("Mock Evaluation : [FAIL]")
                            print(f"   Expected Res: {result.get('expected_resolution')}")
                            print(f"   Expected Ev:  {result.get('expected_evidence')}")
                            print(f"   Diff:         {result.get('diff_explanation')}")
                            if is_mock:
                                print(f"   Debug URL:    {base_url.rstrip('/')}/dashboard")
                    else:
                        print("[+] Submission received and recorded by Arena platform.")

                    completed_in_memory.append(
                        {
                            "task_id": task_id,
                            "opportunity_detected": opp_choice,
                            "notify_user": notif_choice,
                            "duration": duration,
                            "correct": is_correct,
                        }
                    )

                    if mode == "practice" and once:
                        print("\n[+] Single task completed (--once flag). Exiting.")
                        break

                    if poll_interval > 0:
                        time.sleep(poll_interval)

                # Finalize submission if in submission mode (for step-by-step fallback)
                if sub_id and mode == "submission":
                    try:
                        client.finalize_submission(sub_id)
                        print(f"\n[+] Finalized submission '{sub_id}'.")
                    except ApiError as e:
                        print(f"\n[*] Finalize notice: {e.detail}")

            total_elapsed = time.time() - total_start_time
            avg_time = (total_elapsed / tasks_completed) if tasks_completed > 0 else 0.0

            # 8. Execution Summary Report
            print("\n" + "=" * 65)
            print("  EPOCH EXECUTION SUMMARY")
            print("=" * 65)
            print(f"Mode               : {mode.upper()}")
            print(f"Tasks Processed    : {tasks_completed}")
            print(f"Total Epoch Time   : {total_elapsed:.2f}s (avg {avg_time:.2f}s/task)")

            if is_mock or (passed_count + failed_count > 0):
                total_eval = passed_count + failed_count
                acc = (passed_count / total_eval * 100.0) if total_eval > 0 else 0.0
                print(f"Mock Score         : {passed_count}/{total_eval} tasks correct ({acc:.1f}%)")
                if is_mock:
                    print(f"Debug Dashboard    : {base_url.rstrip('/')}/dashboard")
            elif mode == "submission" and "batch_res" in locals():
                agg = batch_res.get("aggregate_score")
                if agg is not None:
                    print(f"Aggregate Score    : {agg * 100:.2f}% (Hidden Benchmark Evaluation)")
                dims = batch_res.get("breakdown", {}).get("dimensions", {})
                if dims:
                    print("\n--- Macro Scoring Dimensions ---")
                    for d_name, d_val in dims.items():
                        print(f"  - {d_name.replace('_', ' ').title():<20}: {float(d_val) * 100:.1f}%")
            else:
                print("Arena Evaluation   : Completed and scored on hidden competition dataset.")

            print("=" * 65)

    except TransportError as e:
        print(f"\n[-] Network / Transport Error: {e}")
        print("Tip: Make sure the target Arena or Mock Simulator is running:")
        print(f"     Target URL: {base_url}")
        print("     To start mock simulator: python mock_simulator/server.py")
        sys.exit(1)
    except ApiError as e:
        print(f"\n[-] Arena API Error ({e.status_code}): {e.detail}")
        sys.exit(1)

    print("Run completed successfully.")


if __name__ == "__main__":
    default_mode = os.getenv("MODE", "practice").lower()
    if default_mode not in ("practice", "submission"):
        default_mode = "practice"

    parser = argparse.ArgumentParser(description="Agent Arena SmartCart Participant Runtime")
    parser.add_argument(
        "--mode",
        choices=["practice", "submission"],
        default=default_mode,
        help="Runtime execution mode: 'practice' (interactive/debug) or 'submission' (competition epoch)",
    )
    parser.add_argument("--once", action="store_true", help="Process only one task and exit (practice mode only)")
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum number of tasks to process (practice mode only)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Seconds to wait between task queries (default: 1.0 for practice, 0.0 for submission)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Google Gemini model name (overrides GEMINI_MODEL in .env, default: gemini-3.5-flash-lite)",
    )
    parser.add_argument(
        "--gemini-base-url",
        type=str,
        default=None,
        help="Custom base URL for Gemini / Google API (overrides GEMINI_BASE_URL in .env)",
    )
    args = parser.parse_args()

    main(
        mode=args.mode,
        once=args.once,
        max_tasks=args.max_tasks,
        poll_interval=args.poll_interval,
        model=args.model,
        gemini_base_url=args.gemini_base_url,
    )
