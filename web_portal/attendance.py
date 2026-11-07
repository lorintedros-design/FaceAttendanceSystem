"""Write verified web attendance into AttendIQ's existing daily schema."""
from datetime import datetime
import hashlib
import json
import sqlite3

from security.blockchain import AttendanceBlockchain
from security.encryption import encrypt_data
from web_portal.database import DATABASE_PATH


def mark_web_attendance(student_id: int, web_session_id: int, distance_meters: int,
                        accuracy_meters: int) -> dict:
    """Insert one daily Present row without replacing desktop attendance."""
    if student_id <= 0 or web_session_id <= 0:
        raise ValueError("Student and web session IDs must be positive.")

    now = datetime.now().astimezone()
    day = now.date().isoformat()
    stamp = now.strftime("%H:%M:%S")
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    attendance_id = None
    security_warning = None
    payload = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(attendance)")}
        if not {"id", "student_id", "date", "time", "status", "session_id"}.issubset(columns):
            raise RuntimeError("AttendIQ attendance table is not initialized by the desktop app.")
        if connection.execute("SELECT 1 FROM students WHERE id = ?", (student_id,)).fetchone() is None:
            connection.rollback()
            return {"status": "student_not_found"}

        existing = connection.execute(
            "SELECT id, status FROM attendance "
            "WHERE student_id = ? AND date = ? AND session_id IS NULL",
            (student_id, day),
        ).fetchone()
        if existing:
            connection.rollback()
            return {"status": "already_recorded", "existing_status": existing[1]}

        cursor = connection.execute(
            "INSERT INTO attendance (student_id, date, time, status, session_id) "
            "VALUES (?, ?, ?, 'Present', NULL)",
            (student_id, day, stamp),
        )
        attendance_id = cursor.lastrowid

        payload = {
            "attendance_id": int(attendance_id),
            "student_id": int(student_id),
            "date": day,
            "time": stamp,
            "status": "Present",
        }
        try:
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            encrypted = encrypt_data(serialized)
            record_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            connection.execute("""
                CREATE TABLE IF NOT EXISTS attendance_security (
                    attendance_id INTEGER PRIMARY KEY,
                    encrypted_payload TEXT NOT NULL,
                    record_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(attendance_id) REFERENCES attendance(id) ON DELETE CASCADE
                )
            """)
            connection.execute(
                "INSERT INTO attendance_security "
                "(attendance_id, encrypted_payload, record_hash, created_at) VALUES (?, ?, ?, ?)",
                (attendance_id, encrypted, record_hash, now.isoformat(timespec="seconds")),
            )
        except Exception:
            security_warning = "The attendance row was saved, but its encrypted audit snapshot failed."
            payload = None

        connection.execute("""
            CREATE TABLE IF NOT EXISTS web_attendance_links (
                attendance_id INTEGER PRIMARY KEY,
                web_session_id INTEGER NOT NULL,
                location_distance_meters INTEGER NOT NULL,
                location_accuracy_meters INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attendance_id) REFERENCES attendance(id) ON DELETE CASCADE,
                FOREIGN KEY(web_session_id) REFERENCES web_attendance_sessions(id) ON DELETE CASCADE
            )
        """)
        connection.execute(
            "INSERT INTO web_attendance_links "
            "(attendance_id, web_session_id, location_distance_meters, "
            "location_accuracy_meters, created_at) VALUES (?, ?, ?, ?, ?)",
            (attendance_id, web_session_id, max(0, int(distance_meters)),
             max(0, int(accuracy_meters)), now.isoformat(timespec="seconds")),
        )
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "activity_log" in tables:
            connection.execute(
                "INSERT INTO activity_log(event_time, actor, action, details) VALUES (?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), f"Student {student_id}",
                 "WEB_QR_ATTENDANCE", f"Session {web_session_id}"),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    if payload is not None:
        try:
            AttendanceBlockchain().add_attendance(payload)
        except Exception:
            security_warning = "Attendance was saved, but the blockchain audit append failed."

    result = {"status": "recorded", "attendance_id": attendance_id}
    if security_warning:
        result["security_warning"] = security_warning
    return result
