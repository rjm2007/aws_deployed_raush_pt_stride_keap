"""Idempotently configure the booking tools on the selected Vapi assistant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from rpt_agent.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_NAME = "RPT Booking API Auth"


def checked(response: httpx.Response) -> Any:
    if response.is_error:
        raise RuntimeError(f"Vapi returned HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def main() -> None:
    settings = get_settings()
    if not settings.vapi_api_key or not settings.vapi_assistant_id:
        raise RuntimeError("VAPI_API_KEY and VAPI_ASSISTANT_ID are required")
    if not settings.public_base_url.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must be a public HTTPS URL")

    headers = {"Authorization": f"Bearer {settings.vapi_api_key}"}
    with httpx.Client(base_url=settings.vapi_base_url, headers=headers, timeout=120) as client:
        credentials = checked(client.get("/credential", params={"limit": 100}))
        credential = next(
            (item for item in credentials if item.get("name") == CREDENTIAL_NAME), None
        )
        if credential is None:
            credential = checked(client.post("/credential", json={
                "provider": "custom-credential",
                "name": CREDENTIAL_NAME,
                "authenticationPlan": {
                    "type": "bearer",
                    "token": settings.vapi_webhook_secret,
                    "headerName": "X-Vapi-Secret",
                    "bearerPrefixEnabled": False,
                },
            }))
        credential_id = credential["id"]

        definitions = json.loads(
            (ROOT / "config" / "vapi_tools.json").read_text(encoding="utf-8")
        )
        existing_tools = checked(client.get("/tool", params={"limit": 100}))
        paths = {
            "check_availability": "/api/v1/tools/check-availability",
            "create_appointment": "/api/v1/tools/create-appointment",
            "update_lead_status": "/api/v1/webhooks/vapi/lead-status",
        }
        tool_ids: list[str] = []
        for definition in definitions:
            name = definition["function"]["name"]
            target_url = f"{settings.public_base_url.rstrip('/')}{paths[name]}"
            definition["server"] = {"url": target_url, "credentialId": credential_id}
            existing = next(
                (
                    item for item in existing_tools
                    if item.get("type") == "function"
                    and (item.get("function") or {}).get("name") == name
                ),
                None,
            )
            if existing:
                tool = checked(client.patch(f"/tool/{existing['id']}", json=definition))
            else:
                tool = checked(client.post("/tool", json=definition))
            tool_ids.append(tool["id"])

        assistant = checked(client.get(f"/assistant/{settings.vapi_assistant_id}"))
        built_in_ids = []
        for tool_id in (assistant.get("model") or {}).get("toolIds", []):
            tool = checked(client.get(f"/tool/{tool_id}"))
            if tool.get("type") in {"transferCall", "endCall"}:
                built_in_ids.append(tool_id)
        prompt = (ROOT / "config" / "vapi_assistant_prompt.md").read_text(encoding="utf-8")
        model = assistant["model"]
        payload = {
            "model": {
                "provider": model["provider"],
                "model": model["model"],
                "messages": [{"role": "system", "content": prompt}],
                "toolIds": [*built_in_ids, *tool_ids],
            },
            "server": {
                "url": f"{settings.public_base_url.rstrip('/')}/api/v1/vapi/webhook",
                "credentialId": credential_id,
                "timeoutSeconds": 20,
            },
            "serverMessages": ["end-of-call-report"],
        }
        checked(client.patch(f"/assistant/{settings.vapi_assistant_id}", json=payload))
        verified = checked(client.get(f"/assistant/{settings.vapi_assistant_id}"))

    print(json.dumps({
        "assistant_id": verified["id"],
        "assistant_name": verified.get("name"),
        "tool_ids": verified["model"].get("toolIds", []),
        "server_url": (verified.get("server") or {}).get("url"),
        "credential_id": credential_id,
    }, indent=2))


if __name__ == "__main__":
    main()
