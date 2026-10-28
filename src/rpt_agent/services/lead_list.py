"""The Leads page, paged and filtered in the database (2026-10-28).

The snapshot only carries the newest 250 leads, so lead 251 and older could not be listed, searched
or seen on the board. The list and the board now ask for one page at a time, with search and filters
applied here across every lead, and real totals.

Source is where a row started: a Google Sheet lead stays "sheet" after it is booked. "stride" rows
are Stride patients with no linked lead (front desk bookings, `services/stride_leads.py`); they are
view only and never on the board.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .appointment_views import BOOKED_STATUSES, optional_tables, stride_status
from .lead_stage import STAGE_SQL

STAGES = ("new", "cadence", "attention", "booked", "closed")
SHEET_SOURCE = "google_sheets"
LIST_LIMIT = 500
BOARD_LIMIT = 25
# A Stride visit still on the books (blank status is Stride's default, like appointment_views).
_ON_BOOKS = ",".join(f"'{s}'" for s in sorted(BOOKED_STATUSES | {""}))
STRIDE_BOOKED_SQL = (
    "exists(select 1 from stride_appointments sa where sa.practice_id=sp.practice_id "
    "and sa.patient_stride_id=sp.stride_id and sa.deleted_at is null and sa.start_utc>=now() "
    f"and lower(trim(coalesce(sa.status,''))) in ({_ON_BOOKS}))"
)
_STRIDE_BASE = "sp.practice_id=%s and sp.lead_id is null and sp.deleted_at is null"


def _like(text: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"


def _digits(q: str) -> str | None:
    # Only digits and phone punctuation are a phone search; "Ann 2" or "lead0007" is a name.
    if re.search(r"[^\d\s()+.-]", q):
        return None
    digits = re.sub(r"\D", "", q)
    return digits if len(digits) >= 3 else None


def lead_where(practice_id: int, *, q: str = "", owner: str | None = None, location: str | None = None,
               stages: list[str] | None = None, sheet_only: bool = False,
               with_stage: bool = True) -> tuple[str, list]:
    """WHERE clause over `leads l` for the list and the board."""
    sql, params = ["l.practice_id=%s"], [practice_id]
    if sheet_only:
        sql.append("l.source_system=%s")
        params.append(SHEET_SOURCE)
    if q.strip():
        like, digits = _like(q.strip()), _digits(q)
        sql.append("(l.full_name ilike %s or l.email ilike %s"
                   + (" or regexp_replace(coalesce(l.phone_e164,''),'\\D','','g') like %s" if digits else "")
                   + ")")
        params += [like, like] + ([f"%{digits}%"] if digits else [])
    if owner:
        sql.append("coalesce(nullif(l.owner,''),'Unassigned')=%s")
        params.append(owner)
    if location:
        sql.append("l.location=%s")
        params.append(location)
    if stages and with_stage:
        sql.append(STAGE_SQL + "=any(%s)")
        params.append(list(stages))
    return " and ".join(sql), params


def stride_where(practice_id: int, *, q: str = "", owner: str | None = None, location: str | None = None,
                 stages: list[str] | None = None) -> tuple[str, list] | None:
    """WHERE clause over `stride_patients sp` (unlinked patients), or None when the filters rule
    every Stride row out: they have no owner, and their only stage is Booked."""
    if owner or (stages and "booked" not in stages):
        return None
    sql, params = [_STRIDE_BASE], [practice_id]
    if q.strip():
        like, digits = _like(q.strip()), _digits(q)
        sql.append("(concat_ws(' ',sp.first_name,sp.last_name) ilike %s or sp.personal_email ilike %s "
                   "or sp.work_email ilike %s"
                   + (" or exists(select 1 from unnest(array[sp.mobile_phone,sp.home_phone,sp.work_phone,"
                      "sp.preferred_phone]) p where regexp_replace(coalesce(p,''),'\\D','','g') like %s)"
                      if digits else "")
                   + ")")
        params += [like, like, like] + ([f"%{digits}%"] if digits else [])
    if location:
        sql.append("exists(select 1 from stride_appointments sa where sa.practice_id=sp.practice_id "
                   "and sa.patient_stride_id=sp.stride_id and sa.deleted_at is null and sa.location_name ilike %s)")
        params.append(_like(location))
    if stages:
        sql.append(STRIDE_BOOKED_SQL)
    return " and ".join(sql), params


def list_page(conn, practice_id: int, *, source: str = "all", q: str = "", owner: str | None = None,
              location: str | None = None, stages: list[str] | None = None, offset: int = 0,
              limit: int = LIST_LIMIT) -> dict:
    """One page of keys, newest first: [("lead", uuid) | ("stride", stride_id)], plus totals."""
    leads_sql, leads_params = lead_where(practice_id, q=q, owner=owner, location=location, stages=stages)
    stride = stride_where(practice_id, q=q, owner=owner, location=location, stages=stages)
    if not optional_tables(conn)["imports"]:
        stride = None
    counts = conn.execute(
        "select count(*) as all_leads,count(*) filter (where l.source_system=%s) as sheet "
        "from leads l where " + leads_sql, [SHEET_SOURCE, *leads_params],
    ).fetchone()
    stride_total = 0
    if stride:
        stride_total = conn.execute(
            "select count(*) as n from stride_patients sp where " + stride[0], stride[1]).fetchone()["n"]
    sources = {"all": int(counts["all_leads"]) + int(stride_total), "sheet": int(counts["sheet"]),
               "stride": int(stride_total)}
    parts, params = [], []
    if source in ("all", "sheet"):
        where, where_params = lead_where(practice_id, q=q, owner=owner, location=location, stages=stages,
                                         sheet_only=source == "sheet")
        parts.append("select 'lead' as kind,l.id::text as key,l.created_at as added from leads l where " + where)
        params += where_params
    if source in ("all", "stride") and stride:
        parts.append("select 'stride' as kind,sp.stride_id::text as key,"
                     "coalesce(sp.stride_created_at,sp.created_at) as added "
                     "from stride_patients sp where " + stride[0])
        params += stride[1]
    keys = []
    if parts:
        keys = conn.execute(
            "select kind,key from (" + " union all ".join(parts) + ") page "
            "order by added desc nulls last,key desc offset %s limit %s",
            [*params, offset, limit],
        ).fetchall()
    return {"keys": [(r["kind"], r["key"]) for r in keys], "total": sources[source], "sources": sources}


def stride_rows(conn, practice_id: int, stride_ids: list[int]) -> dict[int, dict]:
    """List rows for unlinked Stride patients, keyed by Stride id. View only: no next step, no owner."""
    if not stride_ids:
        return {}
    rows = conn.execute(
        "select sp.stride_id,sp.first_name,sp.last_name,sp.date_of_birth,"
        "coalesce(sp.preferred_phone,sp.mobile_phone,sp.home_phone,sp.work_phone) as phone,"
        "coalesce(nullif(sp.personal_email,''),nullif(sp.work_email,'')) as email,"
        "coalesce(sp.stride_created_at,sp.created_at) as created_at,"
        + STRIDE_BOOKED_SQL + " as booked,"
        "exists(select 1 from stride_appointments sa where sa.practice_id=sp.practice_id "
        "and sa.patient_stride_id=sp.stride_id and sa.deleted_at is null) as has_visits "
        "from stride_patients sp where " + _STRIDE_BASE + " and sp.stride_id=any(%s)",
        (practice_id, stride_ids),
    ).fetchall()
    return {r["stride_id"]: _stride_row(r) for r in rows}


def _stride_row(r) -> dict:
    name = " ".join(part for part in (r.get("first_name"), r.get("last_name")) if part) or "Name unavailable"
    return {
        "kind": "stride", "id": f"stride-{r['stride_id']}", "stride_id": r["stride_id"], "full_name": name,
        "phone": r.get("phone"), "email": r.get("email"), "source": "stride",
        "date_of_birth": r.get("date_of_birth"), "created_at": r.get("created_at"),
        # Booked when a visit is still on the books; otherwise a plain label, never a lead stage.
        "stage": "booked" if r["booked"] else None,
        "stage_label": "Booked" if r["booked"] else "No upcoming visit" if r["has_visits"] else "No visits",
        "next_step": "View only", "owner": None,
    }


def stride_patient(conn, practice_id: int, stride_id: int) -> dict | None:
    """The read-only Stride patient view: who they are and their visits, newest first."""
    if not optional_tables(conn)["imports"]:
        return None
    row = conn.execute(
        "select sp.stride_id,sp.first_name,sp.last_name,sp.date_of_birth,"
        "coalesce(sp.preferred_phone,sp.mobile_phone,sp.home_phone,sp.work_phone) as phone,"
        "coalesce(nullif(sp.personal_email,''),nullif(sp.work_email,'')) as email,"
        "coalesce(sp.stride_created_at,sp.created_at) as created_at,"
        + STRIDE_BOOKED_SQL + " as booked,true as has_visits "
        "from stride_patients sp where " + _STRIDE_BASE + " and sp.stride_id=%s",
        (practice_id, stride_id),
    ).fetchone()
    if not row:
        return None
    visits = conn.execute(
        "select sa.stride_id,sa.start_utc,sa.appointment_type,sa.status,sa.clinician_name,sa.location_name "
        "from stride_appointments sa where sa.practice_id=%s and sa.patient_stride_id=%s "
        "and sa.deleted_at is null order by sa.start_utc desc nulls last limit 200",
        (practice_id, stride_id),
    ).fetchall()
    patient = _stride_row(row)
    if not visits:
        patient["stage_label"] = "No visits"
    # The clinic: of the next visit on the books, else of the latest one.
    now = datetime.now(UTC)
    upcoming = [v for v in visits if v["start_utc"] and v["start_utc"] >= now]
    patient["clinic"] = (upcoming[-1] if upcoming else visits[0])["location_name"] if visits else None
    return {
        "patient": patient,
        "visits": [{
            "id": v["stride_id"], "start_utc": v["start_utc"], "type": v["appointment_type"],
            "status": stride_status(v["status"]), "clinician": v["clinician_name"],
            "location": v["location_name"],
        } for v in visits],
    }


def board_page(conn, practice_id: int, *, q: str = "", owner: str | None = None, location: str | None = None,
               stages: list[str] | None = None, offset: int = 0, limit: int = BOARD_LIMIT) -> dict:
    """Per board column: the next `limit` lead ids (newest first) and the column's real total."""
    where, params = lead_where(practice_id, q=q, owner=owner, location=location, with_stage=False)
    wanted = list(stages or STAGES)
    rows = conn.execute(
        "with f as (select l.id,l.created_at," + STAGE_SQL + " as stage from leads l where " + where + "), "
        "r as (select id,stage,row_number() over (partition by stage order by created_at desc,id desc) as n "
        "from f where stage=any(%s)) "
        "select 'row' as kind,id::text as id,stage,n from r where n>%s and n<=%s "
        "union all select 'total',null,stage,count(*) from f group by stage order by kind,stage,n",
        [*params, wanted, offset, offset + limit],
    ).fetchall()
    totals = {r["stage"]: int(r["n"]) for r in rows if r["kind"] == "total"}
    return {stage: {"ids": [r["id"] for r in rows if r["kind"] == "row" and r["stage"] == stage],
                    "total": totals.get(stage, 0)} for stage in wanted}
