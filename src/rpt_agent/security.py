from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from .config import get_settings


async def require_vapi_auth(request: Request) -> None:
    settings = get_settings()
    expected = settings.vapi_webhook_secret
    supplied = request.headers.get("authorization", "")
    token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
    legacy_secret = request.headers.get("x-vapi-secret", "")
    if expected and (
        hmac.compare_digest(token, expected) or hmac.compare_digest(legacy_secret, expected)
    ):
        return
    timestamp = request.headers.get("x-vapi-timestamp", "")
    signature = request.headers.get("x-vapi-signature", "")
    try:
        fresh = abs(int(time.time()) - int(timestamp)) <= 300
    except ValueError:
        fresh = False
    if settings.vapi_hmac_secret and signature and fresh:
        body = await request.body()
        expected_signature = hmac.new(
            settings.vapi_hmac_secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(signature.removeprefix("sha256="), expected_signature):
            return
    if expected:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON remains unauthenticated
            body = {}
        candidates = [body.get("vapi_secret")] if isinstance(body, dict) else []
        message = body.get("message") if isinstance(body, dict) else None
        message = message if isinstance(message, dict) else {}
        for key in ("toolCallList", "toolCalls"):
            for item in message.get(key) or []:
                if not isinstance(item, dict):
                    continue
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                arguments = item.get("arguments") or item.get("parameters") or function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        import json

                        arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                if isinstance(arguments, dict):
                    candidates.append(arguments.get("vapi_secret"))
        if any(
            isinstance(candidate, str) and hmac.compare_digest(candidate, expected)
            for candidate in candidates
        ):
            return
    raise HTTPException(status_code=401, detail="invalid Vapi credentials")


async def require_twilio_auth(request: Request, form: dict[str, str]) -> None:
    """Validate Twilio's HMAC-SHA1 request signature."""
    settings = get_settings()
    auth_token = settings.twilio_auth_token
    signature = request.headers.get("x-twilio-signature", "")
    if settings.mode("twilio") == "mock" and signature == "mock-valid":
        return
    if not auth_token or not signature:
        raise HTTPException(status_code=401, detail="missing Twilio signature")
    if settings.public_base_url:
        public_url = f"{settings.public_base_url.rstrip('/')}{request.url.path}"
        if request.url.query:
            public_url = f"{public_url}?{request.url.query}"
    else:
        public_url = str(request.url)
    data = public_url + "".join(f"{key}{form[key]}" for key in sorted(form))
    digest = base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="invalid Twilio signature")


def sign_slot(payload: str, expires_at: int) -> str:
    content = f"{payload}|{expires_at}"
    signature = hmac.new(
        get_settings().slot_token_secret.encode(), content.encode(), hashlib.sha256
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{content}|{signature}".encode()).decode().rstrip("=")


def verify_slot(token: str) -> tuple[str, int]:
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        payload, expires, signature = decoded.rsplit("|", 2)
        content = f"{payload}|{expires}"
        expected = hmac.new(
            get_settings().slot_token_secret.encode(), content.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expires) < int(time.time()):
            raise ValueError
        return payload, int(expires)
    except Exception as exc:
        raise ValueError("invalid or expired slot token") from exc


def sign_handoff(body: bytes) -> str:
    return hmac.new(get_settings().keap_handoff_secret.encode(), body, hashlib.sha256).hexdigest()
