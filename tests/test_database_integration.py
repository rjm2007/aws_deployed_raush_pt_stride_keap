import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def connection():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    with psycopg.connect(url) as conn:
        yield conn


def test_database_has_active_booking_uniqueness(connection):
    rows = connection.execute(
        "select indexname from pg_indexes where schemaname='public' and indexname='idx_one_active_appointment_per_lead'"
    ).fetchall()
    assert rows


def test_concurrent_active_booking_insert_allows_only_one(connection):
    url = os.environ["TEST_DATABASE_URL"]
    slug = f"test-{uuid4().hex}"
    practice_id = connection.execute(
        "insert into practices(name,slug) values('Test Practice',%s) returning id", (slug,)
    ).fetchone()[0]
    lead_id = connection.execute(
        "insert into leads(practice_id,full_name,status) values(%s,'Synthetic Concurrent','in_progress') returning id",
        (practice_id,),
    ).fetchone()[0]
    connection.commit()

    def create_booking(key: str) -> bool:
        try:
            with psycopg.connect(url) as conn:
                conn.execute(
                    "insert into appointments(lead_id,practice_id,state,booking_key) values(%s,%s,'booking',%s)",
                    (lead_id, practice_id, key),
                )
            return True
        except psycopg.errors.UniqueViolation:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create_booking, ("slot-a", "slot-b")))
        assert sorted(results) == [False, True]
    finally:
        connection.execute("delete from appointments where lead_id=%s", (lead_id,))
        connection.execute("delete from leads where id=%s", (lead_id,))
        connection.execute("delete from practices where id=%s", (practice_id,))
        connection.commit()
