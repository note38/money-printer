"""Persistent CSV queue of video subjects/scripts for batch WebUI generation."""

from __future__ import annotations

import csv
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from app.utils import utils

QUEUE_FILENAME = "content_queue.csv"
FIELDNAMES = (
    "id",
    "video_subject",
    "video_script",
    "status",
    "task_id",
    "video_file",
    "notes",
)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_VALID_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_DONE,
        STATUS_FAILED,
        STATUS_SKIPPED,
    }
)


def queue_file_path() -> Path:
    return Path(utils.storage_dir(create=True)) / QUEUE_FILENAME


def ensure_queue_file() -> Path:
    path = queue_file_path()
    if path.is_file():
        return path
    rows = [
        {
            "id": str(uuid.uuid4()),
            "video_subject": "Are You Burned Out? 7 Warning Signs You Shouldn't Ignore",
            "video_script": "",
            "status": STATUS_PENDING,
            "task_id": "",
            "video_file": "",
            "notes": "Example topic; edit in WebUI or import Excel/CSV",
        }
    ]
    save_rows(rows)
    return path


def _normalize_row(raw: dict[str, Any]) -> dict[str, str]:
    row_id = str(raw.get("id") or "").strip() or str(uuid.uuid4())
    status = str(raw.get("status") or STATUS_PENDING).strip().lower()
    if status not in _VALID_STATUSES:
        status = STATUS_PENDING
    return {
        "id": row_id,
        "video_subject": str(raw.get("video_subject") or "").strip(),
        "video_script": str(raw.get("video_script") or "").strip(),
        "status": status,
        "task_id": str(raw.get("task_id") or "").strip(),
        "video_file": str(raw.get("video_file") or "").strip(),
        "notes": str(raw.get("notes") or "").strip(),
    }


def load_rows() -> list[dict[str, str]]:
    path = ensure_queue_file()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [_normalize_row(row) for row in reader]
    except OSError as exc:
        logger.error(f"failed to read content queue: {exc}")
        return []
    return [row for row in rows if row["video_subject"] or row["video_script"]]


def save_rows(rows: list[dict[str, Any]]) -> None:
    path = queue_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [_normalize_row(row) for row in rows]
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            writer = csv.DictWriter(temp_file, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(normalized)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def find_row(rows: list[dict[str, str]], row_id: str) -> dict[str, str] | None:
    for row in rows:
        if row["id"] == row_id:
            return row
    return None


def update_row(row_id: str, **updates: Any) -> bool:
    rows = load_rows()
    found = False
    for index, row in enumerate(rows):
        if row["id"] != row_id:
            continue
        merged = dict(row)
        merged.update(updates)
        rows[index] = _normalize_row(merged)
        found = True
        break
    if not found:
        return False
    save_rows(rows)
    return True


def next_pending_row() -> dict[str, str] | None:
    for row in load_rows():
        if row["status"] == STATUS_PENDING and (
            row["video_subject"] or row["video_script"]
        ):
            return row
    return None


def get_pending_rows() -> list[dict[str, str]]:
    """Return all rows currently in pending status with a subject or script."""
    return [
        row
        for row in load_rows()
        if row.get("status") == STATUS_PENDING
        and (row.get("video_subject") or row.get("video_script"))
    ]


def count_by_status(status: str) -> int:
    return sum(1 for row in load_rows() if row["status"] == status)


def import_from_dataframe(df: Any, replace: bool = False) -> int:
    """Import rows from a pandas DataFrame, auto-detecting column names."""
    import pandas as pd

    if df is None or df.empty:
        return 0

    subject_col = None
    script_col = None
    notes_col = None

    for col in df.columns:
        norm = str(col).strip().lower().replace("_", "").replace(" ", "")
        if norm in {"topic", "subject", "videosubject", "title", "prompt"}:
            subject_col = col
        elif norm in {"script", "videoscript", "content", "text"}:
            script_col = col
        elif norm in {"notes", "note", "comment", "description"}:
            notes_col = col

    # Fallback: if no subject column detected, use the very first column
    if subject_col is None and len(df.columns) > 0:
        subject_col = df.columns[0]

    new_rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        subject_val = (
            str(row[subject_col] if subject_col and not pd.isna(row[subject_col]) else "").strip()
        )
        script_val = (
            str(row[script_col] if script_col and not pd.isna(row[script_col]) else "").strip()
        )
        notes_val = (
            str(row[notes_col] if notes_col and not pd.isna(row[notes_col]) else "").strip()
        )

        if subject_val or script_val:
            new_rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "video_subject": subject_val,
                    "video_script": script_val,
                    "status": STATUS_PENDING,
                    "task_id": "",
                    "video_file": "",
                    "notes": notes_val,
                }
            )

    if not new_rows:
        return 0

    if replace:
        save_rows(new_rows)
    else:
        existing = load_rows()
        save_rows(existing + new_rows)

    return len(new_rows)


def import_file(file_obj_or_path: Any, replace: bool = False) -> int:
    """Import topics from an Excel (.xlsx, .xls) or CSV file/stream."""
    import pandas as pd

    filename = ""
    if hasattr(file_obj_or_path, "name"):
        filename = str(file_obj_or_path.name).lower()
    elif isinstance(file_obj_or_path, (str, Path)):
        filename = str(file_obj_or_path).lower()

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        df = pd.read_excel(file_obj_or_path)
    else:
        try:
            df = pd.read_csv(file_obj_or_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            if hasattr(file_obj_or_path, "seek"):
                file_obj_or_path.seek(0)
            df = pd.read_csv(file_obj_or_path, encoding="cp1252")

    return import_from_dataframe(df, replace=replace)


def sync_with_task_states() -> list[dict[str, str]]:
    """Synchronize queue rows with live task status from sm.state."""
    from app.models import const
    from app.services import state as sm

    rows = load_rows()
    updated = False
    for row in rows:
        task_id = row.get("task_id")
        if not task_id:
            continue
        try:
            task = sm.state.get_task(task_id)
            if not task:
                continue
            state = task.get("state")
            if state == const.TASK_STATE_COMPLETE and row.get("status") != STATUS_DONE:
                row["status"] = STATUS_DONE
                videos = task.get("videos") or []
                if videos:
                    row["video_file"] = videos[0]
                updated = True
            elif state == const.TASK_STATE_FAILED and row.get("status") != STATUS_FAILED:
                row["status"] = STATUS_FAILED
                err = str(task.get("error") or "").strip()
                if err and not row.get("notes"):
                    row["notes"] = f"Error: {err}"
                updated = True
            elif state == const.TASK_STATE_PROCESSING and row.get("status") == STATUS_PENDING:
                row["status"] = STATUS_RUNNING
                updated = True
        except Exception as exc:
            logger.debug(f"could not sync state for task {task_id}: {exc}")

    if updated:
        save_rows(rows)
    return rows


def clear_completed_rows() -> int:
    """Remove done and failed rows from the queue file."""
    rows = load_rows()
    remaining = [
        r for r in rows if r.get("status") in {STATUS_PENDING, STATUS_RUNNING}
    ]
    save_rows(remaining)
    return len(rows) - len(remaining)


def reset_failed_rows() -> int:
    """Reset failed or running rows back to pending so they can be re-run."""
    rows = load_rows()
    count = 0
    for r in rows:
        if r.get("status") in {STATUS_FAILED, STATUS_RUNNING}:
            r["status"] = STATUS_PENDING
            r["task_id"] = ""
            count += 1
    if count > 0:
        save_rows(rows)
    return count
