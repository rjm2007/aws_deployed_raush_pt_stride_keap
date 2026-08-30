from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..security import DashboardActor, require_dashboard_auth
from ..services.provider_http import ProviderError
from ..services.twilio_service import TwilioService
from ..worker import format_phone, materialize_cadence

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
Actor = Annotated[DashboardActor, Depends(require_dashboard_auth)]


class CadenceAction(BaseModel):
    action: Literal["pause", "resume"]


class ReviewAction(BaseModel):
    resolution: str = Field(min_length=2, max_length=500)


class OutreachUpdate(BaseModel):
    scheduled_for: datetime


class CadenceStepUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=2, max_length=300)
    is_active: bool | None = None


class TemplateUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=1600)


class ManualSmsRequest(BaseModel):
    body: str = Field(min_length=1, max_length=1600)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class LeadCreate(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=10, max_length=32)
    email: str | None = Field(
        default=None,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    date_of_birth: date
    referred_by: str | None = Field(default=None, max_length=200)
    lead_type: Literal["Physical Therapy", "Wellness"]
    location: Literal["Dana Point", "Laguna Niguel", "Mission Viejo"]
    owner: str = Field(min_length=1, max_length=200)
    contact_consent: Literal[True]

    @field_validator("first_name", "last_name", "owner")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("date_of_birth")
    @classmethod
    def valid_date_of_birth(cls, value: date) -> date:
        if value > datetime.now(UTC).date():
            raise ValueError("date_of_birth cannot be in the future")
        return value


def _stage(row: dict) -> str:
    if row["status"] == "booked":
        return "booked"
    if row["needs_review"] or row["status"] == "needs_attention":
        return "attention"
    if row["status"] == "new" or row["cadence_state"] == "pending":
        return "new"
    return "cadence"


def _lead(row: dict) -> dict:
    next_status = row.get("next_event_status")
    next_step = row.get("next_step")
    if next_status in {"attempted", "in_flight"}:
        next_step = f"Awaiting result: {next_step or row.get('next_channel', 'outreach')}"
    return {
        "id": str(row["id"]),
        "display_id": f"RPT-{str(row['id']).split('-')[0].upper()}",
        "full_name": row["full_name"],
        "phone": row.get("phone_e164"),
        "email": row.get("email"),
        "source": row.get("source_system"),
        "status": row["status"],
        "stage": _stage(row),
        "cadence_state": row["cadence_state"],
        "needs_review": row["needs_review"],
        "review_reason": row.get("review_reason"),
        "next_event_id": row.get("next_event_id"),
        "next_event_status": next_status,
        "next_step": next_step,
        "next_channel": row.get("next_channel"),
        "next_scheduled_for": row.get("next_scheduled_for"),
        "cadence_progress": row.get("cadence_progress", 0),
        "cadence_total": row.get("cadence_total", 0),
        "created_at": row["created_at"],
        "last_contacted_at": row.get("last_contacted_at"),
        "date_of_birth": row.get("date_of_birth"),
        "referred_by": row.get("referred_by"),
        "lead_type": row.get("lead_type"),
        "location": row.get("location"),
        "owner": row.get("owner"),
        "is_test": bool(row.get("is_test")),
    }


def _audit(
    conn,
    actor: DashboardActor,
    practice_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict | None = None,
) -> None:
    conn.execute(
        "insert into dashboard_audit_log(practice_id,actor_id,actor_email,action,entity_type,"
        "entity_id,metadata) values(%s,%s,%s,%s,%s,%s,%s)",
        (
            practice_id,
            actor.user_id,
            actor.email,
            action,
            entity_type,
            entity_id,
            Jsonb(metadata or {}),
        ),
    )


@router.get("/snapshot")
def dashboard_snapshot(actor: Actor):
    del actor
    with transaction() as conn:
        rows = conn.execute(
            "select l.id,l.full_name,l.phone_e164,l.email,l.source_system,l.status,l.cadence_state,"
            "l.needs_review,l.review_reason,l.created_at,l.last_contacted_at,l.date_of_birth,"
            "l.referred_by,l.lead_type,l.location,l.owner,l.is_test,"
            "(select count(*) from outreach_events progress where progress.lead_id=l.id "
            "and progress.status<>'planned') as cadence_progress,"
            "(select count(*) from outreach_events total where total.lead_id=l.id) as cadence_total,"
            "next_event.id as next_event_id,next_event.status as next_event_status,"
            "next_event.description as next_step,next_event.channel as next_channel,"
            "next_event.scheduled_for as next_scheduled_for from leads l left join lateral ("
            "select oe.id,cs.description,oe.channel,oe.scheduled_for,oe.status from outreach_events oe "
            "left join cadence_steps cs on cs.id=oe.cadence_step_id where oe.lead_id=l.id "
            "and oe.status in ('planned','in_flight','attempted') order by "
            "case when oe.status='planned' then 0 else 1 end,oe.scheduled_for nulls last,oe.id limit 1"
            ") next_event on true order by l.created_at desc limit 250"
        ).fetchall()
        leads = [_lead(row) for row in rows]
        appointments = conn.execute(
            "select a.id,a.lead_id,l.full_name,a.state,a.start_utc,a.end_utc,a.location_id,"
            "a.appointment_type_id,a.needs_staff_review from appointments a join leads l on l.id=a.lead_id "
            "where a.state in ('booking','scheduled','unknown') order by a.start_utc nulls last limit 100"
        ).fetchall()
        cadence = conn.execute(
            "select cs.id,cs.step_order,cs.day_offset,cs.channel,cs.key,cs.description,cs.is_active "
            "from cadence_steps cs join practices p on p.id=cs.practice_id where p.slug='rausch-pt' "
            "order by cs.day_offset,cs.step_order"
        ).fetchall()
        templates = conn.execute(
            "select mt.id,mt.key,mt.body,mt.is_active,cs.day_offset,cs.description "
            "from message_templates mt join practices p on p.id=mt.practice_id "
            "left join cadence_steps cs on cs.id=mt.cadence_step_id where p.slug='rausch-pt' "
            "and mt.channel='sms' order by cs.day_offset,cs.step_order"
        ).fetchall()
        system = conn.execute(
            "select (select count(*) from provider_events where processed_at is null) as provider_queue,"
            "(select count(*) from integration_outbox where status in ('pending','sending')) as handoff_queue,"
            "(select count(*) from outreach_events where status='unknown') as unknown_events,"
            "(select count(*) from leads where needs_review) as review_queue"
        ).fetchone()

    counts = {"new": 0, "cadence": 0, "attention": 0, "booked": 0}
    for lead in leads:
        counts[lead["stage"]] += 1
    settings = get_settings()
    return {
        "generated_at": datetime.now(UTC),
        "counts": counts,
        "leads": leads,
        "appointments": appointments,
        "cadence": cadence,
        "templates": templates,
        "providers": [
            {"name": "Vapi", "mode": settings.mode("vapi"), "status": "configured", "balance": None},
            {"name": "Twilio", "mode": settings.mode("twilio"), "status": "configured", "balance": None},
            {"name": "Stride", "mode": settings.mode("stride"), "status": "configured", "balance": None},
            {"name": "Keap", "mode": settings.mode("keap"), "status": "configured", "balance": None},
        ],
        "system": system,
    }


@router.post("/leads", status_code=201)
def create_dashboard_lead(payload: LeadCreate, actor: Actor):
    phone = format_phone(payload.phone)
    if not phone:
        raise HTTPException(status_code=422, detail="phone must be a valid E.164 or US number")
    settings = get_settings()
    synthetic = settings.test_mode and settings.app_env.lower() in {"development", "test"}
    test_run_id = uuid4() if synthetic else None

    with transaction() as conn:
        practice = conn.execute(
            "select id,timezone from practices where slug='rausch-pt'"
        ).fetchone()
        if not practice:
            raise HTTPException(status_code=503, detail="practice is not configured")

        existing = conn.execute(
            "select id from leads where practice_id=%s and source_system='dashboard' "
            "and external_referral_id=%s",
            (practice["id"], payload.idempotency_key),
        ).fetchone()
        if existing:
            lead_id = existing["id"]
        else:
            inserted = conn.execute(
                "insert into leads(practice_id,source_system,external_referral_id,first_name,"
                "last_name,full_name,phone_e164,phone_original,email,date_of_birth,timezone,"
                "line_type,consent_captured_at,consent_source,consent_reference,"
                "consent_text_version,status,cadence_state,lead_type,referred_by,location,owner,"
                "is_test,test_run_id) "
                "values(%s,'dashboard',%s,%s,%s,%s,%s,%s,%s,%s,%s,'unknown',now(),"
                "%s,%s,'dashboard-manual-v1','new','pending',%s,%s,%s,%s,%s,%s) "
                "returning id",
                (
                    practice["id"],
                    payload.idempotency_key,
                    payload.first_name,
                    payload.last_name,
                    f"{payload.first_name} {payload.last_name}",
                    phone,
                    payload.phone,
                    payload.email.strip().lower() if payload.email else None,
                    payload.date_of_birth,
                    practice["timezone"],
                    "dashboard_staff_attestation",
                    f"dashboard:{payload.idempotency_key}",
                    payload.lead_type,
                    payload.referred_by.strip() if payload.referred_by else None,
                    payload.location,
                    payload.owner,
                    synthetic,
                    test_run_id,
                ),
            ).fetchone()
            lead_id = inserted["id"]
            event_count = materialize_cadence(
                conn, str(lead_id), practice["id"], datetime.now(UTC).date()
            )
            if not event_count:
                raise HTTPException(status_code=409, detail="no active cadence is configured")
            _audit(
                conn,
                actor,
                practice["id"],
                "lead.created",
                "lead",
                str(lead_id),
                {
                    "lead_type": payload.lead_type,
                    "location": payload.location,
                    "cadence_events": event_count,
                },
            )

        row = conn.execute(
            "select l.id,l.full_name,l.phone_e164,l.email,l.source_system,l.status,l.cadence_state,"
            "l.needs_review,l.review_reason,l.created_at,l.last_contacted_at,l.date_of_birth,"
            "l.referred_by,l.lead_type,l.location,l.owner,l.is_test,"
            "(select count(*) from outreach_events progress where progress.lead_id=l.id "
            "and progress.status<>'planned') as cadence_progress,"
            "(select count(*) from outreach_events total where total.lead_id=l.id) as cadence_total,"
            "next_event.id as next_event_id,next_event.status as next_event_status,"
            "next_event.description as next_step,next_event.channel as next_channel,"
            "next_event.scheduled_for as next_scheduled_for from leads l left join lateral ("
            "select oe.id,cs.description,oe.channel,oe.scheduled_for,oe.status from outreach_events oe "
            "left join cadence_steps cs on cs.id=oe.cadence_step_id where oe.lead_id=l.id "
            "and oe.status in ('planned','in_flight','attempted') order by "
            "case when oe.status='planned' then 0 else 1 end,oe.scheduled_for nulls last,oe.id limit 1"
            ") next_event on true where l.id=%s",
            (lead_id,),
        ).fetchone()
    return _lead(row)


@router.get("/leads/{lead_id}")
def dashboard_lead(lead_id: UUID, actor: Actor):
    del actor
    with transaction() as conn:
        row = conn.execute(
            "select id,practice_id,full_name,first_name,last_name,phone_e164,email,date_of_birth,"
            "source_system,status,status_reason,cadence_state,call_opt_out,sms_opt_out,needs_review,"
            "review_reason,created_at,updated_at,last_contacted_at,callback_requested_at,referred_by,"
            "lead_type,location,owner,is_test from leads where id=%s",
            (lead_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="lead not found")
        events = conn.execute(
            "select oe.id,oe.cadence_step_id,oe.channel,oe.day_offset,oe.status,oe.scheduled_for,"
            "oe.executed_at,oe.outcome,cs.description from outreach_events oe left join cadence_steps cs "
            "on cs.id=oe.cadence_step_id where oe.lead_id=%s order by oe.scheduled_for nulls last,oe.id",
            (lead_id,),
        ).fetchall()
        messages = conn.execute(
            "select id,direction,body,occurred_at,delivered_at,delivery_status from sms_messages "
            "where lead_id=%s order by occurred_at,id",
            (lead_id,),
        ).fetchall()
        calls = conn.execute(
            "select id,dialed_at,ended_at,duration_seconds,answer_state,ended_reason,transcript_text,"
            "summary_text from call_logs where lead_id=%s order by dialed_at desc,id desc",
            (lead_id,),
        ).fetchall()
        appointments = conn.execute(
            "select id,state,start_utc,end_utc,booked_at,location_id,appointment_type_id,"
            "needs_staff_review from appointments where lead_id=%s order by booked_at desc",
            (lead_id,),
        ).fetchall()
        status_history = conn.execute(
            "select from_status,to_status,reason,source,changed_at from lead_status_history "
            "where lead_id=%s order by changed_at desc limit 100",
            (lead_id,),
        ).fetchall()
        overrides = conn.execute(
            "select lmo.message_template_id,lmo.body,lmo.updated_at from lead_message_overrides lmo "
            "where lmo.lead_id=%s",
            (lead_id,),
        ).fetchall()
    detail = dict(row)
    detail["id"] = str(detail["id"])
    detail["display_id"] = f"RPT-{str(row['id']).split('-')[0].upper()}"
    detail["stage"] = _stage(row)
    detail["phone"] = detail.get("phone_e164")
    detail["source"] = detail.get("source_system")
    detail["cadence_progress"] = sum(event["status"] != "planned" for event in events)
    detail["cadence_total"] = len(events)
    next_event = next((event for event in events if event["status"] == "planned"), None)
    if not next_event:
        next_event = next(
            (event for event in events if event["status"] in {"in_flight", "attempted"}), None
        )
    detail["next_event_id"] = next_event["id"] if next_event else None
    detail["next_event_status"] = next_event["status"] if next_event else None
    detail["next_channel"] = next_event["channel"] if next_event else None
    detail["next_scheduled_for"] = next_event["scheduled_for"] if next_event else None
    detail["next_step"] = next_event["description"] if next_event else None
    if next_event and next_event["status"] in {"in_flight", "attempted"}:
        detail["next_step"] = f"Awaiting result: {detail['next_step'] or detail['next_channel']}"
    return {
        "lead": detail,
        "events": events,
        "messages": messages,
        "calls": calls,
        "appointments": appointments,
        "history": status_history,
        "message_overrides": overrides,
    }


@router.post("/leads/{lead_id}/cadence")
def update_lead_cadence(lead_id: UUID, payload: CadenceAction, actor: Actor):
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,status,cadence_state from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        if payload.action == "resume" and lead["status"] in {
            "booked", "declined", "do_not_contact", "invalid_phone"
        }:
            raise HTTPException(status_code=409, detail="terminal leads cannot resume cadence")
        new_state = "paused" if payload.action == "pause" else "active"
        conn.execute("update leads set cadence_state=%s where id=%s", (new_state, lead_id))
        _audit(conn, actor, lead["practice_id"], f"cadence.{payload.action}", "lead", str(lead_id))
    return {"status": "updated", "cadence_state": new_state}


@router.patch("/leads/{lead_id}/outreach-events/{event_id}")
def update_outreach_event(
    lead_id: UUID, event_id: int, payload: OutreachUpdate, actor: Actor
):
    if payload.scheduled_for.tzinfo is None or payload.scheduled_for <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="scheduled_for must be a future timezone-aware value")
    with transaction() as conn:
        event = conn.execute(
            "select oe.id,l.practice_id from outreach_events oe join leads l on l.id=oe.lead_id "
            "where oe.id=%s and oe.lead_id=%s and oe.status='planned' for update",
            (event_id, lead_id),
        ).fetchone()
        if not event:
            raise HTTPException(status_code=409, detail="only this lead's planned events can be edited")
        conn.execute(
            "update outreach_events set scheduled_for=%s where id=%s",
            (payload.scheduled_for, event_id),
        )
        _audit(
            conn, actor, event["practice_id"], "cadence.local_schedule_updated", "outreach_event",
            str(event_id), {"lead_id": str(lead_id)},
        )
    return {"status": "updated", "scheduled_for": payload.scheduled_for}


@router.post("/review/{lead_id}/resolve")
def resolve_review(lead_id: UUID, payload: ReviewAction, actor: Actor):
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,needs_review from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        conn.execute(
            "update leads set needs_review=false,review_resolved_at=now(),review_reason=null where id=%s",
            (lead_id,),
        )
        _audit(
            conn, actor, lead["practice_id"], "review.resolved", "lead", str(lead_id),
            {"resolution": payload.resolution},
        )
    return {"status": "resolved"}


@router.patch("/cadence-steps/{step_id}")
def update_cadence_step(step_id: int, payload: CadenceStepUpdate, actor: Actor):
    if payload.description is None and payload.is_active is None:
        raise HTTPException(status_code=422, detail="no cadence fields supplied")
    with transaction() as conn:
        step = conn.execute(
            "select id,practice_id from cadence_steps where id=%s for update", (step_id,)
        ).fetchone()
        if not step:
            raise HTTPException(status_code=404, detail="cadence step not found")
        conn.execute(
            "update cadence_steps set description=coalesce(%s,description),"
            "is_active=coalesce(%s,is_active) where id=%s",
            (payload.description, payload.is_active, step_id),
        )
        _audit(conn, actor, step["practice_id"], "cadence.global_updated", "cadence_step", str(step_id))
    return {"status": "updated"}


@router.patch("/message-templates/{template_id}")
def update_message_template(template_id: int, payload: TemplateUpdate, actor: Actor):
    with transaction() as conn:
        template = conn.execute(
            "select id,practice_id from message_templates where id=%s and channel='sms' for update",
            (template_id,),
        ).fetchone()
        if not template:
            raise HTTPException(status_code=404, detail="SMS template not found")
        conn.execute("update message_templates set body=%s where id=%s", (payload.body, template_id))
        _audit(
            conn, actor, template["practice_id"], "sms_template.global_updated",
            "message_template", str(template_id),
        )
    return {"status": "updated"}


@router.put("/leads/{lead_id}/message-overrides/{template_id}")
def set_message_override(
    lead_id: UUID, template_id: int, payload: TemplateUpdate, actor: Actor
):
    with transaction() as conn:
        lead = conn.execute("select practice_id from leads where id=%s", (lead_id,)).fetchone()
        template = conn.execute(
            "select id from message_templates where id=%s and channel='sms'", (template_id,)
        ).fetchone()
        if not lead or not template:
            raise HTTPException(status_code=404, detail="lead or SMS template not found")
        conn.execute(
            "insert into lead_message_overrides(lead_id,message_template_id,body,updated_by) "
            "values(%s,%s,%s,%s) on conflict(lead_id,message_template_id) do update set "
            "body=excluded.body,updated_by=excluded.updated_by",
            (lead_id, template_id, payload.body, actor.user_id),
        )
        _audit(
            conn, actor, lead["practice_id"], "sms_template.local_updated",
            "lead", str(lead_id), {"template_id": template_id},
        )
    return {"status": "updated"}


@router.delete("/leads/{lead_id}/message-overrides/{template_id}")
def remove_message_override(lead_id: UUID, template_id: int, actor: Actor):
    with transaction() as conn:
        lead = conn.execute("select practice_id from leads where id=%s", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        conn.execute(
            "delete from lead_message_overrides where lead_id=%s and message_template_id=%s",
            (lead_id, template_id),
        )
        _audit(
            conn, actor, lead["practice_id"], "sms_template.local_reset",
            "lead", str(lead_id), {"template_id": template_id},
        )
    return {"status": "reset"}


@router.post("/leads/{lead_id}/sms")
def send_manual_sms(
    request: Request, lead_id: UUID, payload: ManualSmsRequest, actor: Actor
):
    trace = WorkflowTrace("dashboard_manual_sms", "api", request.headers.get("x-trace-id", ""))
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,phone_e164,status,sms_opt_out from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        blocked = (
            not lead["phone_e164"]
            or lead["sms_opt_out"]
            or lead["status"] == "do_not_contact"
            or conn.execute(
                "select 1 from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],)
            ).fetchone()
        )
        if blocked:
            raise HTTPException(status_code=409, detail="SMS is blocked by contact rules")
        inserted = conn.execute(
            "insert into dashboard_sms_requests(lead_id,idempotency_key,body,status,requested_by) "
            "values(%s,%s,%s,'sending',%s) on conflict(idempotency_key) do nothing returning id",
            (lead_id, payload.idempotency_key, payload.body, actor.user_id),
        ).fetchone()
        if not inserted:
            existing = conn.execute(
                "select status,provider_ref from dashboard_sms_requests where idempotency_key=%s",
                (payload.idempotency_key,),
            ).fetchone()
            return {"status": existing["status"], "provider_ref": existing["provider_ref"], "duplicate": True}
        request_id = inserted["id"]

    try:
        provider_ref = TwilioService().send_sms(trace, lead["phone_e164"], payload.body)
    except ProviderError as exc:
        status = "unknown" if exc.ambiguous else "failed"
        with transaction() as conn:
            conn.execute(
                "update dashboard_sms_requests set status=%s,failure_category=%s where id=%s",
                (status, exc.code, request_id),
            )
            if status == "unknown":
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                    ("manual SMS result requires provider reconciliation", lead_id),
                )
            _audit(
                conn, actor, lead["practice_id"], f"sms.manual_{status}", "lead", str(lead_id),
                {"request_id": str(request_id)},
            )
        trace.complete(outcome=status)
        raise HTTPException(
            status_code=502, detail="SMS result is unknown and requires review" if status == "unknown" else "SMS was rejected"
        ) from exc

    with transaction() as conn:
        conn.execute(
            "update dashboard_sms_requests set status='sent',provider_ref=%s where id=%s",
            (provider_ref, request_id),
        )
        conn.execute(
            "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,provider_message_id) "
            "values(%s,'outbound',%s,now(),'sent',%s)",
            (lead_id, payload.body, provider_ref),
        )
        _audit(
            conn, actor, lead["practice_id"], "sms.manual_sent", "lead", str(lead_id),
            {"request_id": str(request_id)},
        )
    trace.complete(outcome="sent")
    return {"status": "sent", "provider_ref": provider_ref, "duplicate": False}
