from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def review_secret() -> str:
    secret = os.getenv("REVIEW_TOKEN_SECRET", "").strip()
    if not secret:
        raise RuntimeError("REVIEW_TOKEN_SECRET is required for local review links")
    return secret


def review_public_base_url() -> str:
    return (os.getenv("REVIEW_PUBLIC_BASE_URL", "").strip() or "http://localhost:8000").rstrip("/")


def relative_review_path(video_path: str | Path) -> str:
    resolved = resolve_path(video_path)
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Review video must be inside project root: {resolved}") from exc


def sign_review_payload(post_id: str, relative_path: str) -> str:
    message = f"{post_id}\n{relative_path}".encode("utf-8")
    return hmac.new(review_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()


def encode_review_token(post_id: str, video_path: str | Path) -> str:
    relative_path = relative_review_path(video_path)
    payload = {
        "id": post_id,
        "path": relative_path,
        "sig": sign_review_payload(post_id, relative_path),
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_review_token(token: str) -> dict[str, str]:
    padded = token + ("=" * (-len(token) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Invalid review token") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid review token payload")
    return {key: str(payload.get(key, "")) for key in ["id", "path", "sig"]}


def validate_review_token(post_id: str, token: str) -> Path:
    payload = decode_review_token(token)
    token_post_id = payload.get("id", "")
    relative_path = payload.get("path", "")
    signature = payload.get("sig", "")
    if token_post_id != post_id:
        raise RuntimeError("Review token does not match post ID")
    expected = sign_review_payload(token_post_id, relative_path)
    if not hmac.compare_digest(signature, expected):
        raise RuntimeError("Review token signature is invalid")

    video_path = resolve_path(relative_path)
    runtime_root = (PROJECT_ROOT / "runtime").resolve()
    try:
        video_path.relative_to(runtime_root)
    except ValueError as exc:
        raise RuntimeError("Review video path is outside runtime") from exc
    if not video_path.exists():
        raise RuntimeError(f"Review video not found: {video_path}")
    return video_path


def build_review_links(post_id: str, video_path: str | Path) -> dict[str, str]:
    token = encode_review_token(post_id, video_path)
    quoted_id = quote(post_id, safe="")
    quoted_token = quote(token, safe="")
    base_url = review_public_base_url()
    review_url = f"{base_url}/review/{quoted_id}?token={quoted_token}"
    video_url = f"{base_url}/review/{quoted_id}/video?token={quoted_token}"
    return {
        "review_url": review_url,
        "video_url": video_url,
        "token": token,
    }
