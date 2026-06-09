from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

from draft_review_helper import prepare_draft_review, resolve_path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_SERVICE_ACCOUNT_PATH = PROJECT_ROOT / "google-service-account.json"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def service_account_path() -> Path:
    configured = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    )
    return resolve_path(configured) if configured else DEFAULT_SERVICE_ACCOUNT_PATH


def sheet_id() -> str:
    value = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not value:
        raise RuntimeError("GOOGLE_SHEET_ID is required for Phase 4 sheet updates.")
    return value


def sheet_tab() -> str:
    return os.getenv("GOOGLE_SHEET_TAB", "").strip() or "Threads"


def sheets_service():
    key_path = service_account_path()
    if not key_path.exists():
        raise RuntimeError(
            f"Google service account file not found: {key_path}. "
            "Set GOOGLE_SERVICE_ACCOUNT_FILE or mount google-service-account.json into /workspace."
        )
    credentials = service_account.Credentials.from_service_account_file(str(key_path), scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_rows() -> tuple[list[str], list[dict[str, Any]]]:
    response = (
        sheets_service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id(), range=f"{sheet_tab()}!A:ZZ")
        .execute()
    )
    values = response.get("values") or []
    if not values:
        raise RuntimeError("Google Sheet has no header row")

    headers = [str(value).strip() for value in values[0]]
    rows: list[dict[str, Any]] = []
    for index, values_row in enumerate(values[1:], start=2):
        row = {header: values_row[column] if column < len(values_row) else "" for column, header in enumerate(headers)}
        row["_row_number"] = index
        rows.append(row)
    return headers, rows


def update_row(headers: list[str], row_number: int, updates: dict[str, Any]) -> dict[str, Any]:
    _, rows = read_rows()
    current = next((row for row in rows if row.get("_row_number") == row_number), None)
    if not current:
        raise RuntimeError(f"Could not find row number {row_number} for update")

    values = []
    for header in headers:
        value = updates.get(header, current.get(header, ""))
        values.append("" if value is None else str(value))

    end_column = column_letter(len(headers))
    sheets_service().spreadsheets().values().update(
        spreadsheetId=sheet_id(),
        range=f"{sheet_tab()}!A{row_number}:{end_column}{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()
    return {key: value for key, value in updates.items() if key in headers}


def update_row_by_id(headers: list[str], post_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    _, rows = read_rows()
    row = next((row for row in rows if str(row.get("ID", "")).strip() == post_id), None)
    if not row:
        raise RuntimeError(f"Could not find sheet row with ID={post_id}")
    return update_row(headers, int(row["_row_number"]), updates)


def column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def clean_caption(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def safe_error_message(exc: Exception) -> str:
    message = str(exc)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token:
        message = message.replace(bot_token, "<telegram-bot-token>")
    return message[:320]


def find_draft_review_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        status = str(row.get("Status", "")).strip().lower()
        video_path = str(row.get("Video_Path", "")).strip()
        caption = clean_caption(row.get("Caption", ""))
        draft_url = str(row.get("Draft_Video_URL", "")).strip()
        if status == "draft" and video_path and caption and not draft_url:
            return row
    return None


def find_publish_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        status = str(row.get("Status", "")).strip().lower()
        video_path = str(row.get("Video_Path", "")).strip()
        caption = clean_caption(row.get("Caption", ""))
        publish_id = str(row.get("TikTok_Publish_ID", "")).strip()
        if status == "approved" and video_path and caption and not publish_id:
            return row
    return None


def send_telegram_review(row: dict[str, Any]) -> dict[str, Any]:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing"}

    draft_url = str(row.get("Draft_Video_URL", "")).strip()
    caption = clean_caption(row.get("Caption", ""))
    preview = caption[:900] + "..." if len(caption) > 900 else caption
    text = "\n".join(
        [
            "Phase 4 draft ready for review",
            "",
            f"ID: {row.get('ID', '')}",
            f"Review video: {draft_url}",
            "",
            "Caption:",
            preview,
            "",
            "Choose an admin decision below.",
        ]
    )
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"phase4|approve|{row.get('ID', '')}"},
                        {"text": "Reject", "callback_data": f"phase4|reject|{row.get('ID', '')}"},
                    ],
                    [{"text": "Open Review", "url": draft_url}],
                ]
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    return {"sent": True, "telegram_result": response.json().get("result") or {}}


def answer_telegram_callback(callback: dict[str, Any], label: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        return

    callback_id = callback.get("id")
    if callback_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": f"Decision saved: {label}", "show_alert": False},
                timeout=30,
            ).raise_for_status()
        except Exception as exc:
            log(f"Could not answer Telegram callback query: {exc}")

    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    original_text = str(message.get("text") or "").strip()
    if chat_id and message_id and original_text:
        updated_text = original_text if "Admin decision:" in original_text else f"{original_text}\n\nAdmin decision: {label}"
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": updated_text,
                    "disable_web_page_preview": False,
                },
                timeout=30,
            ).raise_for_status()
        except Exception as exc:
            log(f"Could not edit Telegram review message: {exc}")


