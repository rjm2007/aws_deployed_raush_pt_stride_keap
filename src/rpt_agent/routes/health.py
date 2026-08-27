from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db import transaction

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok", "service": "rpt-agent-api"}


@router.get("/ready")
def ready():
    errors = get_settings().runtime_errors("api")
    if not errors:
        try:
            with transaction() as conn:
                conn.execute("select 1").fetchone()
                if get_settings().mode("stride") == "real":
                    unverified = conn.execute(
                        "select count(*) as count from practices p join practice_settings ps "
                        "on ps.practice_id=p.id where p.is_active and not ps.stride_booking_enabled"
                    ).fetchone()
                    if unverified and unverified["count"]:
                        errors.append(
                            "Stride booking configuration is not verified for every active practice"
                        )
        except Exception as exc:  # noqa: BLE001 - readiness reports dependency failure
            errors.append(f"database unavailable: {type(exc).__name__}")
    if errors:
        return JSONResponse(status_code=503, content={"status": "not_ready", "errors": errors})
    return {"status": "ready", "service": "rpt-agent-api"}
