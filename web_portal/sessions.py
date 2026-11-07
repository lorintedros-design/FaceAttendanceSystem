"""Persistence for temporary web attendance sessions.

Student registration remains owned by the desktop application. This module adds
only session rows to the shared AttendIQ database.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import sqlite3

from web_portal.database import DATABASE_PATH

CAMPUS_LATITUDE = 13.0246079
CAMPUS_LONGITUDE = 80.0291215
CAMPUS_RADIUS_METERS = 250

# Temporary test-only location. Test sessions never write attendance.
TEST_GEOFENCE_ENABLED = True
TEST_LATITUDE = 13.0259125
TEST_LONGITUDE = 80.022453125
TEST_RADIUS_METERS = 250


def _connect():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _ensure_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS web_attendance_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            teacher_user_id INTEGER NOT NULL,
            teacher_username TEXT NOT NULL,
            started_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            campus_latitude REAL NOT NULL,
            campus_longitude REAL NOT NULL,
            campus_radius_meters INTEGER NOT NULL,
            qr_token_hash TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
            academic_year TEXT NOT NULL DEFAULT '',
            department TEXT NOT NULL DEFAULT '',
            class_name TEXT NOT NULL DEFAULT ''
        )
    """)
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(web_attendance_sessions)"
    )}
    if "qr_token_hash" not in columns:
        connection.execute(
            "ALTER TABLE web_attendance_sessions ADD COLUMN qr_token_hash TEXT"
        )
    if "is_test" not in columns:
        connection.execute(
            "ALTER TABLE web_attendance_sessions ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1))"
        )
    for column in ("academic_year", "department", "class_name"):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE web_attendance_sessions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_sessions_active_teacher "
        "ON web_attendance_sessions(active, teacher_user_id, expires_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_sessions_qr_hash "
        "ON web_attendance_sessions(qr_token_hash)"
    )


