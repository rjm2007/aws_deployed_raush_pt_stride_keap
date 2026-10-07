"""Stride CSV parsing rules that need no database."""

from datetime import UTC, date, datetime

import pytest

from rpt_agent.services.stride_import import (
    StrideFileError,
    file_sort_key,
    is_cancelled,
    normalize_phone,
    parse_file_name,
    read_rows,
)


def test_file_name_gives_entity_and_export_time():
    entity, exported = parse_file_name("patients_20260409153530.csv")
    assert entity == "patients"
    assert exported == datetime(2026, 4, 9, 15, 35, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "name",
    [
        "perform_demo_patients (1).csv",  # the old sample export naming
        "patients_2026040915353.csv",  # 13 digits
        "visits_20260409153530.csv",  # unknown entity
        "patients_20261399153530.csv",  # month 13
        "patients_20260409153530.CSV.part",
    ],
)
def test_unusable_file_names_are_rejected(name):
    with pytest.raises(StrideFileError):
        parse_file_name(name)


def test_files_sort_oldest_export_first_then_parents_before_children():
    names = [
        "appointments_20260409160000.csv",
        "notes_20260409153530.csv",
        "patients_20260409160000.csv",
        "appointments_20260409153530.csv",
        "patients_20260409153530.csv",
        "locations_20260409153530.csv",
    ]
    assert sorted(names, key=file_sort_key) == [
        "locations_20260409153530.csv",
        "patients_20260409153530.csv",
        "appointments_20260409153530.csv",
        "notes_20260409153530.csv",
        "patients_20260409160000.csv",
        "appointments_20260409160000.csv",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(561) 555-0100", "+15615550100"),
        ("561.555.0100", "+15615550100"),
        ("1-561-555-0100", "+15615550100"),
        ("+44 20 7946 0958", "+442079460958"),
        ("555-0100", None),  # too short to dial
        ("(061) 555-0100", None),  # US area codes never start with 0 or 1
        ("", None),
        (None, None),
    ],
)
def test_phone_numbers_become_e164(raw, expected):
    assert normalize_phone(raw) == expected


def test_missing_required_column_rejects_the_whole_file():
    content = b"Id,First Name,Last Name,Action\n1,A,B,Upsert\n"
    with pytest.raises(StrideFileError, match="Date Of Birth"):
        read_rows(content, "patients")


def test_empty_file_is_rejected():
    with pytest.raises(StrideFileError, match="empty"):
        read_rows(b"", "patients")


def test_byte_order_mark_and_windows_encoding_are_read():
    header = "Id,First Name,Last Name,Date Of Birth,Modified Date Time,Action\n"
    bom = ("﻿" + header + "1,Zoë,B,1990-01-01,2026-01-01 00:00:00,Upsert\n").encode()
    assert read_rows(bom, "patients")[0]["First Name"] == "Zoë"
    cp1252 = (header + "1,Zoë,B,1990-01-01,2026-01-01 00:00:00,Upsert\n").encode("cp1252")
    assert read_rows(cp1252, "patients")[0]["First Name"] == "Zoë"


def test_commas_and_quotes_inside_fields_stay_in_one_column():
    content = (
        b"Id,First Name,Last Name,Date Of Birth,Address 1,Modified Date Time,Action\n"
        b'1,A,"O""Neil, Jr",1990-01-01,"10 Main St, Apt 2",2026-01-01 00:00:00,Upsert\n'
    )
    row = read_rows(content, "patients")[0]
    assert row["Last Name"] == 'O"Neil, Jr'
    assert row["Address 1"] == "10 Main St, Apt 2"
    assert row["Action"] == "Upsert"


def test_cancel_statuses_cover_words_and_api_codes():
    assert all(is_cancelled(value) for value in ("Cancel", "Late Cancel", " cancel ", "A", "L"))
    assert not any(is_cancelled(value) for value in ("Unmarked", "Checked In", "No Show", None))


def test_date_only_values_stay_dates():
    # Guard: DOB must never shift a day through timezone handling.
    from rpt_agent.services.stride_import import _date

    assert _date("1985-04-12") == date(1985, 4, 12)


def test_each_entity_lists_exactly_the_columns_its_parser_returns():
    from zoneinfo import ZoneInfo

    from rpt_agent.services.stride_import import ENTITIES

    row = {"Date": "2026-01-01", "Start Time": "9:00 AM", "End Time": "10:00 AM"}
    for name, spec in ENTITIES.items():
        assert tuple(spec.columns(row, ZoneInfo("America/New_York"))) == spec.fields, name
