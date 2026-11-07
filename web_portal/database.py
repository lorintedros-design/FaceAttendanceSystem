"""Safe read-only access to the existing AttendIQ SQLite database.

This module deliberately never creates tables or student records. The desktop
application remains the owner of schema changes and registration.
"""
from pathlib import Path
import os
import sqlite3
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = Path(os.environ.get("ATTENDIQ_DATABASE_PATH", PROJECT_DIR / "database" / "attendance.db")).resolve()


def _connect_readonly() -> sqlite3.Connection:
    if not DATABASE_PATH.is_file():
        raise FileNotFoundError(f"AttendIQ database not found: {DATABASE_PATH}")
    # URI read-only mode prevents accidental writes and schema creation.
    uri = DATABASE_PATH.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def list_students(limit: int = 200) -> list[dict[str, Any]]:
    """Return basic student directory fields from the desktop app's students table."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    connection = _connect_readonly()
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(students)")}
        if not {"id", "name"}.issubset(columns):
            raise RuntimeError("Existing AttendIQ database has no compatible students table")
        optional = [column for column in ("year", "department", "class_name") if column in columns]
        selected = ["id", "name", *optional]
        sql = "SELECT " + ", ".join('"' + name + '"' for name in selected)
        sql += " FROM students ORDER BY id LIMIT ?"
        return [dict(row) for row in connection.execute(sql, (limit,))]
    finally:
        connection.close()


def get_student(student_id: int) -> dict[str, Any] | None:
    """Look up one existing student by primary key; never register a new one."""
    if student_id <= 0:
        raise ValueError("student_id must be a positive integer")
    connection = _connect_readonly()
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(students)")}
        if not {"id", "name"}.issubset(columns):
            raise RuntimeError("Existing AttendIQ database has no compatible students table")
        optional = [column for column in ("year", "department", "class_name") if column in columns]
        selected = ["id", "name", *optional]
        sql = "SELECT " + ", ".join('"' + name + '"' for name in selected)
        sql += " FROM students WHERE id = ?"
        row = connection.execute(sql, (student_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()
