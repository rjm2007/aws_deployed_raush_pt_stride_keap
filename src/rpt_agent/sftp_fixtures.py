from __future__ import annotations

import csv
from pathlib import Path

from .observability import WorkflowTrace


def load_stride_fixtures(trace: WorkflowTrace, root: Path = Path("fixtures/stride")) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    trace.log("fixture_import_started", fixture_root=str(root))
    for name in ("patients", "cases", "users", "locations"):
        path = root / f"{name}.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            result[name] = list(csv.DictReader(stream))
        trace.log("fixture_file_loaded", fixture=name, row_count=len(result[name]))
    trace.complete(file_count=len(result))
    return result

