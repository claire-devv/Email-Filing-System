from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8088"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1 local test helper. Safe checks run by default; paid processing requires --run-paid."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--smoke", action="store_true", help="Run safe API smoke checks.")
    parser.add_argument("--regression", action="store_true", help="Run local no-network regression scripts.")
    parser.add_argument("--process-unread", action="store_true", help="Call /emails/process-unread. Requires --run-paid.")
    parser.add_argument("--message-id", help="Call /emails/{message_id}/process. Requires --run-paid.")
    parser.add_argument("--run-paid", action="store_true", help="Allow Gmail/Claude processing calls.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--newer-than-minutes", type=int, default=1440)
    parser.add_argument("--activity-limit", type=int, default=10)
    parser.add_argument("--review-id", type=int, help="Fetch files for the latest activity is separate; this is only printed for convenience.")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON responses.")
    args = parser.parse_args()

    if args.regression:
        run_regression_tests()

    should_run_smoke = args.smoke or not any([args.regression, args.process_unread, args.message_id, args.review_id])
    if should_run_smoke:
        run_smoke_checks(args.base_url, args.activity_limit, args.raw)

    if args.process_unread:
        require_paid_flag(args.run_paid, "--process-unread")
        result = request_json(
            args.base_url,
            "POST",
            "/emails/process-unread",
            {"limit": args.limit, "newer_than_minutes": args.newer_than_minutes},
        )
        print_section("process-unread result")
        print_process_result(result, args.raw)

    if args.message_id:
        require_paid_flag(args.run_paid, "--message-id")
        result = request_json(args.base_url, "POST", f"/emails/{args.message_id}/process", None)
        print_section("process one message result")
        print_json(result)

    if args.review_id:
        print_section("review action examples")
        print(f"Approve: POST {args.base_url}/review/items/{args.review_id}/approve")
        print(f"Correct: POST {args.base_url}/review/items/{args.review_id}/correct")
        print(f"Reject:  POST {args.base_url}/review/items/{args.review_id}/reject")


def run_regression_tests() -> None:
    print_section("local regression tests")
    commands = [
        [sys.executable, "-m", "tests.test_email_artifacts"],
        [sys.executable, "-m", "tests.test_decision_validator"],
        [sys.executable, "-m", "tests.test_api_limit_handling"],
        [sys.executable, "-m", "compileall", "app", "-q"],
    ]
    for command in commands:
        label = " ".join(command[2:]) if len(command) > 2 else " ".join(command)
        print(f"Running {label} ...")
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        if completed.stdout.strip():
            print(completed.stdout.strip())
        if completed.stderr.strip():
            print(completed.stderr.strip())
        if completed.returncode != 0:
            raise SystemExit(f"Regression command failed: {' '.join(command)}")
    print("Regression checks passed.")


def run_smoke_checks(base_url: str, activity_limit: int, raw: bool) -> None:
    print_section("api smoke checks")
    health = request_json(base_url, "GET", "/health", None)
    counts = request_json(base_url, "GET", "/notifications/counts", None)
    usage = request_json(base_url, "GET", "/admin/api-usage", None)
    entities = request_json(base_url, "GET", "/entities", None)
    review_items = request_json(base_url, "GET", "/review/items", None)
    activity = request_json(base_url, "GET", f"/activity?limit={activity_limit}", None)

    if raw:
        print_json(
            {
                "health": health,
                "notifications": counts,
                "api_usage": usage,
                "entities": entities,
                "review_items": review_items,
                "activity": activity,
            }
        )
        return

    print(f"Health: {health}")
    print(
        "Notifications: "
        f"pending={counts.get('pending_review_count')} urgent={counts.get('urgent_review_count')}"
    )
    print_api_usage(usage)
    print(f"Entities: {len(entities)} active")
    if entities:
        for entity in entities[:5]:
            print(f"  - {entity.get('entity_name')}")
    print_review_items(review_items)
    print_activity(activity)


def print_api_usage(payload: dict[str, Any]) -> None:
    usage = payload.get("usage") or []
    if not usage:
        print("API usage: none recorded")
        return
    print("API usage:")
    for row in usage[:10]:
        print(f"  - {row.get('provider')} {row.get('date')}: {row.get('call_count')}")


def print_review_items(items: list[dict[str, Any]]) -> None:
    print(f"Review queue: {len(items)} pending")
    for item in items[:10]:
        proposed = item.get("proposed") or {}
        print(
            "  - "
            f"id={item.get('id')} urgent={item.get('urgent')} "
            f"entity={proposed.get('entity')} level2={proposed.get('level2')} "
            f"confidence={proposed.get('confidence')}"
        )


def print_activity(items: list[dict[str, Any]]) -> None:
    print(f"Latest activity: {len(items)} items")
    for item in items:
        artifacts = item.get("artifacts") or []
        metadata = item.get("processing_metadata") or {}
        filed_artifacts = [artifact for artifact in artifacts if artifact.get("status") == "filed"]
        internal_artifacts = [artifact for artifact in artifacts if artifact.get("status") == "internal"]
        print(
            "  - "
            f"id={item.get('id')} status={item.get('status')} "
            f"confidence={item.get('confidence')} entity={item.get('entity')} "
            f"subject={shorten(item.get('subject'))}"
        )
        print(
            "    "
            f"files={len(artifacts)} filed={len(filed_artifacts)} internal={len(internal_artifacts)} "
            f"real_attachments={metadata.get('real_attachment_count')} "
            f"inline_assets={metadata.get('inline_asset_count')}"
        )
        message = item.get("message")
        if message:
            print(f"    message={shorten(message, 120)}")


def print_process_result(payload: dict[str, Any], raw: bool) -> None:
    if raw:
        print_json(payload)
        return
    print(
        f"processed={payload.get('processed_count')} "
        f"skipped={payload.get('skipped_count')} "
        f"waiting={payload.get('waiting_count')}"
    )
    for item in payload.get("results") or []:
        print(
            "  - "
            f"{item.get('gmail_message_id')} status={item.get('status')} "
            f"email_id={item.get('email_id')} review_id={item.get('review_id')} "
            f"message={shorten(item.get('message'), 140)}"
        )


def request_json(base_url: str, method: str, path: str, payload: dict[str, Any] | None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {path} failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Cannot reach {base_url}. Start the API first with: "
            ".\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8088"
        ) from exc


def require_paid_flag(enabled: bool, action: str) -> None:
    if not enabled:
        raise SystemExit(
            f"{action} can call Gmail and Claude. Re-run with --run-paid when you intentionally want that."
        )


def print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def shorten(value: Any, limit: int = 90) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


if __name__ == "__main__":
    main()