def publish_row(row: dict[str, Any]) -> dict[str, Any]:
    mode = os.getenv("TIKTOK_PUBLISHER_MODE", "api").strip().lower()
    if mode == "playwright":
        from tiktok_playwright_publisher import publish_video

        return publish_video(
            str(row.get("ID", "")).strip(),
            str(resolve_path(str(row.get("Video_Path", "")).strip())),
            clean_caption(row.get("Caption", "")),
        )

    from tiktok_publisher import publish

    return publish(
        str(row.get("ID", "")).strip(),
        resolve_path(str(row.get("Video_Path", "")).strip()),
        clean_caption(row.get("Caption", "")),
    )


def run_tick() -> dict[str, Any]:
    headers, rows = read_rows()

    draft = find_draft_review_row(rows)
    if draft:
        post_id = str(draft.get("ID", "")).strip()
        video_path = resolve_path(str(draft.get("Video_Path", "")).strip())
        caption = clean_caption(draft.get("Caption", ""))
        result = prepare_draft_review(post_id, video_path, caption)
        update_row(headers, int(draft["_row_number"]), result)
        telegram_result = send_telegram_review(result)
        return {
            "action": "draft_review_created",
            "id": post_id,
            "review_transport": "local",
            "sheet_update": result,
            "telegram": telegram_result,
        }

    approved = find_publish_row(rows)
    if approved:
        post_id = str(approved.get("ID", "")).strip()
        update_row(headers, int(approved["_row_number"]), {"ID": post_id, "Status": "Approved", "Note": "Phase 4: publish started"})
        result = publish_row(approved)
        update_row(headers, int(approved["_row_number"]), result)
        return {"action": "published", "id": post_id, "sheet_update": result}

    return {"action": "idle", "message": "No draft-review or approved publish row found"}


def run_review(update: dict[str, Any]) -> dict[str, Any]:
    post_id = str(update.get("id") or update.get("ID") or "").strip()
    video_path = resolve_path(str(update.get("video_path") or update.get("Video_Path") or "").strip())
    caption = clean_caption(update.get("caption") or update.get("Caption") or "")
    if not post_id:
        raise RuntimeError("Missing id for Phase 4 review")
    if not caption:
        raise RuntimeError("Missing caption for Phase 4 review")

    result = prepare_draft_review(post_id, video_path, caption)
    telegram_result = send_telegram_review(result)

    return {
        "action": "draft_review_created",
        "id": post_id,
        "review_transport": "local",
        "sheet_update": result,
        "telegram": telegram_result,
        **result,
    }


def run_telegram_callback(update: dict[str, Any]) -> dict[str, Any]:
    headers, _ = read_rows()
    callback = update.get("callback_query") or (update.get("body") or {}).get("callback_query")
    if not callback:
        raise RuntimeError("Telegram update does not contain callback_query")

    allowed_user_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    from_id = str((callback.get("from") or {}).get("id") or "").strip()
    if allowed_user_id and from_id != allowed_user_id:
        raise RuntimeError(f"Telegram callback sender is not allowed: {from_id}")

    data = str(callback.get("data") or "").strip()
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "phase4":
        raise RuntimeError(f"Unexpected Phase 4 callback_data: {data}")

    decision = parts[1].strip().lower()
    post_id = parts[2].strip()
    if decision not in {"approve", "reject"}:
        raise RuntimeError(f"Unsupported admin decision: {decision}")
    if not post_id:
        raise RuntimeError("Missing row ID in Telegram callback_data")

    approved = decision == "approve"
    label = "APPROVED" if approved else "REJECTED"
    update = {
        "ID": post_id,
        "Status": "Approved" if approved else "Rejected",
        "Admin_Decision": decision,
        "Note": (
            "Phase 4: approved from Telegram callback; compact workflow can publish it"
            if approved
            else "Phase 4: rejected from Telegram callback"
        ),
    }
    sheet_update = update_row_by_id(headers, post_id, update)
    answer_telegram_callback(callback, label)
    return {"action": "admin_decision_saved", "id": post_id, "decision": decision, "sheet_update": sheet_update}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact Phase 4 workflow helper.")
    parser.add_argument("--mode", choices=["tick", "review", "telegram_callback"], required=True)
    parser.add_argument("--update-json", default="{}")
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH, override=True)
    args = parse_args()
    try:
        if args.mode == "tick":
            result = run_tick()
        elif args.mode == "review":
            result = run_review(json.loads(args.update_json or "{}"))
        else:
            result = run_telegram_callback(json.loads(args.update_json or "{}"))
    except Exception as exc:
        log(f"Phase 4 compact helper failed: {exc}")
        payload: dict[str, Any] = {}
        if args.mode != "tick":
            try:
                payload = json.loads(args.update_json or "{}")
            except json.JSONDecodeError:
                payload = {}
        result = {
            "action": "failed",
            "ID": payload.get("id") or payload.get("ID") or "",
            "Video_Path": payload.get("video_path") or payload.get("Video_Path") or "",
            "Caption": payload.get("caption") or payload.get("Caption") or "",
            "Draft_Video_URL": payload.get("draft_url") or payload.get("Draft_Video_URL") or "",
            "Draft_Drive_File_ID": payload.get("draft_file_id") or payload.get("Draft_Drive_File_ID") or "",
            "Status": "Failed",
            "Note": f"Phase 4 compact helper failed: {safe_error_message(exc)}",
        }

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