def create_session(
    subject: str, duration_minutes: int, teacher: dict, test_mode: bool = False,
    academic_year: str = "", department: str = "", class_name: str = "",
) -> dict:
    subject = (subject or "").strip()
    if not subject or len(subject) > 100:
        raise ValueError("Subject must contain 1 to 100 characters.")
    if not 1 <= duration_minutes <= 60:
        raise ValueError("Session duration must be between 1 and 60 minutes.")
    academic_year = (academic_year or "").strip()[:50]
    department = (department or "").strip()[:50]
    class_name = (class_name or "").strip()[:50]
    if test_mode and not TEST_GEOFENCE_ENABLED:
        raise ValueError("The temporary test location is disabled.")
    campus = (
        {"latitude": TEST_LATITUDE, "longitude": TEST_LONGITUDE, "radius_meters": TEST_RADIUS_METERS}
        if test_mode else
        {"latitude": CAMPUS_LATITUDE, "longitude": CAMPUS_LONGITUDE, "radius_meters": CAMPUS_RADIUS_METERS}
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(minutes=duration_minutes)
    started_at = now.isoformat()
    expires_at = expires.isoformat()
    qr_token = secrets.token_urlsafe(32)
    qr_token_hash = hashlib.sha256(qr_token.encode("utf-8")).hexdigest()
    connection = _connect()
    try:
        with connection:
            _ensure_table(connection)
            connection.execute(
                "UPDATE web_attendance_sessions SET active = 0 "
                "WHERE active = 1 AND expires_at <= ?",
                (started_at,),
            )
            cursor = connection.execute("""
                INSERT INTO web_attendance_sessions (
                    subject, teacher_user_id, teacher_username, started_at,
                    expires_at, campus_latitude, campus_longitude,
                    campus_radius_meters, qr_token_hash, active, is_test,
                    academic_year, department, class_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                subject, teacher["id"], teacher["username"], started_at,
                expires_at, campus["latitude"], campus["longitude"],
                campus["radius_meters"], qr_token_hash, int(test_mode),
                academic_year, department, class_name,
            ))
            session_id = cursor.lastrowid
        return {
            "id": session_id,
            "subject": subject,
            "teacher_username": teacher["username"],
            "started_at": started_at,
            "expires_at": expires_at,
            "campus": campus,
            "test_mode": test_mode,
            "academic_year": academic_year,
            "department": department,
            "class_name": class_name,
            "qr_token": qr_token,
            "active": True,
        }
    finally:
        connection.close()


def resolve_qr_token(token: str) -> dict | None:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    connection = _connect()
    try:
        with connection:
            _ensure_table(connection)
            row = connection.execute("""
                SELECT id, subject, started_at, expires_at, campus_latitude,
                       campus_longitude, campus_radius_meters, is_test,
                       academic_year, department, class_name
                FROM web_attendance_sessions
                WHERE qr_token_hash = ? AND active = 1 AND expires_at > ?
            """, (token_hash, now)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "subject": row["subject"],
            "started_at": row["started_at"],
            "expires_at": row["expires_at"],
            "campus": {
                "latitude": row["campus_latitude"],
                "longitude": row["campus_longitude"],
                "radius_meters": row["campus_radius_meters"],
            },
            "test_mode": bool(row["is_test"]),
            "academic_year": row["academic_year"],
            "department": row["department"],
            "class_name": row["class_name"],
            "active": True,
        }
    finally:
        connection.close()


def check_location(token: str, latitude: float, longitude: float, accuracy_meters: float) -> dict | None:
    """Check a QR holder's reported position without storing it or marking attendance."""
    import math

    values = (latitude, longitude, accuracy_meters)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Location values must be finite numbers.")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Latitude or longitude is outside its valid range.")
    if not 0 <= accuracy_meters <= 10000:
        raise ValueError("Location accuracy is outside its valid range.")

    session = resolve_qr_token(token)
    if session is None:
        return None

    earth_radius = 6_371_000
    lat1 = math.radians(session["campus"]["latitude"])
    lat2 = math.radians(latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(longitude - session["campus"]["longitude"])
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    distance = 2 * earth_radius * math.asin(min(1, math.sqrt(haversine)))
    accurate = accuracy_meters <= 100
    within = distance <= session["campus"]["radius_meters"]
    return {
        "within_geofence": within and accurate,
        "distance_meters": round(distance),
        "accuracy_meters": round(accuracy_meters),
        "maximum_accuracy_meters": 100,
        "radius_meters": session["campus"]["radius_meters"],
        "reason": (
            "inside" if within and accurate else
            "location_accuracy_low" if not accurate else "outside"
        ),
    }
def list_active_sessions(teacher: dict) -> list[dict]:
    connection = _connect()
    try:
        with connection:
            _ensure_table(connection)
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            connection.execute(
                "UPDATE web_attendance_sessions SET active = 0 "
                "WHERE active = 1 AND expires_at <= ?", (now,)
            )
            if teacher["role"] == "Admin":
                rows = connection.execute("""
                    SELECT id, subject, teacher_username, started_at, expires_at,
                           campus_latitude, campus_longitude, campus_radius_meters, is_test,
                           academic_year, department, class_name
                    FROM web_attendance_sessions WHERE active = 1 ORDER BY id DESC
                """).fetchall()
            else:
                rows = connection.execute("""
                    SELECT id, subject, teacher_username, started_at, expires_at,
                           campus_latitude, campus_longitude, campus_radius_meters, is_test,
                           academic_year, department, class_name
                    FROM web_attendance_sessions
                    WHERE active = 1 AND teacher_user_id = ? ORDER BY id DESC
                """, (teacher["id"],)).fetchall()
        return [
            {
                "id": row["id"], "subject": row["subject"],
                "teacher_username": row["teacher_username"],
                "started_at": row["started_at"], "expires_at": row["expires_at"],
                "campus": {
                    "latitude": row["campus_latitude"],
                    "longitude": row["campus_longitude"],
                    "radius_meters": row["campus_radius_meters"],
                },
                "test_mode": bool(row["is_test"]),
                "academic_year": row["academic_year"],
                "department": row["department"],
                "class_name": row["class_name"],
                "active": True,
            }
            for row in rows
        ]
    finally:
        connection.close()


def close_session(session_id: int, teacher: dict) -> bool:
    connection = _connect()
    try:
        with connection:
            _ensure_table(connection)
            if teacher["role"] == "Admin":
                cursor = connection.execute(
                    "UPDATE web_attendance_sessions SET active = 0 "
                    "WHERE id = ? AND active = 1", (session_id,)
                )
            else:
                cursor = connection.execute(
                    "UPDATE web_attendance_sessions SET active = 0 "
                    "WHERE id = ? AND teacher_user_id = ? AND active = 1",
                    (session_id, teacher["id"]),
                )
            return cursor.rowcount == 1
    finally:
        connection.close()

