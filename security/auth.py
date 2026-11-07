"""Local user accounts, password hashing and role permissions for AttendIQ.

Pure logic with no GUI code, so it can be tested on its own.
Passwords are never stored: only a salted PBKDF2-HMAC-SHA256 hash is.
"""
import hashlib
import hmac
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "attendance.db"

ROLE_ADMIN = "Admin"
ROLE_TEACHER = "Teacher"
ROLES = (ROLE_ADMIN, ROLE_TEACHER)

PBKDF2_ITERATIONS = 240_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

# Actions only an Admin may perform. Teachers can do everything else.
ADMIN_ONLY_ACTIONS = {
    "delete_student",
    "manage_users",
    "view_security",
    "view_log",
    "send_email",
}
# Sidebar pages that are protected, and the action each one needs.
PAGE_ACTION = {
    "Manage Users": "manage_users",
    "Security Center": "view_security",
    "Activity Log": "view_log",
    "Email Alerts": "send_email",
}


def can(role, action):
    if role == ROLE_ADMIN:
        return True
    return role == ROLE_TEACHER and action not in ADMIN_ONLY_ACTIONS


def can_open(role, page):
    action = PAGE_ACTION.get(page)
    return action is None or can(role, action)


# ---------------- database ----------------

@contextmanager
def connect(path=None):
    """Commit on success, roll back on error, and always close."""
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_users_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Admin', 'Teacher')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)


# ---------------- passwords ----------------

def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
            int(iterations), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


def password_problem(password, username=""):
    """Return a message describing what is wrong, or None if the password is fine."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if username and password.lower() == username.lower():
        return "Password must not be the same as the username."
    return None


_dummy_hash = None


def _dummy():
    # Verified when the username does not exist, so a wrong username and a
    # wrong password take about the same time.
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not-a-real-password")
    return _dummy_hash


# ---------------- users ----------------

def user_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def active_admin_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = ? AND active = 1",
        (ROLE_ADMIN,)).fetchone()[0]


def _get(conn, user_id):
    row = conn.execute(
        "SELECT id, username, role, active FROM users WHERE id = ?",
        (user_id,)).fetchone()
    if row is None:
        raise ValueError("That user no longer exists.")
    return row


def _guard_last_admin(conn, user_id):
    _id, _name, role, active = _get(conn, user_id)
    if role == ROLE_ADMIN and active and active_admin_count(conn) <= 1:
        raise ValueError("At least one active Admin account must remain.")


def create_user(conn, username, password, role):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3-32 characters: letters, numbers, dot, dash or underscore.")
    if role not in ROLES:
        raise ValueError("Choose a valid role.")
    problem = password_problem(password, username)
    if problem:
        raise ValueError(problem)
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role,
             datetime.now().isoformat(timespec="seconds")))
    except sqlite3.IntegrityError:
        raise ValueError("That username is already taken.") from None
    return cursor.lastrowid


def authenticate(conn, username, password, record_login=True):
    """Return {'id', 'username', 'role'} for a valid, active account, else None."""
    row = conn.execute(
        "SELECT id, username, password_hash, role, active FROM users WHERE username = ?",
        ((username or "").strip(),)).fetchone()
    password_ok = verify_password(password, row[2] if row else _dummy())
    if row is None or not password_ok or not row[4]:
        return None
    if record_login:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                     (datetime.now().isoformat(timespec="seconds"), row[0]))
    return {"id": row[0], "username": row[1], "role": row[3]}


def list_users(conn):
    rows = conn.execute(
        "SELECT id, username, role, active, last_login FROM users ORDER BY id").fetchall()
    return [{"id": r[0], "username": r[1], "role": r[2],
             "active": bool(r[3]), "last_login": r[4]} for r in rows]


def set_password(conn, user_id, new_password):
    _id, username, _role, _active = _get(conn, user_id)
    problem = password_problem(new_password, username)
    if problem:
        raise ValueError(problem)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(new_password), user_id))


def set_active(conn, user_id, active):
    if not active:
        _guard_last_admin(conn, user_id)
    else:
        _get(conn, user_id)
    conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))


def set_role(conn, user_id, role):
    if role not in ROLES:
        raise ValueError("Choose a valid role.")
    if role != ROLE_ADMIN:
        _guard_last_admin(conn, user_id)
    else:
        _get(conn, user_id)
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def delete_user(conn, user_id):
    _guard_last_admin(conn, user_id)
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------------- brute-force protection ----------------

class LoginThrottle:
    """Locks a username for a short time after repeated failures (in memory only)."""

    def __init__(self, max_failures=MAX_FAILED_ATTEMPTS, lockout=LOCKOUT_SECONDS):
        self.max_failures = max_failures
        self.lockout = lockout
        self._state = {}  # username -> [failure_count, locked_until]

    def locked_for(self, username):
        _count, until = self._state.get(username.lower(), (0, 0.0))
        return max(0, math.ceil(until - time.monotonic()))

    def record_failure(self, username):
        key = username.lower()
        count, until = self._state.get(key, (0, 0.0))
        count += 1
        if count >= self.max_failures:
            count, until = 0, time.monotonic() + self.lockout
        self._state[key] = [count, until]

    def reset(self, username):
        self._state.pop(username.lower(), None)