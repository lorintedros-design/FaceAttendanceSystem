import csv
import json
import hashlib
import smtplib
import ssl
import os
import pickle
import re
import sqlite3
import threading
import queue
import time
import zipfile
import shutil
import socket
import subprocess
import webbrowser
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import cv2
import numpy as np

try:
    import face_recognition
except ImportError:
    face_recognition = None

try:
    from PIL import Image
except ImportError:
    Image = None

# =========================================================
# ATTENDIQ - FACE ATTENDANCE SYSTEM (optimized)
# =========================================================

BASE_DIR = Path(__file__).resolve().parent.parent
# Ensure project-root imports work when launched as: python gui/app.py
import sys
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from security.encryption import encrypt_data, decrypt_data
from security.blockchain import AttendanceBlockchain
from gui.auth_ui import AuthMixin
from gui.leave_ui import LeaveMixin
from gui.report_email_ui import ReportEmailMixin

DB_PATH = BASE_DIR / "database" / "attendance.db"
CACHE_PATH = BASE_DIR / "database" / "face_cache.pkl"
IMAGE_DIR = BASE_DIR / "images"
IMAGE_EXT = {".jpg", ".jpeg", ".png"}

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

NAVY = "#071225"
BLUE = "#00C8F0"
BG = "#07101F"
WHITE = "#101D32"
TEXT = "#EAF4FF"
MUTED = "#91A9C8"
BORDER = "#203754"
GREEN = "#00D6A0"
RED = "#FF5B7F"
AMBER = "#FFC15A"

YEARS = ["All Years", "1st Year", "2nd Year", "3rd Year", "4th Year"]
DEPARTMENTS = ["All Departments", "CSE", "IT", "ECE", "EEE", "MECH", "CIVIL",
               "AI & DS", "CYBER SECURITY", "Other"]

MATCH_THRESHOLD = 0.55
MATCH_MARGIN = 0.025
STREAK_REQUIRED = 3
RECOGNITION_SCALE = 0.75
PREVIEW_SIZE = (850, 440)
MAX_ROWS_SHOWN = 120  # widgets are expensive; exports are never limited

# An Absent record is "excused" when the student has approved leave covering that date.
# Excused absences are left out of attendance percentages. Use with the attendance table aliased as `a`.
EXCUSED_SQL = ("(a.status = 'Absent' AND EXISTS (SELECT 1 FROM leaves lv "
               "WHERE lv.student_id = a.student_id AND a.date BETWEEN lv.start_date AND lv.end_date))")

HISTORY_SQL = f"""
    SELECT a.date, s.id, s.name, CASE WHEN {EXCUSED_SQL} THEN 'Excused' ELSE a.status END, a.time, COALESCE(t.subject, 'Daily')
    FROM attendance a
    LEFT JOIN students s ON s.id = a.student_id
    LEFT JOIN timetable t ON t.id = a.session_id
    ORDER BY a.date DESC, a.time DESC, s.id
"""


# ---------------- DATABASE ----------------

@contextmanager
def db():
    """Open a connection, commit on success, and ALWAYS close it
    (a plain `with sqlite3.connect()` never closes the connection)."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, far fewer disk syncs
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8000")     # ~8 MB page cache
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_database():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                face_encoding TEXT
            )
        """)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
        if "year" not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN year TEXT DEFAULT ''")
        if "department" not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN department TEXT DEFAULT ''")
        if "class_name" not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN class_name TEXT DEFAULT ''")
        if "email" not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN email TEXT DEFAULT ''")
        if "parent_email" not in columns:
            conn.execute("ALTER TABLE students ADD COLUMN parent_email TEXT DEFAULT ''")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        # One record per student per day, enforced by the database itself.
        conn.execute("""
            DELETE FROM attendance WHERE id NOT IN (
                SELECT MAX(id) FROM attendance GROUP BY student_id, date
            )
        """)
        attendance_columns = {r[1] for r in conn.execute("PRAGMA table_info(attendance)")}
        if "session_id" not in attendance_columns:
            conn.execute("ALTER TABLE attendance ADD COLUMN session_id INTEGER")
        # Preserve legacy one-record-per-day rows while allowing one record per timetable session.
        conn.execute("DROP INDEX IF EXISTS idx_att_student_date")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_att_legacy_day "
                     "ON attendance(student_id, date) WHERE session_id IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_att_session "
                     "ON attendance(student_id, date, session_id) WHERE session_id IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_att_student_status "
                     "ON attendance(student_id, date, status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS timetable (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
            period TEXT NOT NULL, weekday TEXT NOT NULL,
            start_time TEXT DEFAULT '', end_time TEXT DEFAULT '', active INTEGER DEFAULT 1
        )""")
        timetable_columns = {r[1] for r in conn.execute("PRAGMA table_info(timetable)")}
        for column in ("academic_year", "department", "class_name"):
            if column not in timetable_columns:
                conn.execute(f"ALTER TABLE timetable ADD COLUMN {column} TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timetable_group ON timetable(academic_year, department, class_name, weekday)")
        conn.execute("""CREATE TABLE IF NOT EXISTS performance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
            subject TEXT NOT NULL, assessment TEXT NOT NULL, marks REAL NOT NULL,
            max_marks REAL NOT NULL, recorded_on TEXT NOT NULL,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS student_fee_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            academic_year TEXT NOT NULL,
            fee_type TEXT NOT NULL,
            total_amount REAL NOT NULL CHECK(total_amount > 0),
            due_date TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_student_fee_student "
                     "ON student_fee_accounts(student_id, academic_year)")
        conn.execute("""CREATE TABLE IF NOT EXISTS student_fee_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fee_account_id INTEGER NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            paid_on TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'Other',
            reference TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            recorded_by TEXT DEFAULT '',
            recorded_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_student_fee_payment_account "
                     "ON student_fee_payments(fee_account_id, paid_on)")
        conn.execute("""CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL,
            actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS leaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            leave_type TEXT NOT NULL, reason TEXT DEFAULT '',
            created_by TEXT DEFAULT '', created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leaves_student "
                     "ON leaves(student_id, start_date, end_date)")
        # Encrypted snapshots for newly saved attendance. Existing attendance rows remain unchanged.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance_security (
                attendance_id INTEGER PRIMARY KEY,
                encrypted_payload TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attendance_id) REFERENCES attendance(id) ON DELETE CASCADE
            )
        """)
        # Weekly email automation settings. SMTP credentials are encrypted before storage.
        conn.execute("""CREATE TABLE IF NOT EXISTS email_automation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0,
            weekday INTEGER NOT NULL DEFAULT 4,
            send_time TEXT NOT NULL DEFAULT '16:00',
            encrypted_config BLOB,
            last_sent_date TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
        )""")
        conn.execute("INSERT OR IGNORE INTO app_settings(setting_key,setting_value) VALUES('attendance_threshold','75')")
        conn.execute("INSERT OR IGNORE INTO app_settings(setting_key,setting_value) VALUES('student_email_subject','AttendIQ: Low Attendance Alert')")
        conn.execute("INSERT OR IGNORE INTO app_settings(setting_key,setting_value) VALUES('student_email_template','Hello {name},\n\nYour current attendance is {percentage}%, which is below the required {threshold}% threshold. Please contact your faculty advisor.\n\nAttendIQ')")
        conn.execute("INSERT OR IGNORE INTO app_settings(setting_key,setting_value) VALUES('parent_email_subject','AttendIQ: Low Attendance Alert for {name}')")
        conn.execute("INSERT OR IGNORE INTO app_settings(setting_key,setting_value) VALUES('parent_email_template','Dear Parent/Guardian,\n\nThe current attendance of {name} is {percentage}%, which is below the required {threshold}% threshold. Please speak with your child and contact the faculty advisor if you have any questions.\n\nAttendIQ')")


def get_student_names():
    with db() as conn:
        return dict(conn.execute("SELECT id, name FROM students").fetchall())


def get_attendance(day, session_id=None):
    with db() as conn:
        if session_id is None:
            return conn.execute("""
                SELECT s.id, s.name, a.status, a.time
                FROM students s
                LEFT JOIN attendance a ON s.id = a.student_id AND a.date = ? AND a.session_id IS NULL
                ORDER BY s.id
            """, (day,)).fetchall()
        return conn.execute("""
            SELECT s.id, s.name, a.status, a.time
            FROM students s
            JOIN timetable t ON t.id = ?
            LEFT JOIN attendance a ON s.id = a.student_id AND a.date = ? AND a.session_id = ?
            WHERE (COALESCE(t.academic_year,'') = '' OR s.year = t.academic_year)
              AND (COALESCE(t.department,'') = '' OR UPPER(s.department) = UPPER(t.department))
              AND (COALESCE(t.class_name,'') = '' OR UPPER(COALESCE(s.class_name,'')) = UPPER(t.class_name))
            ORDER BY s.id
        """, (session_id, day, session_id)).fetchall()


def attendance_summary(day):
    with db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM attendance WHERE date = ? GROUP BY status",
            (day,)
        ).fetchall()
    result = {"Present": 0, "Absent": 0}
    for status, count in rows:
        if status in result:
            result[status] = count
    return result


_photo_cache = {"stamp": None, "ids": set()}


def photo_student_ids():
    """Student IDs that have at least one photo. The folder is only re-scanned
    when its modification time changes, not on every filter/sort click."""
    try:
        stamp = IMAGE_DIR.stat().st_mtime_ns
    except OSError:
        stamp = None
    if stamp is not None and stamp == _photo_cache["stamp"]:
        return _photo_cache["ids"]
    ids = set()
    for p in IMAGE_DIR.iterdir():
        if p.suffix.lower() in IMAGE_EXT:
            head = p.name.split("_", 1)[0]
            if head.isdigit():
                ids.add(int(head))
    _photo_cache["stamp"], _photo_cache["ids"] = stamp, ids
    return ids


def open_camera(width=None, height=None):
    camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not camera.isOpened():
        camera.release()
        camera = cv2.VideoCapture(0)
    if not camera.isOpened():
        camera.release()
        return None
    if width and height:
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return camera


# ---------------- FAST SCROLLING ----------------
# CTkScrollableFrame re-binds the mouse wheel on every child widget and redraws
# a lot on each scroll step, which is what makes long tables stutter.
# FastScrollFrame is a plain tk.Canvas + one scrollbar, and ONE global wheel
# handler finds the scroll area under the pointer.

def _resolve_bg(master, fg_color=None):
    def pick(c):
        if isinstance(c, (tuple, list)):
            return c[1] if ctk.get_appearance_mode() == "Dark" else c[0]
        return c
    c = pick(fg_color) if fg_color else None
    if isinstance(c, str) and c.startswith("#"):
        return c
    w = master
    while w is not None:
        c = pick(getattr(w, "_fg_color", None))
        if isinstance(c, str) and c not in ("transparent", ""):
            return c
        if not hasattr(w, "_fg_color") and isinstance(w, tk.Frame):
            try:
                return w.cget("bg")
            except Exception:
                pass
        w = getattr(w, "master", None)
    return BG


class FastScrollFrame(tk.Frame):
    """Drop-in replacement for ctk.CTkScrollableFrame.
    The object you get back is the inner frame (put children in it);
    grid()/pack()/place() are forwarded to the outer container."""

    STEP_PX = 24  # pixels per scroll "unit"

    def __init__(self, master, fg_color=None, corner_radius=0,
                 scrollbar_button_color="#315071",
                 scrollbar_button_hover_color=BLUE, **_ignored):
        bg = _resolve_bg(master, fg_color)
        outer = tk.Frame(master, bg=bg, bd=0, highlightthickness=0)
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, bg=bg, bd=0, highlightthickness=0,
                           yscrollincrement=self.STEP_PX)
        bar = ctk.CTkScrollbar(outer, orientation="vertical", width=12,
                               command=canvas.yview,
                               button_color=scrollbar_button_color,
                               button_hover_color=scrollbar_button_hover_color)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        super().__init__(canvas, bg=bg, bd=0, highlightthickness=0)
        self._outer, self._canvas, self._bar = outer, canvas, bar
        self._win = canvas.create_window(0, 0, window=self, anchor="nw")
        self._update_pending = False
        outer._fast_canvas = canvas
        canvas._fast_canvas = canvas
        self._fast_canvas = canvas
        canvas.bind("<Configure>", self._on_canvas_configure)
        tk.Frame.bind(self, "<Configure>", self._schedule_update, add="+")

    # geometry-manager calls go to the outer container
    def grid(self, *a, **k):        return self._outer.grid(*a, **k)
    def grid_configure(self, *a, **k): return self._outer.grid(*a, **k)
    def grid_remove(self):          return self._outer.grid_remove()
    def grid_forget(self):          return self._outer.grid_forget()
    def pack(self, *a, **k):        return self._outer.pack(*a, **k)
    def pack_forget(self):          return self._outer.pack_forget()
    def place(self, *a, **k):       return self._outer.place(*a, **k)
    def place_forget(self):         return self._outer.place_forget()

    def configure(self, **kw):
        color = kw.pop("fg_color", None)
        if color is not None and color != "transparent":
            kw["bg"] = color if isinstance(color, str) else color[-1]
        kw = {k: v for k, v in kw.items() if k in ("bg", "background", "width", "height")}
        if "bg" in kw:
            self._canvas.configure(bg=kw["bg"])
            self._outer.configure(bg=kw["bg"])
        if kw:
            tk.Frame.configure(self, **kw)
    config = configure

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._win, width=event.width)
        self._schedule_update()

    def _schedule_update(self, _event=None):
        if self._update_pending:
            return
        self._update_pending = True
        self._canvas.after_idle(self._update_region)

    def _update_region(self):
        self._update_pending = False
        try:
            view_h = self._canvas.winfo_height()
            height = max(self.winfo_reqheight(), view_h)
            self._canvas.configure(scrollregion=(0, 0, self._canvas.winfo_width(), height))
        except tk.TclError:
            pass


def install_fast_wheel(root):
    """One global mouse-wheel handler for every FastScrollFrame."""
    def on_wheel(event):
        try:
            w = root.winfo_containing(event.x_root, event.y_root)
        except Exception:      # tkinter can raise KeyError for popup menus
            return
        while w is not None:
            if isinstance(w, tk.Text):      # textboxes scroll themselves
                return
            canvas = getattr(w, "_fast_canvas", None)
            if canvas is not None:
                try:
                    if canvas.yview() != (0.0, 1.0):
                        notches = max(1, abs(event.delta) // 120)
                        canvas.yview_scroll((-1 if event.delta > 0 else 1) * notches * 3, "units")
                except tk.TclError:
                    pass
                return
            w = getattr(w, "master", None)

    def on_linux_up(event):   event.delta = 120;  on_wheel(event)
    def on_linux_down(event): event.delta = -120; on_wheel(event)

    root.bind_all("<MouseWheel>", on_wheel)
    root.bind_all("<Button-4>", on_linux_up)
    root.bind_all("<Button-5>", on_linux_down)


# Any other module (auth_ui, leave_ui, ...) that calls ctk.CTkScrollableFrame
# automatically gets the fast version too.
ctk.CTkScrollableFrame = FastScrollFrame


# ---------------- APPLICATION ----------------
class AttendIQ(AuthMixin, LeaveMixin, ReportEmailMixin, ctk.CTk):
    def __init__(self):
        super().__init__()
        initialize_database()
        self._email_results = queue.Queue()
        self._email_job_running = False
        self._email_last_attempt = ""
        self._email_scheduler_after = None
        self._teacher_portal_process = None
        self._teacher_portal_port = None
        self._teacher_portal_ready = False
        self._teacher_starting_dialog = None
        self._teacher_start_status = None
        self._teacher_tunnel_process = None
        self._teacher_portal_url = None
        self._teacher_local_url = None
        self._teacher_local_opened = False
        self._teacher_tunnel_events = queue.Queue()
        self._teacher_tunnel_poll_id = None
        self._teacher_tunnel_candidate_url = None
        self._teacher_tunnel_connected = False
        self._teacher_tunnel_dns_check_started = False
        self._teacher_tunnel_dns_ready = False
        self._teacher_tunnel_opened = False

        self.title("AttendIQ | Smart Attendance Management")
        self.geometry("1280x800")
        self.minsize(1050, 650)
        self.configure(fg_color=BG)

        self.nav_buttons = {}
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # recognition state
        self.recognition_camera = None
        self.active_session_id = None
        self.recognition_running = False
        self.recognition_loading = False
        self.recognition_after_id = None
        self.recognition_busy = False
        self.recognition_generation = 0
        self.known_encodings = np.empty((0, 128))
        self.known_unique_ids = []
        self.known_starts = np.array([], dtype=int)
        self.student_names = {}
        self.recognition_seen_in_session = set()
        self.recognition_streaks = {}
        self.liveness_state = {}
        self.recognition_frame_counter = 0
        self.recognition_last_results = []
        self.recognition_frame_label = None
        self.recognition_status_label = None
        self.recognition_count_label = None
        self._last_status = None
        self._last_count = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        install_fast_wheel(self)
        self.create_sidebar()

        # Pages are built in a fresh hidden frame and revealed in one step;
        # the old frame is destroyed afterwards so switching feels instant.
        self.content = None
        self._trash = []
        self._reveal_id = None

        self.init_auth()
        self.show_login_screen()
        # Check the weekly schedule in the background while AttendIQ is running.
        self._email_scheduler_after = self.after(5000, self._check_weekly_email_schedule)
        self.after(1000, self._poll_email_automation_results)

    # ---------------- SIDEBAR ----------------

    def create_sidebar(self):
        side = ctk.CTkFrame(self, width=225, fg_color=NAVY, corner_radius=0)
        self.sidebar = side
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(1, weight=1)  # the menu area takes all spare height

        # --- fixed, compact header ---
        head = ctk.CTkFrame(side, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        ctk.CTkLabel(
            head, text="A", width=38, height=38, corner_radius=10,
            fg_color=BLUE, text_color="#04111F", font=("Segoe UI", 20, "bold")
        ).grid(row=0, column=0, rowspan=2, padx=(0, 10))
        ctk.CTkLabel(
            head, text="AttendIQ", font=("Segoe UI", 20, "bold"), text_color=TEXT
        ).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            head, text="ATTENDANCE MANAGEMENT",
            font=("Segoe UI", 8, "bold"), text_color="#A9B8CC"
        ).grid(row=1, column=1, sticky="w")

        # --- scrollable menu: every page stays reachable on small screens ---
        menu = FastScrollFrame(
            side, fg_color="transparent", corner_radius=0,
            scrollbar_button_color="#315071", scrollbar_button_hover_color=BLUE)
        menu.grid(row=1, column=0, sticky="nsew", padx=(4, 0), pady=(6, 0))
        menu.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            menu, text="WORKSPACE", font=("Segoe UI", 10, "bold"), text_color="#93A2B8"
        ).pack(anchor="w", padx=10, pady=(0, 4))

        pages = [
            ("Dashboard", self.show_dashboard),
            ("TeacherIQ", self.open_teacher_portal),
            ("Students", self.show_students),
            ("Fee Management", self.show_fee_management),
            ("Attendance", self.show_attendance),
            ("Leave Management", self.show_leave_management),
            ("Day-wise Overview", self.show_daywise_overview),
            ("Date Range Analysis", self.show_date_range_analysis),
            ("Reports", self.show_reports),
            ("Face Recognition", self.show_camera_page),
            ("Security Center", self.show_security_center),
            ("Timetable", self.show_timetable),
            ("Student Analytics", self.show_student_analytics),
            ("Send Report Emails", self.show_report_email_page),
            ("Email Alerts", self.show_email_alerts),
            ("Settings & Backup", self.show_settings_backup),
            ("Activity Log", self.show_activity_log),
            ("Manage Users", self.show_manage_users),
        ]
        for name, command in pages:
            button = ctk.CTkButton(
                menu, text=name, anchor="w", height=34, corner_radius=8,
                fg_color="transparent", hover_color="#2A3A52",
                text_color="#E2E9F2", font=("Segoe UI", 13),
                command=lambda n=name, c=command: self.navigate_to(n, c)
            )
            button.pack(fill="x", padx=4, pady=2)
            self.nav_buttons[name] = button

        # --- account box (signed-in user, password, sign out) ---
        self.build_account_box(side)

        # --- fixed footer ---
        ctk.CTkLabel(
            side, text="Local database \u2022 data stays on this computer",
            font=("Segoe UI", 9), text_color="#A9B8CC", justify="left"
        ).grid(row=3, column=0, sticky="w", padx=22, pady=(6, 12))

    def open_teacher_portal(self):
        """Start a fresh public tunnel and open the TeacherIQ website when ready."""
        tunnel = self._teacher_tunnel_process
        portal = self._teacher_portal_process
        tunnel_alive = tunnel is not None and tunnel.poll() is None
        portal_alive = portal is not None and portal.poll() is None
        if tunnel_alive and portal_alive and self._teacher_portal_url:
            try:
                with urllib.request.urlopen(self._teacher_portal_url + "/health", timeout=4) as response:
                    if response.status == 200:
                        webbrowser.open(self._teacher_portal_url + "/teacher")
                        return
            except (OSError, urllib.error.URLError):
                pass
            # A quick tunnel can die while its local process still appears alive.
            # Discard the stale URL and create a fresh tunnel on this same click.
            self._stop_teacher_services()
            tunnel = portal = None
            tunnel_alive = portal_alive = False
        if tunnel_alive and portal_alive:
            self._show_teacher_starting()
            if self._teacher_tunnel_poll_id is None:
                self._teacher_tunnel_poll_id = self.after(150, self._poll_teacher_tunnel)
            return
        if tunnel is not None or portal is not None:
            self._stop_teacher_services()

        bundled_cloudflared = BASE_DIR / "tools" / "cloudflared.exe"
        cloudflared = str(bundled_cloudflared) if bundled_cloudflared.is_file() else shutil.which("cloudflared")
        if not cloudflared:
            messagebox.showerror("TeacherIQ tunnel unavailable",
                "The secure website cannot open because cloudflared is unavailable. "
                "Restore tools\\cloudflared.exe and select TeacherIQ again.", parent=self)
            return

        try:
            self._teacher_tunnel_candidate_url = None
            self._teacher_tunnel_connected = False
            self._teacher_tunnel_dns_check_started = False
            self._teacher_tunnel_dns_ready = False
            self._teacher_tunnel_opened = False
            self._teacher_portal_ready = False
            self._teacher_tunnel_events = queue.Queue()
            self._teacher_portal_url = None
            self._teacher_portal_port = None
            self._teacher_local_opened = False
            with socket.socket() as port_probe:
                port_probe.bind(("127.0.0.1", 0))
                self._teacher_portal_port = port_probe.getsockname()[1]
            self._teacher_local_url = f"http://127.0.0.1:{self._teacher_portal_port}"
            self._save_setting("teacher_public_url", "")
            self._teacher_portal_process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "web_portal.main:app",
                 "--host", "127.0.0.1", "--port", str(self._teacher_portal_port)],
                cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._teacher_tunnel_started_at = time.monotonic()
            self._show_teacher_starting()
            self._teacher_tunnel_process = subprocess.Popen(
                [cloudflared, "tunnel", "--no-autoupdate", "--url",
                 f"http://127.0.0.1:{self._teacher_portal_port}"],
                cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(target=self._read_teacher_tunnel_output, daemon=True).start()
            threading.Thread(
                target=self._wait_for_teacher_portal,
                args=(self._teacher_local_url, self._teacher_tunnel_events), daemon=True,
            ).start()
        except OSError as error:
            self._stop_teacher_services()
            self._close_teacher_starting()
            messagebox.showerror("TeacherIQ could not start",
                f"Could not start the secure teacher website.\n\n{error}", parent=self)
            return

        self._teacher_tunnel_poll_id = self.after(150, self._poll_teacher_tunnel)

    def _show_teacher_starting(self, status="Starting secure tunnel and website…"):
        dialog = self._teacher_starting_dialog
        if dialog is not None and dialog.winfo_exists():
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("Starting TeacherIQ")
        dialog.geometry("440x180")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.protocol("WM_DELETE_WINDOW", self._cancel_teacher_startup)
        self._teacher_starting_dialog = dialog
        ctk.CTkLabel(dialog, text="Connecting TeacherIQ", font=("Segoe UI", 19, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=22, pady=(20, 8))
        self._teacher_start_status = ctk.CTkLabel(
            dialog, text=status, text_color=MUTED, wraplength=390, justify="left")
        self._teacher_start_status.pack(anchor="w", padx=22, pady=(0, 12))
        ctk.CTkButton(dialog, text="Cancel", height=34, fg_color="#1C3554",
                      command=self._cancel_teacher_startup).pack(anchor="e", padx=22, pady=(0, 16))
        dialog.lift()
        dialog.focus_force()

    def _set_teacher_start_status(self, text):
        dialog, label = self._teacher_starting_dialog, self._teacher_start_status
        if dialog is not None and label is not None:
            try:
                if dialog.winfo_exists() and label.winfo_exists():
                    label.configure(text=text)
            except tk.TclError:
                pass

    def _close_teacher_starting(self):
        dialog = self._teacher_starting_dialog
        self._teacher_starting_dialog = None
        self._teacher_start_status = None
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except tk.TclError:
                pass

    def _cancel_teacher_startup(self):
        if self._teacher_tunnel_poll_id is not None:
            try:
                self.after_cancel(self._teacher_tunnel_poll_id)
            except tk.TclError:
                pass
            self._teacher_tunnel_poll_id = None
        self._stop_teacher_services()
        self._close_teacher_starting()

    def _wait_for_teacher_portal(self, url, events):
        """Report local service readiness; do not open login before public tunnel is ready."""
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "/health", timeout=1) as response:
                    if response.status == 200:
                        events.put(("portal_ready", True))
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        events.put(("portal_error", "The local TeacherIQ service did not become ready."))

    def _read_teacher_tunnel_output(self):
        tunnel = self._teacher_tunnel_process
        if tunnel is None or tunnel.stdout is None:
            self._teacher_tunnel_events.put(("error", "Cloudflare Tunnel did not provide output."))
            return
        url_sent = False
        connected_sent = False
        for line in tunnel.stdout:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line, re.IGNORECASE)
            if match and not url_sent:
                self._teacher_tunnel_events.put(("url", match.group(0)))
                url_sent = True
            if "registered tunnel connection" in line.lower() and not connected_sent:
                self._teacher_tunnel_events.put(("connected", True))
                connected_sent = True
        if not url_sent:
            self._teacher_tunnel_events.put(("error", "Cloudflare Tunnel stopped before creating a public link."))

    def _poll_teacher_tunnel(self):
        self._teacher_tunnel_poll_id = None
        dns_wait_expired = False
        while True:
            try:
                kind, value = self._teacher_tunnel_events.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                self._stop_teacher_services()
                self._close_teacher_starting()
                messagebox.showerror(
                    "TeacherIQ phone link unavailable",
                    f"{value}\n\nSelect TeacherIQ to try starting the secure website again.",
                    parent=self,
                )
                return
            if kind == "url":
                self._teacher_tunnel_candidate_url = value
                self._set_teacher_start_status("Public link created. Waiting for the secure tunnel to connect…")
            elif kind == "connected":
                self._teacher_tunnel_connected = True
                self._set_teacher_start_status("Tunnel connected. Checking the public link before opening login…")
            elif kind == "portal_ready":
                self._teacher_portal_ready = True
            elif kind == "portal_error":
                self._stop_teacher_services()
                self._close_teacher_starting()
                messagebox.showerror("TeacherIQ could not start", value)
                return
            elif kind == "dns_ready":
                self._teacher_tunnel_dns_ready = True
            elif kind == "dns_wait_expired":
                dns_wait_expired = True

        tunnel = self._teacher_tunnel_process
        portal = self._teacher_portal_process
        if portal is None or portal.poll() is not None:
            self._stop_teacher_services()
            self._close_teacher_starting()
            messagebox.showerror(
                "TeacherIQ could not start",
                "The local TeacherIQ web service stopped. Select TeacherIQ to start it again.",
            )
            return
        if tunnel is None or tunnel.poll() is not None:
            self._stop_teacher_services()
            self._close_teacher_starting()
            messagebox.showerror(
                "TeacherIQ phone link unavailable",
                "Cloudflare Tunnel stopped before the login page could open. Select TeacherIQ to try again.",
                parent=self,
            )
            return

        if dns_wait_expired:
            self._set_teacher_start_status("Tunnel is connected. Windows DNS is still catching up; retrying automatically…")
            self._teacher_tunnel_dns_check_started = False
            self._teacher_tunnel_started_at = time.monotonic()

        if self._teacher_tunnel_candidate_url and self._teacher_tunnel_connected:
            self._save_setting("teacher_public_url", self._teacher_tunnel_candidate_url)
            if not self._teacher_tunnel_dns_check_started:
                self._teacher_tunnel_dns_check_started = True
                threading.Thread(
                    target=self._wait_for_teacher_tunnel_dns,
                    args=(self._teacher_tunnel_candidate_url, self._teacher_tunnel_events),
                    daemon=True,
                ).start()
            if not self._teacher_tunnel_dns_ready or not self._teacher_portal_ready:
                self._teacher_tunnel_poll_id = self.after(250, self._poll_teacher_tunnel)
                return
            self._teacher_portal_url = self._teacher_tunnel_candidate_url
            self._teacher_tunnel_opened = True
            self.log_activity("TEACHERIQ_OPEN", "Started TeacherIQ with a temporary public HTTPS link")
            webbrowser.open(self._teacher_portal_url + "/teacher")
            self._close_teacher_starting()
            messagebox.showinfo(
                "TeacherIQ is ready",
                "Sign in with your AttendIQ staff account, start a session, then show its QR code to students.\n\n"
                f"Student phone link: {self._teacher_portal_url}\n\n"
                "Anyone with a session QR can open that session link. The teacher page still requires your AttendIQ staff login.\n\n"
                "The public link works only while AttendIQ is open. Each attendance QR expires after its selected duration.",
            )
            self._teacher_tunnel_poll_id = self.after(3000, self._monitor_teacher_tunnel)
            return

        timed_out = time.monotonic() - getattr(self, "_teacher_tunnel_started_at", time.monotonic()) > 60
        if tunnel is None or tunnel.poll() is not None or timed_out:
            self._stop_teacher_services()
            self._close_teacher_starting()
            messagebox.showerror(
                "TeacherIQ link unavailable",
                "Cloudflare could not establish a live HTTPS connection. Check your internet connection and select TeacherIQ to retry.",
                parent=self,
            )
            return
        self._teacher_tunnel_poll_id = self.after(250, self._poll_teacher_tunnel)

    @staticmethod
    def _wait_for_teacher_tunnel_dns(url, events):
        """Do not open the temporary link until Windows DNS can resolve it."""
        hostname = url.split("//", 1)[-1].split("/", 1)[0]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
                events.put(("dns_ready", True))
                return
            except OSError:
                time.sleep(0.5)
        events.put(("dns_wait_expired", hostname))

    def _monitor_teacher_tunnel(self):
        tunnel = self._teacher_tunnel_process
        if tunnel is None or tunnel.poll() is not None:
            self._teacher_portal_url = None
            self._stop_teacher_services()
            messagebox.showwarning(
                "TeacherIQ link stopped",
                "The temporary phone link disconnected. Select TeacherIQ to create a fresh link, then sign in again.",
            )
            return
        self._teacher_tunnel_poll_id = self.after(3000, self._monitor_teacher_tunnel)

    def _stop_teacher_services(self):
        self._teacher_portal_url = None
        for process in (self._teacher_tunnel_process, self._teacher_portal_process):
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        self._teacher_tunnel_process = None
        self._teacher_portal_process = None
        self._teacher_portal_port = None
        self._teacher_local_url = None
        self._teacher_local_opened = False
        self._teacher_portal_ready = False
        self._close_teacher_starting()
        self._save_setting("teacher_public_url", "")

    def _stop_teacher_tunnel(self):
        self._teacher_portal_url = None
        tunnel = self._teacher_tunnel_process
        if tunnel is not None and tunnel.poll() is None:
            try:
                tunnel.terminate()
            except OSError:
                pass
        self._teacher_tunnel_process = None
        self._teacher_tunnel_candidate_url = None
        self._teacher_tunnel_connected = False
        self._teacher_tunnel_dns_check_started = False
        self._teacher_tunnel_dns_ready = False
        self._save_setting("teacher_public_url", "")

    # ---------------- COMMON UI ----------------

    def clear_content(self):
        if self.recognition_running or self.recognition_camera is not None or self.recognition_loading:
            self.stop_recognition_camera(silent=True)
        old = self.content
        if old is not None:
            old.grid_remove()          # hide immediately, destroy later
            self._trash.append(old)
        frame = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        self.content = frame
        if self._reveal_id is None:    # runs once the page has finished building
            self._reveal_id = self.after_idle(self._reveal_content)

    def _reveal_content(self):
        self._reveal_id = None
        if not self.winfo_exists() or self.content is None:
            return
        self.content.grid(row=0, column=1, sticky="nsew", padx=22, pady=22)
        trash, self._trash = self._trash, []

        # Destroy the old page a few widgets at a time so the UI never freezes.
        queue = []
        for frame in trash:
            try:
                queue.extend(frame.winfo_children())
            except Exception:
                pass
            queue.append(frame)

        def cleanup():
            for _ in range(8):
                if not queue:
                    return
                widget = queue.pop(0)
                try:
                    widget.destroy()
                except Exception:
                    pass
            if queue:
                self.after(4, cleanup)
        if queue:
            self.after(120, cleanup)

    def select_nav(self, active):
        current = getattr(self, "_active_nav", None)
        if current == active:
            return
        if current in self.nav_buttons:
            self.nav_buttons[current].configure(fg_color="transparent")
        if active in self.nav_buttons:
            self.nav_buttons[active].configure(fg_color=BLUE)
        self._active_nav = active

    def fill_batched(self, table, rows, build, batch=12):
        """Build table rows a few at a time so the page shows instantly and the
        UI never freezes. A newer fill on the same table cancels an older one."""
        token = object()
        table._fill_token = token

        def step(pos):
            if getattr(table, "_fill_token", None) is not token:
                return
            try:
                if not table.winfo_exists():
                    return
            except Exception:
                return
            stop = min(pos + batch, len(rows))
            for i in range(pos, stop):
                build(i + 1, rows[i])
            if stop < len(rows):
                self.after(1, lambda: step(stop))
        self.after(1, lambda: step(0))

    def header(self, title, subtitle):
        top = ctk.CTkFrame(self.content, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 22))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=title, font=("Segoe UI", 27, "bold"),
                     text_color=TEXT).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(top, text=subtitle, font=("Segoe UI", 12),
                     text_color=MUTED).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ctk.CTkLabel(
            top, text=date.today().strftime("%d %b %Y"),
            font=("Segoe UI", 12, "bold"), text_color=TEXT,
            fg_color=WHITE, corner_radius=9, padx=14, pady=10
        ).grid(row=0, column=1, rowspan=2, sticky="e")

    def white_panel(self, parent):
        return ctk.CTkFrame(parent, fg_color=WHITE, corner_radius=13,
                            border_width=1, border_color=BORDER)

    def card(self, parent, title, value, note, accent):
        panel = self.white_panel(parent)
        ctk.CTkLabel(panel, text=title, font=("Segoe UI", 12),
                     text_color=MUTED).pack(anchor="w", padx=18, pady=(17, 4))
        ctk.CTkLabel(panel, text=str(value), font=("Segoe UI", 29, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=18)
        ctk.CTkLabel(panel, text=note, font=("Segoe UI", 10),
                     text_color=accent).pack(anchor="w", padx=18, pady=(4, 17))
        return panel

    def make_panel(self, parent, title):
        panel = self.white_panel(parent)
        ctk.CTkLabel(panel, text=title, font=("Segoe UI", 16, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=20, pady=(18, 14))
        return panel

    def valid_date(self, value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None

    # --- shared table helpers (consistent column alignment, no duplicated code) ---

    def _px(self, value):
        """Scale a pixel size like CustomTkinter does (for plain tk widgets)."""
        try:
            return max(1, int(round(value * self._get_window_scaling())))
        except Exception:
            return value

    def _font(self, size, weight="normal"):
        return ("Segoe UI", -self._px(size), weight)

    # Table cells use plain tk widgets: they look identical but build about
    # 10x faster than CTkLabel/CTkFrame, which is what made pages lag.
    def table_header(self, table, labels, weights, uniform):
        table.grid_columnconfigure(0, weight=1)
        head = tk.Frame(table, bg="#172B46", bd=0, highlightthickness=0)
        head.grid(row=0, column=0, sticky="ew", pady=(0, self._px(6)))
        for c, (label, weight) in enumerate(zip(labels, weights)):
            head.grid_columnconfigure(c, weight=weight, uniform=uniform)
            tk.Label(head, text=label, font=self._font(10, "bold"), fg=MUTED,
                     bg="#172B46", anchor="w", bd=0, highlightthickness=0
                     ).grid(row=0, column=c, sticky="ew", padx=self._px(12), pady=self._px(11))

    def table_row(self, table, index, values, weights, uniform,
                  colors=None, bold=(), shade=True):
        """Add one row. A value of None leaves that cell empty for custom widgets."""
        bg = "#0C192B" if (shade and index % 2) else WHITE
        row = tk.Frame(table, bg=bg, bd=0, highlightthickness=0)
        row.grid(row=index, column=0, sticky="ew", pady=self._px(2))
        padx, pady = self._px(12), self._px(10)
        for c, (value, weight) in enumerate(zip(values, weights)):
            row.grid_columnconfigure(c, weight=weight, uniform=uniform)
            if value is None:
                continue
            tk.Label(
                row, text=str(value), fg=(colors or {}).get(c, TEXT), bg=bg,
                anchor="w", bd=0, highlightthickness=0,
                font=self._font(11, "bold" if c in bold else "normal")
            ).grid(row=0, column=c, sticky="ew", padx=padx, pady=pady)
        return row

    def empty_message(self, table, text):
        ctk.CTkLabel(table, text=text, text_color=MUTED).grid(row=1, column=0, pady=25)

    @staticmethod
    def clear_children(widget):
        """Hide old rows instantly, then destroy them a few at a time so a
        filter change never freezes the window."""
        kids = widget.winfo_children()
        for child in kids:
            try:
                child.grid_forget()
            except Exception:
                pass
        if not kids:
            return
        root = widget._root()

        def purge():
            for _ in range(6):
                if not kids:
                    return
                try:
                    kids.pop().destroy()
                except Exception:
                    pass
            if kids:
                root.after(2, purge)
        root.after(40, purge)

    def mini_button(self, parent, text, bg, hover, fg, command):
        """Lightweight button (a tk.Label) - ~10x cheaper than CTkButton in long tables."""
        lab = tk.Label(parent, text=text, bg=bg, fg=fg, cursor="hand2",
                       font=self._font(11, "bold"), padx=self._px(14), pady=self._px(6))
        lab.bind("<Enter>", lambda _e: lab.configure(bg=hover))
        lab.bind("<Leave>", lambda _e: lab.configure(bg=bg))
        lab.bind("<Button-1>", lambda _e: command())
        return lab

    # ---------------- NEW INTELLIGENCE / AUDIT HELPERS ----------------

    def log_activity(self, action, details, actor=None):
        actor = actor or self.current_username()
        try:
            with db() as conn:
                conn.execute("INSERT INTO activity_log(event_time, actor, action, details) VALUES(?,?,?,?)",
                             (datetime.now().isoformat(timespec="seconds"), actor, action, str(details)))
        except sqlite3.Error as error:
            print("Activity log warning:", error)

    @staticmethod
    def _eye_ratio(eye):
        # Eye aspect ratio using six eye landmark points.
        def dist(a, b):
            return float(np.linalg.norm(np.array(a) - np.array(b)))
        return (dist(eye[1], eye[5]) + dist(eye[2], eye[4])) / (2.0 * max(dist(eye[0], eye[3]), 1e-6))

    @staticmethod
    def _setting(key, default=""):
        with db() as conn:
            row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _save_setting(key, value):
        with db() as conn:
            conn.execute("INSERT INTO app_settings(setting_key,setting_value) VALUES(?,?) "
                         "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                         (key, str(value)))

    def attendance_threshold(self):
        try:
            return max(1.0, min(100.0, float(self._setting("attendance_threshold", "75"))))
        except (TypeError, ValueError):
            return 75.0

    def show_settings_backup(self):
        self.clear_content(); self.select_nav("Settings & Backup")
        self.header("Settings & Backup", "Manage attendance rules, email configuration, templates and data backups")
        self.content.grid_rowconfigure(1, weight=1)
        area = FastScrollFrame(self.content, fg_color="transparent", corner_radius=0)
        area.grid(row=1, column=0, sticky="nsew"); area.grid_columnconfigure(0, weight=1)

        # Attendance threshold
        threshold_panel = self.white_panel(area); threshold_panel.grid(row=0, column=0, sticky="ew", padx=2, pady=(0, 12))
        ctk.CTkLabel(threshold_panel, text="Attendance Warning Threshold", font=("Segoe UI", 17, "bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(15, 6))
        ctk.CTkLabel(threshold_panel, text="Students below this percentage will be included in low-attendance alerts.", text_color=MUTED).pack(anchor="w", padx=18)
        threshold_entry = ctk.CTkEntry(threshold_panel, width=140, placeholder_text="75")
        threshold_entry.insert(0, str(self.attendance_threshold())); threshold_entry.pack(anchor="w", padx=18, pady=10)

        # SMTP credentials: encrypted using existing Fernet-based email automation storage.
        smtp_panel = self.white_panel(area); smtp_panel.grid(row=1, column=0, sticky="ew", padx=2, pady=12); smtp_panel.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(smtp_panel, text="Saved SMTP Details", font=("Segoe UI", 17, "bold"), text_color=TEXT).grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(15, 8))
        saved = self._load_email_automation() or {"enabled": False, "weekday": 4, "send_time": "16:00", "config": {}}
        conf = saved.get("config", {})
        smtp_fields = {}
        for idx, (label, key, default, masked) in enumerate([
            ("SMTP host", "host", "smtp.gmail.com", False), ("SMTP port", "port", "587", False),
            ("Sender email", "sender", "name@example.com", False), ("App password", "password", "Google App Password", True)] , start=1):
            ctk.CTkLabel(smtp_panel, text=label, text_color=TEXT).grid(row=idx, column=0, sticky="w", padx=18, pady=6)
            ent=ctk.CTkEntry(smtp_panel, placeholder_text=default, show="•" if masked else "")
            ent.grid(row=idx, column=1, sticky="ew", padx=18, pady=6)
            if conf.get(key): ent.insert(0, str(conf[key]))
            smtp_fields[key]=ent

        # Email templates
        template_panel = self.white_panel(area); template_panel.grid(row=2, column=0, sticky="ew", padx=2, pady=12); template_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(template_panel, text="Editable Email Templates", font=("Segoe UI", 17, "bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(15, 5))
        ctk.CTkLabel(template_panel, text="Available placeholders: {name}, {percentage}, {threshold}", text_color=MUTED).pack(anchor="w", padx=18, pady=(0, 8))
        template_fields={}
        for title,key in [("Student subject", "student_email_subject"),("Student message", "student_email_template"),("Parent subject", "parent_email_subject"),("Parent message", "parent_email_template")]:
            ctk.CTkLabel(template_panel, text=title, text_color=TEXT).pack(anchor="w", padx=18, pady=(7, 3))
            if "template" in key:
                widget=ctk.CTkTextbox(template_panel, height=105, wrap="word")
                widget.insert("1.0", self._setting(key, ""))
            else:
                widget=ctk.CTkEntry(template_panel); widget.insert(0, self._setting(key, ""))
            widget.pack(fill="x", padx=18, pady=(0, 5)); template_fields[key]=widget

        def save_settings():
            try: value=float(threshold_entry.get().strip())
            except ValueError:
                messagebox.showerror("Invalid threshold", "Enter a number from 1 to 100."); return
            if not 1 <= value <= 100:
                messagebox.showerror("Invalid threshold", "Attendance threshold must be between 1 and 100."); return
            config={"host":smtp_fields["host"].get().strip() or "smtp.gmail.com", "port":smtp_fields["port"].get().strip() or "587",
                    "sender":smtp_fields["sender"].get().strip(), "password":"".join(smtp_fields["password"].get().split()),
                    "send_to_students":bool(conf.get("send_to_students", True)), "send_to_parents":bool(conf.get("send_to_parents", True))}
            if config["password"] == "" and conf.get("password"): config["password"]=conf["password"]
            try: int(config["port"])
            except ValueError: messagebox.showerror("SMTP port", "SMTP port must be numeric."); return
            self._save_setting("attendance_threshold", value)
            for key, widget in template_fields.items():
                text = widget.get("1.0", "end-1c") if isinstance(widget, ctk.CTkTextbox) else widget.get()
                self._save_setting(key, text)
            self._save_email_automation(saved.get("enabled", False), saved.get("weekday", 4), saved.get("send_time", "16:00"), config)
            self.log_activity("SETTINGS_UPDATED", f"Attendance threshold set to {value:.1f}%; SMTP and email templates saved")
            messagebox.showinfo("Settings saved", "Attendance threshold, SMTP details and email templates have been saved.")
        ctk.CTkButton(template_panel, text="Save Settings", command=save_settings, fg_color=BLUE, text_color="#04111F").pack(anchor="e", padx=18, pady=14)

        # Backup and restore
        backup_panel=self.white_panel(area); backup_panel.grid(row=3,column=0,sticky="ew",padx=2,pady=12)
        ctk.CTkLabel(backup_panel,text="Backup & Restore",font=("Segoe UI",17,"bold"),text_color=TEXT).pack(anchor="w",padx=18,pady=(15,5))
        ctk.CTkLabel(backup_panel,text="Create a ZIP containing the database, student photos and face-encoding cache. Restoring replaces current project data.",text_color=MUTED,wraplength=850,justify="left").pack(anchor="w",padx=18,pady=(0,10))
        def make_backup():
            target=filedialog.asksaveasfilename(title="Save AttendIQ Backup",defaultextension=".zip",filetypes=[("ZIP archive","*.zip")],initialfile=f"AttendIQ_Backup_{datetime.now():%Y%m%d_%H%M}.zip")
            if not target: return
            try:
                with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
                    for folder in ("database", "images"):
                        root=BASE_DIR/folder
                        if root.exists():
                            for item in root.rglob("*"):
                                if item.is_file(): archive.write(item,item.relative_to(BASE_DIR).as_posix())
                    for filename in ("face_encodings.pkl",):
                        item=BASE_DIR/filename
                        if item.is_file(): archive.write(item,item.relative_to(BASE_DIR).as_posix())
                self.log_activity("BACKUP_CREATED", f"Backup saved to {target}")
                messagebox.showinfo("Backup complete", "Your database and student photos have been saved to a ZIP file.")
            except Exception as ex: messagebox.showerror("Backup failed", str(ex))
        def restore_backup():
            source=filedialog.askopenfilename(title="Select AttendIQ Backup",filetypes=[("ZIP archive","*.zip")])
            if not source: return
            if not messagebox.askyesno("Confirm restore", "Restoring will replace the current database, images and face-encoding cache. A safety backup of the current data will be created first. Continue?"):
                return
            try:
                with zipfile.ZipFile(source,"r") as archive:
                    allowed=("database/", "images/", "face_encodings.pkl")
                    names=archive.namelist()
                    if not names or any(not n.startswith(allowed) or ".." in Path(n).parts or Path(n).is_absolute() for n in names):
                        raise ValueError("This ZIP is not a valid AttendIQ backup or contains unsafe paths.")
                    if "database/attendance.db" not in names and "attendance.db" not in names:
                        raise ValueError("The backup does not contain database/attendance.db.")
                    # Create safety backup before changing any current data.
                    safety=BASE_DIR/f"AttendIQ_PreRestore_{datetime.now():%Y%m%d_%H%M%S}.zip"
                    with zipfile.ZipFile(safety,"w",zipfile.ZIP_DEFLATED) as out:
                        for folder in ("database", "images"):
                            root=BASE_DIR/folder
                            if root.exists():
                                for item in root.rglob("*"):
                                    if item.is_file(): out.write(item,item.relative_to(BASE_DIR).as_posix())
                        cache=BASE_DIR/"face_encodings.pkl"
                        if cache.exists(): out.write(cache,"face_encodings.pkl")
                    self.stop_recognition_camera(silent=True)
                    for name in names:
                        dest=BASE_DIR/name; dest.parent.mkdir(parents=True,exist_ok=True)
                        with archive.open(name) as src, open(dest,"wb") as dst: shutil.copyfileobj(src,dst)
                self.log_activity("BACKUP_RESTORED", f"Restored from {source}; safety backup: {safety.name}")
                messagebox.showinfo("Restore complete", f"Backup restored. A safety backup was saved as {safety.name}. Restart AttendIQ to reload all data.")
            except Exception as ex: messagebox.showerror("Restore failed", str(ex))
        row=ctk.CTkFrame(backup_panel,fg_color="transparent"); row.pack(anchor="w",padx=18,pady=(0,15))
        ctk.CTkButton(row,text="Create ZIP Backup",command=make_backup,fg_color=BLUE,text_color="#04111F").pack(side="left",padx=(0,10))
        ctk.CTkButton(row,text="Restore from ZIP",command=restore_backup,fg_color="#8C4B57").pack(side="left")

    def attendance_percentage(self, student_id):
        with db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM attendance a WHERE a.student_id=? AND a.status IN ('Present','Absent') AND NOT {EXCUSED_SQL}", (student_id,)).fetchone()[0]
            present = conn.execute("SELECT COUNT(*) FROM attendance WHERE student_id=? AND status='Present'", (student_id,)).fetchone()[0]
        return (present / total * 100.0) if total else 0.0

    def show_activity_log(self):
        self.clear_content(); self.select_nav("Activity Log")
        self.header("Security Activity Log", "Local audit trail for important application actions")
        panel=self.white_panel(self.content); panel.grid(row=1,column=0,sticky="nsew"); panel.grid_columnconfigure(0,weight=1); panel.grid_rowconfigure(1,weight=1)
        ctk.CTkLabel(panel,text="Recent system events",font=("Segoe UI",17,"bold"),text_color=TEXT).grid(row=0,column=0,sticky="w",padx=18,pady=14)
        table=FastScrollFrame(panel,fg_color="transparent"); table.grid(row=1,column=0,sticky="nsew",padx=12,pady=(0,12))
        weights=[2,1,2,5]; self.table_header(table,["TIME","ACTOR","ACTION","DETAILS"],weights,"audit")
        with db() as conn: rows=conn.execute("SELECT event_time,actor,action,details FROM activity_log ORDER BY id DESC LIMIT 500").fetchall()
        if not rows: self.empty_message(table,"No activity recorded yet."); return
        self.fill_batched(
            table, rows,
            lambda i, row: self.table_row(table, i, row, weights, "audit", shade=False),
        )

    def show_timetable(self):
        self.clear_content(); self.select_nav("Timetable")
        self.header("Timetable & Sessions", "Choose a year and department to view the weekly class schedule")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)
        filters = self.white_panel(self.content)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        filters.grid_columnconfigure(3, weight=1)
        with db() as conn:
            saved_years = [r[0] for r in conn.execute(
                "SELECT DISTINCT academic_year FROM timetable WHERE COALESCE(academic_year,'')<>'' ORDER BY academic_year").fetchall()]
            saved_departments = [r[0] for r in conn.execute(
                "SELECT DISTINCT department FROM timetable WHERE COALESCE(department,'')<>'' ORDER BY department").fetchall()]
        year_filter = ctk.CTkOptionMenu(filters, values=["All Years"] + list(dict.fromkeys(saved_years + YEARS[1:])), width=150)
        year_filter.set("All Years")
        year_filter.grid(row=0, column=0, padx=(12, 6), pady=12, sticky="w")
        department_values = ["All Departments"] + list(dict.fromkeys(saved_departments + [d for d in DEPARTMENTS if d != "All Departments"]))
        department_filter = ctk.CTkOptionMenu(filters, values=department_values, width=190)
        department_filter.set("All Departments")
        department_filter.grid(row=0, column=1, padx=6, pady=12, sticky="w")
        class_filter = ctk.CTkOptionMenu(filters, values=["All Classes"], width=150)
        class_filter.set("All Classes")
        class_filter.grid(row=0, column=2, padx=6, pady=12, sticky="w")
        subject_search = ctk.CTkEntry(filters, placeholder_text="Search subject…", width=190, height=36)
        subject_search.grid(row=0, column=3, padx=6, pady=12, sticky="ew")
        form = self.white_panel(self.content)
        form.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        form.grid_remove()
        form_state = {"visible": False}
        def toggle_add_form():
            if form_state["visible"]:
                form.grid_remove()
                form_state["visible"] = False
                add_session_button.configure(text="＋ Add Session")
            else:
                form.grid(row=3, column=0, sticky="ew", pady=(0, 12))
                form_state["visible"] = True
                add_session_button.configure(text="Hide Add Session")
        add_session_button = ctk.CTkButton(filters, text="＋ Add Session", width=140,
                                           command=toggle_add_form, fg_color="#1C3554")
        add_session_button.grid(row=0, column=4, padx=(6, 12), pady=12, sticky="e")
        form.grid_columnconfigure((0,1,2,3,4,5), weight=1)
        subject=ctk.CTkEntry(form,placeholder_text="Subject name",width=170); subject.grid(row=0,column=0,padx=7,pady=9,sticky="ew")
        period=ctk.CTkEntry(form,placeholder_text="Period (e.g. P1)",width=120); period.grid(row=0,column=1,padx=7,pady=9,sticky="ew")
        year=ctk.CTkOptionMenu(form,values=YEARS[1:],width=120); year.set("1st Year"); year.grid(row=0,column=2,padx=7,pady=9,sticky="ew")
        department=ctk.CTkEntry(form,placeholder_text="Department (e.g. CSE)",width=140); department.grid(row=0,column=3,padx=7,pady=9,sticky="ew")
        class_name=ctk.CTkEntry(form,placeholder_text="Class / Section (e.g. A)",width=140); class_name.grid(row=0,column=4,padx=7,pady=9,sticky="ew")
        weekday=ctk.CTkOptionMenu(form,values=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],width=125); weekday.grid(row=1,column=0,padx=7,pady=9,sticky="ew")
        start=ctk.CTkEntry(form,placeholder_text="Start HH:MM",width=110); start.grid(row=1,column=1,padx=7,pady=9,sticky="ew")
        end=ctk.CTkEntry(form,placeholder_text="End HH:MM",width=110); end.grid(row=1,column=2,padx=7,pady=9,sticky="ew")
        def load_third_year_cse():
            # Periods copied from the supplied 3rd Year CSE timetable. Breaks are
            # intentionally omitted; consecutive lab/project periods are combined.
            schedule = [
                ("Monday", "P1", "CN", "08:00", "08:50"),
                ("Monday", "P2", "CC", "08:50", "09:40"),
                ("Monday", "P3", "DT", "09:55", "10:45"),
                ("Monday", "P4", "DM", "10:45", "11:35"),
                ("Monday", "P5", "EE", "11:35", "12:25"),
                ("Monday", "P6", "TC", "13:10", "14:00"),
                ("Monday", "P7", "Mini Project", "14:00", "14:50"),
                ("Tuesday", "P1", "DM", "08:00", "08:50"),
                ("Tuesday", "P2", "CN", "08:50", "09:40"),
                ("Tuesday", "P3", "CC", "09:55", "10:45"),
                ("Tuesday", "P4", "DT", "10:45", "11:35"),
                ("Tuesday", "P5", "UI&UX", "11:35", "12:25"),
                ("Tuesday", "P6-P7", "CC LAB", "13:10", "14:50"),
                ("Wednesday", "P1", "CN", "08:00", "08:50"),
                ("Wednesday", "P2", "LIB", "08:50", "09:40"),
                ("Wednesday", "P3", "DM", "09:55", "10:45"),
                ("Wednesday", "P4", "CC", "10:45", "11:35"),
                ("Wednesday", "P5", "TC", "11:35", "12:25"),
                ("Wednesday", "P6", "UI&UX", "13:10", "14:00"),
                ("Wednesday", "P7", "Sports", "14:00", "14:50"),
                ("Thursday", "P1", "TC", "08:00", "08:50"),
                ("Thursday", "P2", "CN", "08:50", "09:40"),
                ("Thursday", "P3", "DM", "09:55", "10:45"),
                ("Thursday", "P4", "DT", "10:45", "11:35"),
                ("Thursday", "P5", "TC", "11:35", "12:25"),
                ("Thursday", "P6-P7", "UI&UX LAB", "13:10", "14:50"),
                ("Friday", "P1", "CC", "08:00", "08:50"),
                ("Friday", "P2", "DT", "08:50", "09:40"),
                ("Friday", "P3-P4", "Mini Project", "09:55", "11:35"),
                ("Friday", "P5", "UI&UX", "11:35", "12:25"),
                ("Friday", "P6-P7", "CN LAB", "13:10", "14:50"),
            ]
            added = 0
            with db() as conn:
                for day, period_name, subj, start_time, end_time in schedule:
                    exists = conn.execute(
                        "SELECT 1 FROM timetable WHERE subject=? AND period=? AND weekday=? "
                        "AND start_time=? AND end_time=? AND academic_year=? AND department=? "
                        "AND COALESCE(class_name,'')='' LIMIT 1",
                        (subj, period_name, day, start_time, end_time, "3rd Year", "CSE"),
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        "INSERT INTO timetable(subject,period,weekday,start_time,end_time,academic_year,department,class_name) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (subj, period_name, day, start_time, end_time, "3rd Year", "CSE", ""),
                    )
                    added += 1
            if added:
                self.log_activity("TIMETABLE_IMPORT", f"Loaded {added} timetable entries for 3rd Year CSE")
                year_filter.set("3rd Year")
                department_filter.set("CSE")
                refresh()
                messagebox.showinfo("Timetable Added", f"Added {added} schedule entries for all 3rd Year CSE classes.")
            else:
                year_filter.set("3rd Year")
                department_filter.set("CSE")
                refresh()
                messagebox.showinfo("Timetable Already Added", "This 3rd Year CSE timetable is already in the schedule.")

        def load_third_year_cyber_security():
            # Tuesday P3 is illegible in the supplied image, so it is left blank.
            schedule = [
                ("Monday", "P1", "CN", "08:00", "08:50"),
                ("Monday", "P2", "C&CS", "08:50", "09:40"),
                ("Monday", "P3", "DT", "09:55", "10:45"),
                ("Monday", "P4", "DM", "10:45", "11:35"),
                ("Monday", "P5", "EE", "11:35", "12:25"),
                ("Monday", "P6-P7", "C&CS LAB", "13:10", "14:50"),
                ("Tuesday", "P1", "DM", "08:00", "08:50"),
                ("Tuesday", "P2", "CN", "08:50", "09:40"),
                ("Tuesday", "P4", "DT", "10:45", "11:35"),
                ("Tuesday", "P5", "UI&UX", "11:35", "12:25"),
                ("Tuesday", "P6-P7", "EH LAB", "13:10", "14:50"),
                ("Wednesday", "P1", "CN", "08:00", "08:50"),
                ("Wednesday", "P2", "LIB", "08:50", "09:40"),
                ("Wednesday", "P3", "DM", "09:55", "10:45"),
                ("Wednesday", "P4", "C&CS", "10:45", "11:35"),
                ("Wednesday", "P5", "EH", "11:35", "12:25"),
                ("Wednesday", "P6", "UI&UX", "13:10", "14:00"),
                ("Wednesday", "P7", "Sports", "14:00", "14:50"),
                ("Thursday", "P1", "EH", "08:00", "08:50"),
                ("Thursday", "P2", "CN", "08:50", "09:40"),
                ("Thursday", "P3", "DM", "09:55", "10:45"),
                ("Thursday", "P4", "DT", "10:45", "11:35"),
                ("Thursday", "P5", "EH", "11:35", "12:25"),
                ("Thursday", "P6-P7", "UI&UX LAB", "13:10", "14:50"),
                ("Friday", "P1", "C&CS", "08:00", "08:50"),
                ("Friday", "P2", "DT", "08:50", "09:40"),
                ("Friday", "P3-P4", "Mini Project", "09:55", "11:35"),
                ("Friday", "P5", "UI&UX", "11:35", "12:25"),
                ("Friday", "P6-P7", "CN LAB", "13:10", "14:50"),
            ]
            department_name = "CYBER SECURITY"
            added = 0
            with db() as conn:
                for day, period_name, subj, start_time, end_time in schedule:
                    exists = conn.execute(
                        "SELECT 1 FROM timetable WHERE subject=? AND period=? AND weekday=? "
                        "AND start_time=? AND end_time=? AND academic_year=? AND department=? "
                        "AND COALESCE(class_name,'')='' LIMIT 1",
                        (subj, period_name, day, start_time, end_time, "3rd Year", department_name),
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        "INSERT INTO timetable(subject,period,weekday,start_time,end_time,academic_year,department,class_name) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (subj, period_name, day, start_time, end_time, "3rd Year", department_name, ""),
                    )
                    added += 1
            if added:
                self.log_activity("TIMETABLE_IMPORT", f"Loaded {added} timetable entries for 3rd Year {department_name}")
                year_filter.set("3rd Year")
                department_filter.set("CYBER SECURITY")
                refresh()
                messagebox.showinfo(
                    "Timetable Added",
                    f"Added {added} schedule entries for all 3rd Year Cyber Security classes. "
                    "Tuesday P3 was skipped because its subject is unreadable in the image.",
                )
            else:
                year_filter.set("3rd Year")
                department_filter.set("CYBER SECURITY")
                refresh()
                messagebox.showinfo(
                    "Timetable Already Added",
                    "This 3rd Year Cyber Security timetable is already in the schedule.",
                )

        def load_third_year_ai_ds():
            schedule = [
                ("Monday", "P1", "CN", "08:00", "08:50"),
                ("Monday", "P2", "BDA", "08:50", "09:40"),
                ("Monday", "P3", "DT", "09:55", "10:45"),
                ("Monday", "P4", "DM", "10:45", "11:35"),
                ("Monday", "P5", "EE", "11:35", "12:25"),
                ("Monday", "P6-P7", "ML LAB", "13:10", "14:50"),
                ("Tuesday", "P1", "DM", "08:00", "08:50"),
                ("Tuesday", "P2", "CN", "08:50", "09:40"),
                ("Tuesday", "P3", "BDA", "09:55", "10:45"),
                ("Tuesday", "P4", "DT", "10:45", "11:35"),
                ("Tuesday", "P5", "UI&UX", "11:35", "12:25"),
                ("Tuesday", "P6-P7", "BDA LAB", "13:10", "14:50"),
                ("Wednesday", "P1", "CN", "08:00", "08:50"),
                ("Wednesday", "P2", "LIB", "08:50", "09:40"),
                ("Wednesday", "P3", "DM", "09:55", "10:45"),
                ("Wednesday", "P4", "BDA", "10:45", "11:35"),
                ("Wednesday", "P5", "ML", "11:35", "12:25"),
                ("Wednesday", "P6", "UI&UX", "13:10", "14:00"),
                ("Wednesday", "P7", "Sports", "14:00", "14:50"),
                ("Thursday", "P1", "ML", "08:00", "08:50"),
                ("Thursday", "P2", "CN", "08:50", "09:40"),
                ("Thursday", "P3", "DM", "09:55", "10:45"),
                ("Thursday", "P4", "DT", "10:45", "11:35"),
                ("Thursday", "P5", "ML", "11:35", "12:25"),
                ("Thursday", "P6-P7", "UI&UX LAB", "13:10", "14:50"),
                ("Friday", "P1", "BDA", "08:00", "08:50"),
                ("Friday", "P2", "DT", "08:50", "09:40"),
                ("Friday", "P3-P4", "Mini Project", "09:55", "11:35"),
                ("Friday", "P5", "UI&UX", "11:35", "12:25"),
                ("Friday", "P6-P7", "CN LAB", "13:10", "14:50"),
            ]
            department_name = "AI & DS"
            added = 0
            with db() as conn:
                for day, period_name, subj, start_time, end_time in schedule:
                    exists = conn.execute(
                        "SELECT 1 FROM timetable WHERE subject=? AND period=? AND weekday=? "
                        "AND start_time=? AND end_time=? AND academic_year=? AND department=? "
                        "AND COALESCE(class_name,'')='' LIMIT 1",
                        (subj, period_name, day, start_time, end_time, "3rd Year", department_name),
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        "INSERT INTO timetable(subject,period,weekday,start_time,end_time,academic_year,department,class_name) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (subj, period_name, day, start_time, end_time, "3rd Year", department_name, ""),
                    )
                    added += 1
            if added:
                self.log_activity("TIMETABLE_IMPORT", f"Loaded {added} timetable entries for 3rd Year {department_name}")
                year_filter.set("3rd Year")
                department_filter.set(department_name)
                refresh()
                messagebox.showinfo("Timetable Added", f"Added {added} schedule entries for all 3rd Year AI & DS classes.")
            else:
                year_filter.set("3rd Year")
                department_filter.set("AI & DS")
                refresh()
                messagebox.showinfo("Timetable Already Added", "This 3rd Year AI & DS timetable is already in the schedule.")

        view_panel = self.white_panel(self.content)
        view_panel.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        view_panel.grid_columnconfigure(0, weight=1)
        view_panel.grid_rowconfigure(1, weight=1)
        view_title = ctk.CTkLabel(view_panel, text="Weekly Timetable", font=("Segoe UI", 17, "bold"), text_color=TEXT)
        view_title.grid(row=0, column=0, sticky="w", padx=16, pady=(13, 8))
        table = tk.Frame(view_panel, bg=WHITE, bd=0, highlightthickness=0)
        table.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 12))
        table.grid_columnconfigure(0, weight=0, minsize=self._px(118))
        def refresh():
            for widget in table.winfo_children():
                widget.destroy()
            with db() as conn:
                rows = conn.execute(
                    "SELECT id,subject,COALESCE(academic_year,''),COALESCE(department,''),COALESCE(class_name,''),weekday,start_time,end_time,period,active "
                    "FROM timetable WHERE active=1 ORDER BY academic_year,department,class_name,weekday,start_time,id").fetchall()
            selected_year, selected_dept = year_filter.get(), department_filter.get()
            scoped = [r for r in rows if (selected_year == "All Years" or r[2] == selected_year)
                      and (selected_dept == "All Departments" or r[3].casefold() == selected_dept.casefold())]
            classes = sorted({r[4] for r in scoped if r[4]})
            class_values = ["All Classes"] + classes
            previous_class = class_filter.get()
            class_filter.configure(values=class_values)
            class_filter.set(previous_class if previous_class in class_values else "All Classes")
            selected_class = class_filter.get()
            needle = subject_search.get().strip().casefold()
            scoped = [r for r in scoped if (selected_class == "All Classes" or r[4] == selected_class)
                      and (not needle or needle in r[1].casefold())]
            selected_groups = [v for v in (selected_year, selected_dept, selected_class)
                               if v not in ("All Years", "All Departments", "All Classes")]
            view_title.configure(text="Weekly Timetable" + (" · " + " / ".join(selected_groups) if selected_groups else " · All Groups"))
            for widget in view_panel.grid_slaves(row=2):
                widget.destroy()
            if selected_year == "All Years" or selected_dept == "All Departments":
                ctk.CTkLabel(view_panel, text="Select an academic year and department, then press Search to view the timetable.",
                             text_color=MUTED).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))
                return
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
            period_times = [("P1", "08:00–08:50"), ("P2", "08:50–09:40"), ("P3", "09:55–10:45"),
                            ("P4", "10:45–11:35"), ("P5", "11:35–12:25"), ("P6", "13:10–14:00"),
                            ("P7", "14:00–14:50")]
            base = {"bg": "#172B46", "fg": TEXT, "bd": 0, "highlightthickness": 1, "highlightbackground": BORDER}
            tk.Label(table, text="PERIOD / TIME", font=self._font(10, "bold"), anchor="w", padx=self._px(10), pady=self._px(12), **base).grid(row=0, column=0, sticky="nsew")
            for col, day in enumerate(days, 1):
                table.grid_columnconfigure(col, weight=1, uniform="timetable_days")
                tk.Label(table, text=day.upper(), font=self._font(10, "bold"), anchor="center", padx=self._px(7), pady=self._px(12), **base).grid(row=0, column=col, sticky="nsew")
            cells = {}
            for record in scoped:
                _id, subj, _year, _dept, _class, day, start_time, end_time, period_name, _active = record
                match = re.search(r"P(\d+)(?:\s*[-–]\s*P?(\d+))?", period_name.upper())
                if not match or day not in days:
                    continue
                first = int(match.group(1)); last = int(match.group(2) or first)
                if not 1 <= first <= 7 or not first <= last <= 7:
                    continue
                cells[(day, first)] = (subj, start_time, end_time, last)
            for period_index, (period_label, default_time) in enumerate(period_times, 1):
                row = period_index
                table.grid_rowconfigure(row, weight=1)
                tk.Label(table, text=f"{period_label}\n{default_time}", font=self._font(9, "bold"),
                         fg=TEXT, bg="#172B46", justify="left", anchor="w", padx=self._px(10), pady=self._px(10),
                         highlightthickness=1, highlightbackground=BORDER).grid(row=row, column=0, sticky="nsew")
                for col, day in enumerate(days, 1):
                    entry = cells.get((day, period_index))
                    # A lab or project covering two consecutive periods occupies one tall cell.
                    if any(day == d and first < period_index <= last for (d, first), (_s, _st, _en, last) in cells.items()):
                        continue
                    if entry:
                        subj, start_time, end_time, last = entry
                        detail = f"{start_time}–{end_time}" if start_time and end_time else default_time
                        label = tk.Label(table, text=f"{subj}\n{detail}", font=self._font(10, "bold"),
                                         fg=TEXT, bg="#163A55", justify="center", padx=self._px(6), pady=self._px(10),
                                         wraplength=self._px(150), highlightthickness=1, highlightbackground=BORDER)
                        label.grid(row=row, column=col, rowspan=last - period_index + 1, sticky="nsew", padx=1, pady=1)
                    else:
                        tk.Label(table, text="—", font=self._font(10), fg=MUTED, bg="#0C192B",
                                 highlightthickness=1, highlightbackground=BORDER).grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
            if not scoped:
                ctk.CTkLabel(view_panel, text="No matching timetable. Adjust the filters or load a timetable preset.",
                             text_color=MUTED).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))
        ctk.CTkButton(filters, text="Search", width=90, command=refresh,
                      fg_color=BLUE, text_color="#04111F").grid(
                          row=0, column=5, padx=(0, 12), pady=12, sticky="e")
        subject_search.bind("<Return>", lambda _event: refresh())
        preset_choice = ctk.CTkOptionMenu(
            filters,
            values=["Choose timetable preset", "3rd Year CSE", "3rd Year Cyber Security", "3rd Year AI & DS"],
            width=210,
        )
        preset_choice.set("Choose timetable preset")
        preset_choice.grid(row=0, column=6, padx=(0, 6), pady=12)
        def load_selected_preset():
            loaders = {
                "3rd Year CSE": load_third_year_cse,
                "3rd Year Cyber Security": load_third_year_cyber_security,
                "3rd Year AI & DS": load_third_year_ai_ds,
            }
            loader = loaders.get(preset_choice.get())
            if loader:
                loader()
        ctk.CTkButton(filters, text="Load", width=74, command=load_selected_preset,
                      fg_color=BLUE, text_color="#04111F").grid(
                          row=0, column=7, padx=(0, 12), pady=12, sticky="e")
        def add():
            subj=subject.get().strip(); per=period.get().strip(); dept=department.get().strip().upper(); cls=class_name.get().strip().upper()
            if not subj or not per or not dept or not cls:
                messagebox.showwarning("Missing details","Enter subject, period, department and class/section."); return
            if (start.get().strip() and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",start.get().strip())) or (end.get().strip() and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",end.get().strip())):
                messagebox.showwarning("Invalid time","Use 24-hour HH:MM format, for example 09:30."); return
            with db() as conn:
                conn.execute("INSERT INTO timetable(subject,period,weekday,start_time,end_time,academic_year,department,class_name) VALUES(?,?,?,?,?,?,?,?)",(subj,per,weekday.get(),start.get().strip(),end.get().strip(),year.get(),dept,cls))
            self.log_activity("TIMETABLE_ADD",f"{subj} / {per} / {year.get()} / {dept} / {cls} / {weekday.get()}")
            refresh(); subject.delete(0,"end"); period.delete(0,"end"); department.delete(0,"end"); class_name.delete(0,"end"); start.delete(0,"end"); end.delete(0,"end")
        ctk.CTkButton(form,text="＋ Add Session",command=add,fg_color=BLUE,text_color="#04111F").grid(row=1,column=5,padx=8,pady=9,sticky="ew")
        refresh()

    def show_student_analytics(self, student_id=None, initial_tab=None):
        """Full student profile with attendance, academic and leave analysis."""
        self.clear_content()
        self.select_nav("Student Analytics")
        self.header("Student Full Analysis", "A complete attendance, academic and support profile for each student")
        # Keep the selector toolbar at the top; let the analysis body use spare height.
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        with db() as conn:
            students = conn.execute(
                "SELECT id,name,COALESCE(year,''),COALESCE(department,'') FROM students ORDER BY LOWER(name), id").fetchall()
        if not students:
            ctk.CTkLabel(
                self.content, text="Register students to view their full analysis.",
                text_color=MUTED, font=("Segoe UI", 14),
            ).grid(row=1, column=0, sticky="w", padx=12, pady=20)
            return

        student_options = {f"{sid}  •  {name}": sid for sid, name, _year, _department in students}
        student_meta = {sid: (year, department) for sid, _name, year, department in students}
        toolbar = ctk.CTkFrame(self.content, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        toolbar.grid_columnconfigure(0, weight=1)
        selected = ctk.CTkOptionMenu(
            toolbar, values=list(student_options), width=360, height=38,
            font=("Segoe UI", 13))
        selected.grid(row=0, column=1, sticky="w", padx=(8, 10))
        selected_option = next(
            (option for option, sid in student_options.items() if sid == student_id),
            next(iter(student_options)))
        selected.set(selected_option)
        student_search = ctk.CTkEntry(toolbar, height=38,
            placeholder_text="Search student ID or name…")
        student_search.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        year_filter = ctk.CTkOptionMenu(toolbar,
            values=["All Years"] + sorted({year for _sid, _name, year, _dept in students if year}),
            width=120)
        year_filter.grid(row=0, column=2, padx=4)
        department_filter = ctk.CTkOptionMenu(toolbar,
            values=["All Departments"] + sorted({dept for _sid, _name, _year, dept in students if dept}),
            width=140)
        department_filter.grid(row=0, column=3, padx=4)
        ctk.CTkButton(
            toolbar, text="＋ Add Assessment", height=38, fg_color=BLUE,
            text_color="#04111F", command=lambda: add_assessment()).grid(
                row=0, column=4, padx=5)
        ctk.CTkButton(
            toolbar, text="Export Student Report", height=38, fg_color="#1C3554",
            command=lambda: export_student_report()).grid(row=0, column=5, padx=(5, 0))

        def filter_student_choices(_event=None):
            query = student_search.get().strip().lower()
            choices = [label for label, sid in student_options.items()
                       if (not query or query in str(sid).lower() or query in label.lower())
                       and (year_filter.get() == "All Years" or student_meta.get(sid, ("", ""))[0] == year_filter.get())
                       and (department_filter.get() == "All Departments" or student_meta.get(sid, ("", ""))[1].upper() == department_filter.get().upper())]
            if not choices:
                selected.configure(values=["No matching students"])
                selected.set("No matching students")
                return
            selected.configure(values=choices)
            if selected.get() not in choices:
                selected.set(choices[0])
                refresh_analysis()

        student_search.bind("<KeyRelease>", filter_student_choices)
        year_filter.configure(command=lambda _value: filter_student_choices())
        department_filter.configure(command=lambda _value: filter_student_choices())

        overview = FastScrollFrame(self.content, fg_color="transparent", corner_radius=0)
        overview.grid(row=2, column=0, sticky="nsew")
        overview.grid_columnconfigure(0, weight=1)

        profile = self.white_panel(overview)
        profile.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        profile.grid_columnconfigure(0, weight=1)

        metrics = ctk.CTkFrame(overview, fg_color="transparent")
        metrics.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for column in range(4):
            metrics.grid_columnconfigure(column, weight=1, uniform="student_metrics")

        insight_panel = self.white_panel(overview)
        insight_panel.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(
            insight_panel, text="Analysis & Follow-up", font=("Segoe UI", 16, "bold"),
            text_color=TEXT).pack(anchor="w", padx=18, pady=(14, 6))
        insights = ctk.CTkFrame(insight_panel, fg_color="transparent")
        insights.pack(fill="x", padx=16, pady=(0, 14))

        tabs = ctk.CTkTabview(overview, height=470, fg_color=BG,
                              segmented_button_selected_color=BLUE,
                              segmented_button_selected_hover_color="#00A9CE")
        tabs.grid(row=3, column=0, sticky="nsew", pady=(0, 12))
        attendance_tab = tabs.add("Attendance")
        academics_tab = tabs.add("Academics")
        support_tab = tabs.add("Leave & Support")
        fees_tab = tabs.add("Fees")
        if initial_tab in {"Attendance", "Academics", "Leave & Support", "Fees"}:
            tabs.set(initial_tab)
        for tab in (attendance_tab, academics_tab, support_tab, fees_tab):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)

        attendance_area = FastScrollFrame(attendance_tab, fg_color="transparent")
        attendance_area.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        attendance_area.grid_columnconfigure(0, weight=1)
        academics_area = FastScrollFrame(academics_tab, fg_color="transparent")
        academics_area.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        academics_area.grid_columnconfigure(0, weight=1)
        support_area = FastScrollFrame(support_tab, fg_color="transparent")
        support_area.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        support_area.grid_columnconfigure(0, weight=1)
        fees_area = FastScrollFrame(fees_tab, fg_color="transparent")
        fees_area.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        fees_area.grid_columnconfigure(0, weight=1)

        def table_panel(parent, row, title, headings, weights, uniform):
            panel = self.white_panel(parent)
            panel.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            panel.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(panel, text=title, font=("Segoe UI", 14, "bold"),
                         text_color=TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(13, 8))
            table = tk.Frame(panel, bg=WHITE, bd=0, highlightthickness=0)
            table.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 12))
            self.table_header(table, headings, weights, uniform)
            return table

        attendance_subject_table = table_panel(
            attendance_area, 0, "Attendance by Subject / Session",
            ["SUBJECT", "MARKED", "PRESENT", "EXCUSED", "ATTENDANCE"],
            [3, 1, 1, 1, 1], "analytics_att_subject")
        attendance_trend_table = table_panel(
            attendance_area, 1, "Monthly Attendance Trend · Last 6 Months",
            ["MONTH", "MARKED SESSIONS", "PRESENT", "RATE"],
            [2, 2, 1, 1], "analytics_att_trend")
        attendance_history_table = table_panel(
            attendance_area, 2, "Recent Attendance Records · Latest 100",
            ["DATE", "TIME", "SUBJECT / SESSION", "STATUS"],
            [2, 1, 3, 1], "analytics_att_history")

        performance_subject_table = table_panel(
            academics_area, 0, "Subject Performance Summary",
            ["SUBJECT", "ASSESSMENTS", "AVERAGE SCORE", "LATEST"],
            [3, 1, 2, 2], "analytics_subject_marks")
        performance_records_table = table_panel(
            academics_area, 1, "Assessment History · Latest 100",
            ["SUBJECT", "ASSESSMENT", "SCORE", "RECORDED", "ACTIONS"],
            [2, 2, 1, 2, 2], "analytics_marks")
        assessment_limit_note = ctk.CTkLabel(
            academics_area, text="", text_color=MUTED, font=("Segoe UI", 10))
        assessment_limit_note.grid(row=2, column=0, sticky="w", pady=(0, 10))

        leave_summary = self.white_panel(support_area)
        leave_summary.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        leave_note = ctk.CTkLabel(leave_summary, text="", text_color=TEXT,
                                  justify="left", wraplength=900)
        leave_note.pack(anchor="w", padx=16, pady=14)
        leave_table = table_panel(
            support_area, 1, "Leave History",
            ["FROM", "TO", "TYPE", "REASON", "RECORDED BY"],
            [2, 2, 2, 4, 2], "analytics_leaves")

        fee_toolbar = ctk.CTkFrame(fees_area, fg_color="transparent")
        fee_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        fee_toolbar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            fee_toolbar, text="Fee amounts are entered by staff; balances are calculated from recorded payments.",
            text_color=MUTED, font=("Segoe UI", 11), wraplength=700,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            fee_toolbar, text="＋ Add Fee Item", height=36, fg_color=BLUE,
            text_color="#04111F", command=lambda: add_fee_item()).grid(row=0, column=1, padx=5)
        ctk.CTkButton(
            fee_toolbar, text="＋ Record Payment", height=36, fg_color="#1C3554",
            command=lambda: record_fee_payment()).grid(row=0, column=2, padx=(5, 0))

        fee_metrics = ctk.CTkFrame(fees_area, fg_color="transparent")
        fee_metrics.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for column in range(4):
            fee_metrics.grid_columnconfigure(column, weight=1, uniform="student_fee_metrics")
        fee_status_note = ctk.CTkLabel(
            fees_area, text="", text_color=MUTED, font=("Segoe UI", 11), wraplength=950)
        fee_status_note.grid(row=2, column=0, sticky="w", pady=(0, 8))
        fee_ledger_table = table_panel(
            fees_area, 3, "Fee Ledger",
            ["ACADEMIC YEAR", "FEE ITEM", "BILLED", "PAID", "BALANCE", "DUE DATE", "STATUS", "ACTIONS"],
            [2, 2, 1, 1, 1, 2, 1, 2], "analytics_fee_ledger")
        fee_payment_table = table_panel(
            fees_area, 4, "Payment History · Latest 100",
            ["PAID ON", "FEE ITEM", "AMOUNT", "METHOD", "REFERENCE"],
            [2, 3, 1, 2, 2], "analytics_fee_payments")

        current_data = {"student_id": None}

        def format_inr(amount):
            return f"₹{float(amount or 0):,.2f}"

        def add_insight(text, color=TEXT):
            ctk.CTkLabel(
                insights, text="•  " + text, text_color=color,
                font=("Segoe UI", 12), justify="left", wraplength=1100,
            ).pack(anchor="w", padx=4, pady=3)

        def refresh_analysis():
            student_id = student_options.get(selected.get())
            if student_id is None:
                return
            threshold = self.attendance_threshold()
            with db() as conn:
                student = conn.execute(
                    "SELECT id,name,COALESCE(year,''),COALESCE(department,''),"
                    "COALESCE(class_name,''),COALESCE(email,''),COALESCE(parent_email,'') "
                    "FROM students WHERE id=?", (student_id,)).fetchone()
                attendance_summary = conn.execute(f"""
                    SELECT
                        SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN a.status='Absent' AND NOT {EXCUSED_SQL} THEN 1 ELSE 0 END),
                        SUM(CASE WHEN {EXCUSED_SQL} THEN 1 ELSE 0 END),
                        SUM(CASE WHEN a.status NOT IN ('Present','Absent') THEN 1 ELSE 0 END),
                        MAX(a.date)
                    FROM attendance a WHERE a.student_id=?
                """, (student_id,)).fetchone()
                attendance_subjects = conn.execute(f"""
                    SELECT COALESCE(t.subject,'Daily'),
                           SUM(CASE WHEN a.status IN ('Present','Absent')
                                    AND NOT {EXCUSED_SQL} THEN 1 ELSE 0 END),
                           SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN {EXCUSED_SQL} THEN 1 ELSE 0 END)
                    FROM attendance a LEFT JOIN timetable t ON t.id=a.session_id
                    WHERE a.student_id=? GROUP BY COALESCE(t.subject,'Daily')
                    ORDER BY COALESCE(t.subject,'Daily')
                """, (student_id,)).fetchall()
                attendance_trend = conn.execute(f"""
                    SELECT substr(a.date,1,7), COUNT(*),
                           SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END)
                    FROM attendance a
                    WHERE a.student_id=? AND a.date>=date('now','-5 months')
                      AND a.status IN ('Present','Absent') AND NOT {EXCUSED_SQL}
                    GROUP BY substr(a.date,1,7) ORDER BY substr(a.date,1,7)
                """, (student_id,)).fetchall()
                attendance_history = conn.execute(f"""
                    SELECT a.date,a.time,COALESCE(t.subject,'Daily'),
                           CASE WHEN {EXCUSED_SQL} THEN 'Excused' ELSE a.status END
                    FROM attendance a LEFT JOIN timetable t ON t.id=a.session_id
                    WHERE a.student_id=? ORDER BY a.date DESC,a.time DESC,a.id DESC LIMIT 100
                """, (student_id,)).fetchall()
                performance_summary = conn.execute("""
                    SELECT subject,COUNT(*),AVG(CASE WHEN max_marks>0
                           THEN marks*100.0/max_marks END),MAX(recorded_on)
                    FROM performance_records WHERE student_id=? GROUP BY subject
                    ORDER BY subject
                """, (student_id,)).fetchall()
                performance_records = conn.execute("""
                    SELECT id,subject,assessment,marks,max_marks,recorded_on
                    FROM performance_records WHERE student_id=?
                    ORDER BY recorded_on DESC,id DESC LIMIT 100
                """, (student_id,)).fetchall()
                performance_count = conn.execute(
                    "SELECT COUNT(*) FROM performance_records WHERE student_id=?",
                    (student_id,)).fetchone()[0]
                leaves = conn.execute("""
                    SELECT start_date,end_date,leave_type,COALESCE(reason,''),
                           COALESCE(created_by,'')
                    FROM leaves WHERE student_id=? ORDER BY start_date DESC,id DESC LIMIT 100
                """, (student_id,)).fetchall()
                leave_count = conn.execute(
                    "SELECT COUNT(*) FROM leaves WHERE student_id=?", (student_id,)
                ).fetchone()[0]
                fee_accounts = conn.execute("""
                    SELECT fa.id,fa.academic_year,fa.fee_type,fa.total_amount,fa.due_date,
                           COALESCE(fa.notes,''),COALESCE(SUM(fp.amount),0)
                    FROM student_fee_accounts fa
                    LEFT JOIN student_fee_payments fp ON fp.fee_account_id=fa.id
                    WHERE fa.student_id=?
                    GROUP BY fa.id
                    ORDER BY fa.academic_year DESC,fa.due_date,fa.id DESC
                """, (student_id,)).fetchall()
                fee_payments = conn.execute("""
                    SELECT fp.paid_on,fa.fee_type,fp.amount,fp.method,COALESCE(fp.reference,'')
                    FROM student_fee_payments fp
                    JOIN student_fee_accounts fa ON fa.id=fp.fee_account_id
                    WHERE fa.student_id=?
                    ORDER BY fp.paid_on DESC,fp.id DESC LIMIT 100
                """, (student_id,)).fetchall()

            if not student:
                messagebox.showwarning("Student unavailable", "This student record no longer exists.", parent=self)
                return
            current_data.update({
                "student_id": student_id,
                "profile": student,
                "attendance_summary": attendance_summary,
                "attendance_subjects": attendance_subjects,
                "attendance_trend": attendance_trend,
                "performance_summary": performance_summary,
                "performance_count": performance_count,
                "leave_count": leave_count,
                "fee_accounts": fee_accounts,
                "fee_payments": fee_payments,
            })
            sid, name, year, department, class_name, email, parent_email = student
            present, absent, excused, other, last_attendance = attendance_summary
            present, absent, excused, other = (value or 0 for value in (present, absent, excused, other))
            marked = present + absent
            attendance_rate = present / marked * 100 if marked else None
            scored_subjects = [row for row in performance_summary if row[2] is not None]
            scored_assessments = sum(row[1] for row in scored_subjects)
            average_score = (
                sum(row[2] * row[1] for row in scored_subjects) / scored_assessments
                if scored_assessments else None)
            total_leaves = leave_count
            current_data["attendance_rate"] = attendance_rate
            current_data["marked_sessions"] = marked
            current_data["present"] = present
            current_data["absent"] = absent
            current_data["excused"] = excused
            current_data["average_score"] = average_score
            fee_total = sum(float(row[3] or 0) for row in fee_accounts)
            fee_paid = sum(float(row[6] or 0) for row in fee_accounts)
            fee_balance = sum(max(0.0, float(row[3] or 0) - float(row[6] or 0))
                              for row in fee_accounts)
            overdue_accounts = [
                row for row in fee_accounts
                if row[4] and row[4] < date.today().isoformat()
                and float(row[3] or 0) - float(row[6] or 0) > 0.005
            ]
            overdue_balance = sum(
                max(0.0, float(row[3] or 0) - float(row[6] or 0))
                for row in overdue_accounts)
            current_data.update({
                "fee_total": fee_total,
                "fee_paid": fee_paid,
                "fee_balance": fee_balance,
                "overdue_balance": overdue_balance,
                "overdue_count": len(overdue_accounts),
            })

            self.clear_children(profile)
            profile.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(profile, text=name, font=("Segoe UI", 21, "bold"),
                         text_color=TEXT).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 3))
            details = (f"Student ID {sid}   •   {year or 'Year not set'}   •   "
                       f"{department or 'Department not set'}   •   "
                       f"{class_name or 'Class not set'}")
            ctk.CTkLabel(profile, text=details, text_color=MUTED,
                         font=("Segoe UI", 12)).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 4))
            contact = "   •   ".join(value for value in (
                f"Student: {email}" if email else "",
                f"Parent/Guardian: {parent_email}" if parent_email else "") if value)
            ctk.CTkLabel(profile, text=contact or "No contact emails recorded",
                         text_color=MUTED, font=("Segoe UI", 11),
                         wraplength=1100).grid(row=2, column=0, sticky="w", padx=18, pady=(0, 14))

            self.clear_children(metrics)
            cards = [
                ("Attendance Rate", f"{attendance_rate:.1f}%" if attendance_rate is not None else "—",
                 f"Threshold {threshold:.0f}%", GREEN if attendance_rate is not None and attendance_rate >= threshold else AMBER),
                ("Marked Sessions", marked, f"{present} present · {absent} absent", BLUE),
                ("Excused Absences", excused, "Excluded from attendance rate", AMBER),
                ("Average Assessment", f"{average_score:.1f}%" if average_score is not None else "—",
                 f"{performance_count} recorded assessment(s)", GREEN if average_score is not None and average_score >= 75 else AMBER),
            ]
            for index, card in enumerate(cards):
                self.card(metrics, *card).grid(row=0, column=index, sticky="ew",
                                               padx=(0 if index == 0 else 6, 6 if index < 3 else 0))

            self.clear_children(insights)
            if attendance_rate is None:
                add_insight("No marked attendance sessions are recorded yet.", AMBER)
            elif attendance_rate < threshold:
                add_insight(f"Attendance is {threshold - attendance_rate:.1f} points below the {threshold:.0f}% requirement; review recent absences and follow up.", RED)
            else:
                add_insight(f"Attendance meets the configured {threshold:.0f}% requirement.", GREEN)
            if average_score is None:
                add_insight("No assessment marks are recorded yet.", AMBER)
            else:
                best_subject = max((row for row in performance_summary if row[2] is not None), key=lambda row: row[2], default=None)
                lowest_subject = min((row for row in performance_summary if row[2] is not None), key=lambda row: row[2], default=None)
                if best_subject:
                    add_insight(f"Strongest subject average: {best_subject[0]} at {best_subject[2]:.1f}% across {best_subject[1]} assessment(s).", GREEN)
                if lowest_subject and lowest_subject[0] != (best_subject[0] if best_subject else None):
                    add_insight(f"Subject to review: {lowest_subject[0]} at {lowest_subject[2]:.1f}% across {lowest_subject[1]} assessment(s).", AMBER)
            add_insight(f"Latest attendance record: {last_attendance or 'none'} · {total_leaves} leave request(s) · {other} unmarked/other attendance record(s).", MUTED)

            self.clear_children(fee_metrics)
            fee_cards = [
                ("Total Billed", format_inr(fee_total), f"{len(fee_accounts)} fee item(s)", BLUE),
                ("Paid", format_inr(fee_paid), f"{len(fee_payments)} payment(s) on file", GREEN),
                ("Outstanding", format_inr(fee_balance), "Remaining across all fee items", AMBER if fee_balance else GREEN),
                ("Past Due", format_inr(overdue_balance), f"{len(overdue_accounts)} overdue item(s)", RED if overdue_balance else GREEN),
            ]
            for index, card in enumerate(fee_cards):
                self.card(fee_metrics, *card).grid(
                    row=0, column=index, sticky="ew",
                    padx=(0 if index == 0 else 6, 6 if index < 3 else 0))
            fee_status_note.configure(
                text=("No fee items are recorded for this student yet. Add a fee item to start tracking balances."
                      if not fee_accounts else
                      "Balances equal billed amounts minus recorded payments. Overdue status uses the due date and current date."),
                text_color=MUTED if not overdue_balance else RED)

            for table in (attendance_subject_table, attendance_trend_table,
                          attendance_history_table, performance_subject_table,
                          performance_records_table, leave_table, fee_ledger_table,
                          fee_payment_table):
                table._fill_token = object()
                self.clear_children(table)
            for table, headings, weights, uniform in (
                (attendance_subject_table, ["SUBJECT", "MARKED", "PRESENT", "EXCUSED", "ATTENDANCE"], [3,1,1,1,1], "analytics_att_subject"),
                (attendance_trend_table, ["MONTH", "MARKED SESSIONS", "PRESENT", "RATE"], [2,2,1,1], "analytics_att_trend"),
                (attendance_history_table, ["DATE", "TIME", "SUBJECT / SESSION", "STATUS"], [2,1,3,1], "analytics_att_history"),
                (performance_subject_table, ["SUBJECT", "ASSESSMENTS", "AVERAGE SCORE", "LATEST"], [3,1,2,2], "analytics_subject_marks"),
                (performance_records_table, ["SUBJECT", "ASSESSMENT", "SCORE", "RECORDED", "ACTIONS"], [2,2,1,2,2], "analytics_marks"),
                (leave_table, ["FROM", "TO", "TYPE", "REASON", "RECORDED BY"], [2,2,2,4,2], "analytics_leaves"),
                (fee_ledger_table, ["ACADEMIC YEAR", "FEE ITEM", "BILLED", "PAID", "BALANCE", "DUE DATE", "STATUS", "ACTIONS"], [2,2,1,1,1,2,1,2], "analytics_fee_ledger"),
                (fee_payment_table, ["PAID ON", "FEE ITEM", "AMOUNT", "METHOD", "REFERENCE"], [2,3,1,2,2], "analytics_fee_payments"),
            ):
                self.table_header(table, headings, weights, uniform)

            def add_rows(table, rows, build, empty):
                if rows:
                    self.fill_batched(table, rows, build)
                else:
                    self.empty_message(table, empty)

            def build_att_subject(index, row):
                subject_name, total, subject_present, subject_excused = row
                total, subject_present, subject_excused = total or 0, subject_present or 0, subject_excused or 0
                rate = f"{subject_present / total * 100:.1f}%" if total else "—"
                self.table_row(attendance_subject_table, index,
                               [subject_name, total, subject_present, subject_excused, rate],
                               [3,1,1,1,1], "analytics_att_subject", shade=False)
            add_rows(attendance_subject_table, attendance_subjects, build_att_subject,
                     "No attendance by subject is available.")

            def build_trend(index, row):
                month, total, month_present = row
                rate = month_present / total * 100 if total else 0
                self.table_row(attendance_trend_table, index,
                               [month, total, month_present, f"{rate:.1f}%"],
                               [2,2,1,1], "analytics_att_trend", shade=False)
            add_rows(attendance_trend_table, attendance_trend, build_trend,
                     "No marked attendance in the last six months.")

            def build_att_history(index, row):
                day, stamp, subject_name, status = row
                color = GREEN if status == "Present" else AMBER if status == "Excused" else RED if status == "Absent" else MUTED
                self.table_row(attendance_history_table, index,
                               [day, stamp, subject_name, status],
                               [2,1,3,1], "analytics_att_history", colors={3: color}, shade=False)
            add_rows(attendance_history_table, attendance_history, build_att_history,
                     "No attendance history is available.")

            def build_subject_marks(index, row):
                subject_name, count, average, latest = row
                self.table_row(performance_subject_table, index,
                               [subject_name, count, f"{average:.1f}%" if average is not None else "—", latest or "—"],
                               [3,1,2,2], "analytics_subject_marks", shade=False)
            add_rows(performance_subject_table, performance_summary, build_subject_marks,
                     "No academic subjects have assessment records.")

            def build_assessment(index, row):
                record_id, subject_name, assessment_name, marks_value, max_marks, recorded_on = row
                score = f"{marks_value:g}/{max_marks:g} ({marks_value / max_marks * 100:.1f}%)" if max_marks else f"{marks_value:g}/—"
                table_row = self.table_row(
                    performance_records_table, index,
                    [subject_name, assessment_name or "Assessment", score, recorded_on, None],
                    [2,2,1,2,2], "analytics_marks", shade=False)
                actions = tk.Frame(table_row, bg=table_row.cget("bg"), bd=0, highlightthickness=0)
                actions.grid(row=0, column=4, sticky="w", padx=self._px(4), pady=self._px(3))
                self.mini_button(actions, "Edit", "#203754", "#2A4668", TEXT,
                                 lambda rec=row: edit_assessment(rec)).pack(side="left", padx=2)
                self.mini_button(actions, "Delete", "#4B2032", "#64263C", RED,
                                 lambda rid=record_id: delete_assessment(rid)).pack(side="left", padx=2)
            add_rows(performance_records_table, performance_records, build_assessment,
                     "No assessment history is recorded.")
            assessment_limit_note.configure(
                text=(f"Showing the latest {len(performance_records)} of {performance_count} assessments."
                      if performance_count > len(performance_records) else ""))

            leave_note.configure(
                text=(f"{leave_count} leave request(s) on file. {excused} absence(s) are currently excluded from the attendance rate when covered by approved leave dates. "
                      f"The attendance rate uses {marked} marked sessions and excludes excused absences."))

            def build_leave(index, row):
                start_day, end_day, leave_type, reason, created_by = row
                self.table_row(leave_table, index,
                               [start_day, end_day, leave_type, reason or "—", created_by or "—"],
                               [2,2,2,4,2], "analytics_leaves", shade=False)
            add_rows(leave_table, leaves, build_leave, "No leave history is recorded.")

            def build_fee_account(index, row):
                account_id, academic_year, fee_type, total_amount, due_date, notes, paid_amount = row
                total_amount, paid_amount = float(total_amount or 0), float(paid_amount or 0)
                balance = max(0.0, total_amount - paid_amount)
                if balance <= 0.005:
                    status, status_color = "Paid", GREEN
                elif due_date and due_date < date.today().isoformat():
                    status, status_color = "Overdue", RED
                elif paid_amount > 0:
                    status, status_color = "Part-paid", AMBER
                else:
                    status, status_color = "Unpaid", AMBER
                fee_row = self.table_row(
                    fee_ledger_table, index,
                    [academic_year, fee_type, format_inr(total_amount), format_inr(paid_amount),
                     format_inr(balance), due_date or "—", status, None],
                    [2,2,1,1,1,2,1,2], "analytics_fee_ledger",
                    colors={6: status_color}, shade=False)
                if balance > 0.005:
                    actions = tk.Frame(fee_row, bg=fee_row.cget("bg"), bd=0, highlightthickness=0)
                    actions.grid(row=0, column=7, sticky="w", padx=self._px(4), pady=self._px(3))
                    self.mini_button(
                        actions, "Record Payment", "#123F45", "#15525A", GREEN,
                        lambda aid=account_id, remaining=balance, label=fee_type:
                            record_fee_payment(aid, remaining, label),
                    ).pack(side="left", padx=2)
            add_rows(fee_ledger_table, fee_accounts, build_fee_account,
                     "No fee items are recorded for this student.")

            def build_fee_payment(index, row):
                paid_on, fee_type, amount, method, reference = row
                self.table_row(fee_payment_table, index,
                               [paid_on, fee_type, format_inr(amount), method, reference or "—"],
                               [2,3,1,2,2], "analytics_fee_payments", shade=False)
            add_rows(fee_payment_table, fee_payments, build_fee_payment,
                     "No payments are recorded for this student.")

        def add_fee_item():
            student_id = current_data["student_id"]
            dialog = ctk.CTkToplevel(self)
            dialog.title("Add Fee Item")
            dialog.geometry("460x500")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Add Fee Item", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 6))
            ctk.CTkLabel(dialog, text=f"For {selected.get()}", text_color=MUTED).pack(anchor="w", padx=24, pady=(0, 12))
            fields = {}
            for key, placeholder, value in (
                ("fee_type", "Fee item (e.g. Tuition, Exam, Transport)", ""),
                ("academic_year", "Academic year (e.g. 2026-27)", f"{date.today().year}-{str(date.today().year + 1)[-2:]}") ,
                ("amount", "Total amount in INR", ""),
                ("due_date", "Due date (YYYY-MM-DD, optional)", ""),
            ):
                entry = ctk.CTkEntry(dialog, height=38, placeholder_text=placeholder)
                if value:
                    entry.insert(0, value)
                entry.pack(fill="x", padx=24, pady=6)
                fields[key] = entry
            notes = ctk.CTkEntry(dialog, height=38, placeholder_text="Notes (optional)")
            notes.pack(fill="x", padx=24, pady=6)

            def save_fee_item():
                fee_type = fields["fee_type"].get().strip()
                academic_year = fields["academic_year"].get().strip()
                try:
                    amount = float(fields["amount"].get().replace(",", "").replace("₹", "").strip())
                    due_date = fields["due_date"].get().strip()
                    if due_date:
                        due_date = datetime.strptime(due_date, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Invalid fee item", "Enter a positive amount and a valid due date.", parent=dialog)
                    return
                if not fee_type or not academic_year or amount <= 0:
                    messagebox.showerror("Invalid fee item", "Enter a fee item, academic year and positive amount.", parent=dialog)
                    return
                with db() as conn:
                    conn.execute(
                        "INSERT INTO student_fee_accounts(student_id,academic_year,fee_type,total_amount,due_date,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                        (student_id, academic_year, fee_type, amount, due_date,
                         notes.get().strip(), datetime.now().isoformat(timespec="seconds")))
                self.log_activity("FEE_ITEM_ADDED", f"Student {student_id}: {fee_type} {amount:.2f} INR for {academic_year}")
                dialog.destroy()
                refresh_analysis()
            ctk.CTkButton(dialog, text="Save Fee Item", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save_fee_item).pack(padx=24, pady=18, anchor="e")

        def record_fee_payment(account_id=None, balance=None, fee_type=None):
            accounts = [row for row in current_data.get("fee_accounts", [])
                        if float(row[3] or 0) - float(row[6] or 0) > 0.005]
            if account_id is not None:
                accounts = [row for row in accounts if row[0] == account_id]
            if not accounts:
                messagebox.showinfo("No balance due", "This student has no outstanding fee items.", parent=self)
                return
            account_options = {f"{row[1]} · {row[2]} · Balance {format_inr(float(row[3]) - float(row[6] or 0))}": row
                               for row in accounts}
            dialog = ctk.CTkToplevel(self)
            dialog.title("Record Fee Payment")
            dialog.geometry("460x480")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Record Fee Payment", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 8))
            account_menu = ctk.CTkOptionMenu(dialog, values=list(account_options), width=400)
            account_menu.pack(fill="x", padx=24, pady=8)
            first_option = next(iter(account_options))
            account_menu.set(first_option)
            amount_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Payment amount in INR")
            amount_entry.pack(fill="x", padx=24, pady=8)
            amount_entry.insert(0, f"{float(account_options[first_option][3]) - float(account_options[first_option][6] or 0):.2f}")
            paid_on = ctk.CTkEntry(dialog, height=38, placeholder_text="Payment date (YYYY-MM-DD)")
            paid_on.pack(fill="x", padx=24, pady=8)
            paid_on.insert(0, date.today().isoformat())
            method = ctk.CTkOptionMenu(dialog, values=["Cash", "UPI", "Bank transfer", "Card", "Cheque", "Other"])
            method.pack(fill="x", padx=24, pady=8)
            reference = ctk.CTkEntry(dialog, height=38, placeholder_text="Receipt / transaction reference (optional)")
            reference.pack(fill="x", padx=24, pady=8)

            def update_amount_default(_value=None):
                row = account_options[account_menu.get()]
                amount_entry.delete(0, "end")
                amount_entry.insert(0, f"{float(row[3]) - float(row[6] or 0):.2f}")
            account_menu.configure(command=update_amount_default)

            def save_payment():
                account = account_options.get(account_menu.get())
                try:
                    amount = float(amount_entry.get().replace(",", "").replace("₹", "").strip())
                    payment_date = datetime.strptime(paid_on.get().strip(), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Invalid payment", "Enter a positive amount and valid payment date.", parent=dialog)
                    return
                remaining = float(account[3]) - float(account[6] or 0)
                if amount <= 0 or amount > remaining + 0.005:
                    messagebox.showerror("Invalid payment", f"Payment must be greater than zero and no more than the remaining {format_inr(remaining)}.", parent=dialog)
                    return
                with db() as conn:
                    conn.execute(
                        "INSERT INTO student_fee_payments(fee_account_id,amount,paid_on,method,reference,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                        (account[0], amount, payment_date, method.get(), reference.get().strip(),
                         self.current_username(), datetime.now().isoformat(timespec="seconds")))
                self.log_activity("FEE_PAYMENT_RECORDED", f"Student {current_data['student_id']}: {amount:.2f} INR for {account[2]}")
                dialog.destroy()
                refresh_analysis()
            ctk.CTkButton(dialog, text="Save Payment", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save_payment).pack(padx=24, pady=18, anchor="e")

        def add_assessment():
            student_id = current_data["student_id"]
            dialog = ctk.CTkToplevel(self)
            dialog.title("Add Assessment")
            dialog.geometry("430x420")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Record Assessment", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 6))
            ctk.CTkLabel(dialog, text=f"For {selected.get()}", text_color=MUTED).pack(anchor="w", padx=24, pady=(0, 12))
            fields = {}
            for key, placeholder in (("subject", "Subject"), ("assessment", "Assessment name"),
                                     ("marks", "Marks achieved"), ("maximum", "Maximum marks")):
                entry = ctk.CTkEntry(dialog, height=38, placeholder_text=placeholder)
                entry.pack(fill="x", padx=24, pady=6)
                fields[key] = entry
            fields["assessment"].insert(0, "Assessment")
            date_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Date (YYYY-MM-DD)")
            date_entry.insert(0, date.today().isoformat())
            date_entry.pack(fill="x", padx=24, pady=6)

            def save_assessment():
                subject_name = fields["subject"].get().strip()
                assessment_name = fields["assessment"].get().strip() or "Assessment"
                try:
                    marks_value = float(fields["marks"].get())
                    max_value = float(fields["maximum"].get())
                    date_value = datetime.strptime(date_entry.get().strip(), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Invalid assessment", "Enter numeric marks, a valid date and a maximum score.", parent=dialog)
                    return
                if not subject_name or max_value <= 0 or marks_value < 0 or marks_value > max_value:
                    messagebox.showerror("Invalid assessment", "Subject is required and marks must be between zero and the maximum.", parent=dialog)
                    return
                with db() as conn:
                    conn.execute(
                        "INSERT INTO performance_records(student_id,subject,assessment,marks,max_marks,recorded_on) VALUES(?,?,?,?,?,?)",
                        (student_id, subject_name, assessment_name, marks_value, max_value, date_value))
                self.log_activity("PERFORMANCE_ENTRY", f"Student {student_id}: {subject_name} {marks_value}/{max_value}")
                dialog.destroy()
                refresh_analysis()
            ctk.CTkButton(dialog, text="Save Assessment", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save_assessment).pack(padx=24, pady=18, anchor="e")

        def edit_assessment(record):
            record_id, subject_name, assessment_name, marks_value, max_value, recorded_on = record
            dialog = ctk.CTkToplevel(self)
            dialog.title("Edit Assessment")
            dialog.geometry("430x420")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Edit Assessment", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 8))
            fields = {}
            for key, placeholder, value in (
                ("subject", "Subject", subject_name),
                ("assessment", "Assessment name", assessment_name),
                ("marks", "Marks achieved", str(marks_value)),
                ("maximum", "Maximum marks", str(max_value)),
                ("date", "Date (YYYY-MM-DD)", recorded_on),
            ):
                entry = ctk.CTkEntry(dialog, height=38, placeholder_text=placeholder)
                entry.insert(0, value or "")
                entry.pack(fill="x", padx=24, pady=6)
                fields[key] = entry

            def save_edit():
                subject_value = fields["subject"].get().strip()
                assessment_value = fields["assessment"].get().strip() or "Assessment"
                try:
                    mark_value = float(fields["marks"].get())
                    maximum_value = float(fields["maximum"].get())
                    date_value = datetime.strptime(fields["date"].get().strip(), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Invalid assessment", "Enter valid marks, maximum and date.", parent=dialog)
                    return
                if not subject_value or maximum_value <= 0 or mark_value < 0 or mark_value > maximum_value:
                    messagebox.showerror("Invalid assessment", "Marks must be between zero and the maximum.", parent=dialog)
                    return
                with db() as conn:
                    conn.execute(
                        "UPDATE performance_records SET subject=?,assessment=?,marks=?,max_marks=?,recorded_on=? WHERE id=? AND student_id=?",
                        (subject_value, assessment_value, mark_value, maximum_value,
                         date_value, record_id, current_data["student_id"]))
                self.log_activity("PERFORMANCE_EDIT", f"Updated assessment {record_id} for student {current_data['student_id']}")
                dialog.destroy()
                refresh_analysis()
            ctk.CTkButton(dialog, text="Save Changes", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save_edit).pack(padx=24, pady=18, anchor="e")

        def delete_assessment(record_id):
            if not messagebox.askyesno(
                "Delete Assessment", "Delete this assessment record?", parent=self):
                return
            with db() as conn:
                conn.execute("DELETE FROM performance_records WHERE id=? AND student_id=?",
                             (record_id, current_data["student_id"]))
            self.log_activity("PERFORMANCE_DELETE", f"Deleted assessment {record_id} for student {current_data['student_id']}")
            refresh_analysis()

        def export_student_report():
            student_id = current_data["student_id"]
            if student_id is None:
                return
            filename = filedialog.asksaveasfilename(
                title="Export Student Analysis", defaultextension=".csv",
                filetypes=[("CSV file", "*.csv")],
                initialfile=f"student_analysis_{student_id}_{date.today().isoformat()}.csv")
            if not filename:
                return
            try:
                with db() as conn:
                    student = conn.execute(
                        "SELECT id,name,COALESCE(year,''),COALESCE(department,''),COALESCE(class_name,''),COALESCE(email,''),COALESCE(parent_email,'') FROM students WHERE id=?",
                        (student_id,)).fetchone()
                    attendance_rows = conn.execute(f"""
                        SELECT a.date,a.time,COALESCE(t.subject,'Daily'),
                               CASE WHEN {EXCUSED_SQL} THEN 'Excused' ELSE a.status END
                        FROM attendance a LEFT JOIN timetable t ON t.id=a.session_id
                        WHERE a.student_id=? ORDER BY a.date DESC,a.time DESC,a.id DESC
                    """, (student_id,)).fetchall()
                    marks_rows = conn.execute(
                        "SELECT subject,assessment,marks,max_marks,recorded_on FROM performance_records WHERE student_id=? ORDER BY recorded_on DESC,id DESC",
                        (student_id,)).fetchall()
                    leave_rows = conn.execute(
                        "SELECT start_date,end_date,leave_type,reason,created_by FROM leaves WHERE student_id=? ORDER BY start_date DESC,id DESC",
                        (student_id,)).fetchall()
                    fee_rows = conn.execute("""
                        SELECT fa.academic_year,fa.fee_type,fa.total_amount,
                               COALESCE(SUM(fp.amount),0),fa.due_date,COALESCE(fa.notes,'')
                        FROM student_fee_accounts fa
                        LEFT JOIN student_fee_payments fp ON fp.fee_account_id=fa.id
                        WHERE fa.student_id=? GROUP BY fa.id
                        ORDER BY fa.academic_year DESC,fa.due_date,fa.id DESC
                    """, (student_id,)).fetchall()
                    payment_rows = conn.execute("""
                        SELECT fp.paid_on,fa.academic_year,fa.fee_type,fp.amount,
                               fp.method,COALESCE(fp.reference,''),COALESCE(fp.recorded_by,'')
                        FROM student_fee_payments fp
                        JOIN student_fee_accounts fa ON fa.id=fp.fee_account_id
                        WHERE fa.student_id=? ORDER BY fp.paid_on DESC,fp.id DESC
                    """, (student_id,)).fetchall()
                with open(filename, "w", newline="", encoding="utf-8-sig") as output:
                    writer = csv.writer(output)
                    writer.writerow(["STUDENT PROFILE"])
                    writer.writerow(["ID", "Name", "Year", "Department", "Class", "Email", "Parent Email"])
                    writer.writerow(list(student))
                    writer.writerow([])
                    writer.writerow(["ANALYSIS SUMMARY"])
                    writer.writerow(["Attendance Rate", f"{current_data['attendance_rate']:.1f}%" if current_data["attendance_rate"] is not None else "N/A"])
                    writer.writerow(["Marked Sessions", current_data["marked_sessions"]])
                    writer.writerow(["Present", current_data["present"]])
                    writer.writerow(["Unexcused Absences", current_data["absent"]])
                    writer.writerow(["Excused Absences", current_data["excused"]])
                    writer.writerow(["Average Assessment Score", f"{current_data['average_score']:.1f}%" if current_data["average_score"] is not None else "N/A"])
                    writer.writerow(["Assessment Count", current_data["performance_count"]])
                    writer.writerow(["Leave Requests", current_data["leave_count"]])
                    writer.writerow(["Total Fees Billed (INR)", f"{current_data.get('fee_total', 0):.2f}"])
                    writer.writerow(["Fees Paid (INR)", f"{current_data.get('fee_paid', 0):.2f}"])
                    writer.writerow(["Outstanding Fees (INR)", f"{current_data.get('fee_balance', 0):.2f}"])
                    writer.writerow(["Past-Due Fees (INR)", f"{current_data.get('overdue_balance', 0):.2f}"])
                    writer.writerow([])
                    writer.writerow(["ATTENDANCE BY SUBJECT"])
                    writer.writerow(["Subject", "Marked", "Present", "Excused", "Attendance Rate"])
                    for subject_name, total, subject_present, subject_excused in current_data["attendance_subjects"]:
                        total, subject_present = total or 0, subject_present or 0
                        writer.writerow([subject_name, total, subject_present, subject_excused or 0,
                                         f"{subject_present / total * 100:.1f}%" if total else "N/A"])
                    writer.writerow([])
                    writer.writerow(["MONTHLY ATTENDANCE TREND"])
                    writer.writerow(["Month", "Marked Sessions", "Present", "Rate"])
                    for month, total, month_present in current_data["attendance_trend"]:
                        writer.writerow([month, total, month_present,
                                         f"{month_present / total * 100:.1f}%" if total else "N/A"])
                    writer.writerow([])
                    writer.writerow(["SUBJECT PERFORMANCE SUMMARY"])
                    writer.writerow(["Subject", "Assessments", "Average Score", "Latest Assessment"])
                    for subject_name, count, average, latest in current_data["performance_summary"]:
                        writer.writerow([subject_name, count,
                                         f"{average:.1f}%" if average is not None else "N/A", latest or ""])
                    writer.writerow([])
                    writer.writerow(["ATTENDANCE HISTORY"])
                    writer.writerow(["Date", "Time", "Subject / Session", "Status"])
                    writer.writerows(attendance_rows)
                    writer.writerow([])
                    writer.writerow(["ASSESSMENT HISTORY"])
                    writer.writerow(["Subject", "Assessment", "Marks", "Maximum", "Date"])
                    writer.writerows(marks_rows)
                    writer.writerow([])
                    writer.writerow(["LEAVE HISTORY"])
                    writer.writerow(["Start", "End", "Type", "Reason", "Recorded By"])
                    writer.writerows(leave_rows)
                    writer.writerow([])
                    writer.writerow(["FEE LEDGER (INR)"])
                    writer.writerow(["Academic Year", "Fee Item", "Billed", "Paid", "Balance", "Due Date", "Status", "Notes"])
                    for academic_year, fee_type, billed, paid, due_date, notes in fee_rows:
                        balance = max(0.0, float(billed or 0) - float(paid or 0))
                        status = ("Paid" if balance <= 0.005 else
                                  "Overdue" if due_date and due_date < date.today().isoformat() else
                                  "Part-paid" if float(paid or 0) > 0 else "Unpaid")
                        writer.writerow([academic_year, fee_type, f"{float(billed or 0):.2f}",
                                         f"{float(paid or 0):.2f}", f"{balance:.2f}",
                                         due_date or "", status, notes])
                    writer.writerow([])
                    writer.writerow(["FEE PAYMENT HISTORY (INR)"])
                    writer.writerow(["Paid On", "Academic Year", "Fee Item", "Amount", "Method", "Reference", "Recorded By"])
                    writer.writerows(payment_rows)
                self.log_activity("STUDENT_ANALYSIS_EXPORT", f"Exported student analysis for ID {student_id}")
                messagebox.showinfo("Export complete", f"Student analysis saved to:\n{filename}", parent=self)
            except (OSError, sqlite3.Error) as error:
                messagebox.showerror("Export failed", str(error), parent=self)

        selected.configure(command=lambda _value: refresh_analysis())
        refresh_analysis()

    # ---------------- AUTOMATIC WEEKLY EMAIL ALERTS ----------------

    def _load_email_automation(self):
        with db() as conn:
            row = conn.execute(
                "SELECT enabled, weekday, send_time, encrypted_config, last_sent_date "
                "FROM email_automation WHERE id=1").fetchone()
        if not row:
            return None
        enabled, weekday, send_time, encrypted_config, last_sent = row
        config = {}
        if encrypted_config:
            try:
                raw = decrypt_data(encrypted_config)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                config = json.loads(raw)
            except Exception as error:
                print("Could not decrypt saved email settings:", error)
        return {"enabled": bool(enabled), "weekday": int(weekday),
                "send_time": send_time, "config": config, "last_sent": last_sent or ""}

    def _save_email_automation(self, enabled, weekday, send_time, config):
        encrypted = encrypt_data(json.dumps(config, separators=(",", ":")))
        with db() as conn:
            conn.execute("""INSERT INTO email_automation
                (id, enabled, weekday, send_time, encrypted_config, last_sent_date)
                VALUES (1, ?, ?, ?, ?, COALESCE((SELECT last_sent_date FROM email_automation WHERE id=1), ''))
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,
                weekday=excluded.weekday, send_time=excluded.send_time,
                encrypted_config=excluded.encrypted_config""",
                (int(enabled), int(weekday), send_time, encrypted))

    @staticmethod
    def _send_scheduled_low_attendance(config):
        host = (config.get("host") or "smtp.gmail.com").strip()
        sender = (config.get("sender") or "").strip()
        password = "".join((config.get("password") or "").split())
        try:
            preferred_port = int(config.get("port") or 587)
        except (TypeError, ValueError):
            preferred_port = 587
        if not sender or not password:
            raise ValueError("Email sender and app password are not configured.")

        with db() as conn:
            students = conn.execute(
                "SELECT id, name, COALESCE(email,''), COALESCE(parent_email,'') FROM students ORDER BY id"
            ).fetchall()
            attendance_rows = conn.execute(f"""
                SELECT a.student_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END) AS present
                FROM attendance a
                WHERE NOT {EXCUSED_SQL}
                GROUP BY a.student_id
            """).fetchall()
        percentages = {sid: (present / total * 100.0 if total else 0.0)
                       for sid, total, present in attendance_rows}
        # Recipient choices are saved with the weekly schedule. Default to both
        # for schedules created by older versions of AttendIQ.
        send_to_students = bool(config.get("send_to_students", True))
        send_to_parents = bool(config.get("send_to_parents", True))
        due = []
        try:
            with db() as conn:
                threshold_row = conn.execute("SELECT setting_value FROM app_settings WHERE setting_key='attendance_threshold'").fetchone()
            threshold = float(threshold_row[0]) if threshold_row else 75.0
        except Exception:
            threshold = 75.0
        for sid, name, student_email, parent_email in students:
            pct = percentages.get(sid, 0.0)
            if pct >= threshold:
                continue
            if send_to_students and student_email.strip():
                due.append((name, student_email.strip(), pct, "student"))
            if send_to_parents and parent_email.strip():
                due.append((name, parent_email.strip(), pct, "parent"))
        if not due:
            return {"sent": 0, "failed": 0, "note": f"No recipients below {threshold:g}% attendance."}

        ports = [465, 587] if "gmail.com" in host.lower() else [preferred_port] + [x for x in (465, 587) if x != preferred_port]
        last_error = None
        for port in ports:
            try:
                context = ssl.create_default_context()
                if port == 465:
                    server = smtplib.SMTP_SSL(host, port, context=context, timeout=25)
                else:
                    server = smtplib.SMTP(host, port, timeout=25)
                with server:
                    if port != 465:
                        server.starttls(context=context)
                    server.login(sender, password)
                    sent, failed = 0, 0
                    for name, address, pct, kind in due:
                        msg = EmailMessage()
                        msg["From"] = sender
                        msg["To"] = address
                        with db() as conn:
                            settings = dict(conn.execute("SELECT setting_key,setting_value FROM app_settings").fetchall())
                        values = {"name": name, "percentage": f"{pct:.1f}", "threshold": f"{threshold:g}"}
                        if kind == "parent":
                            subject = settings.get("parent_email_subject", "AttendIQ: Low Attendance Alert for {name}")
                            body = settings.get("parent_email_template", "Dear Parent/Guardian,\n\n{name}'s attendance is {percentage}%, below {threshold}%.")
                        else:
                            subject = settings.get("student_email_subject", "AttendIQ: Low Attendance Alert")
                            body = settings.get("student_email_template", "Hello {name}, your attendance is {percentage}%, below {threshold}%.")
                        msg["Subject"] = subject.format(**values)
                        msg.set_content(body.format(**values))
                        try:
                            server.send_message(msg)
                            sent += 1
                        except Exception:
                            failed += 1
                return {"sent": sent, "failed": failed, "port": port}
            except smtplib.SMTPAuthenticationError:
                raise RuntimeError("SMTP login rejected. Check the saved Gmail App Password.")
            except Exception as error:
                last_error = error
        raise RuntimeError(f"Could not send scheduled email using ports {ports}: {last_error}")

    def _check_weekly_email_schedule(self):
        try:
            settings = self._load_email_automation()
            now = datetime.now()
            today = now.date().isoformat()
            if (settings and settings["enabled"] and now.weekday() == settings["weekday"]
                    and now.strftime("%H:%M") >= settings["send_time"]
                    and settings["last_sent"] != today
                    and self._email_last_attempt != today
                    and not self._email_job_running):
                self._email_last_attempt = today
                self._email_job_running = True
                config = settings["config"]
                threading.Thread(
                    target=self._run_scheduled_email_job, args=(config, today), daemon=True
                ).start()
        except Exception as error:
            print("Weekly email scheduler warning:", error)
        finally:
            if self.winfo_exists():
                self._email_scheduler_after = self.after(30000, self._check_weekly_email_schedule)

    def _run_scheduled_email_job(self, config, today):
        try:
            result = self._send_scheduled_low_attendance(config)
            self._email_results.put((True, today, result))
        except Exception as error:
            self._email_results.put((False, today, str(error)))

    def _poll_email_automation_results(self):
        try:
            while True:
                success, today, result = self._email_results.get_nowait()
                self._email_job_running = False
                if success:
                    with db() as conn:
                        conn.execute("UPDATE email_automation SET last_sent_date=? WHERE id=1", (today,))
                    self.log_activity("AUTO_EMAIL_ALERTS",
                                      f"Weekly scheduled alerts: sent {result['sent']}, failed {result['failed']}")
                    print("AttendIQ weekly email result:", result)
                else:
                    self.log_activity("AUTO_EMAIL_FAILED", str(result))
                    print("AttendIQ weekly email failed:", result)
        except queue.Empty:
            pass
        finally:
            if self.winfo_exists():
                self.after(1000, self._poll_email_automation_results)

    def show_email_alerts(self):
        self.clear_content()
        self.select_nav("Email Alerts")
        self.header("Email Notifications", "Send attendance and low-attendance alerts using your SMTP account")

        # Keep the entire Email Alerts workspace inside a scrollable container.
        # This allows the lower controls and help text to remain accessible on
        # smaller laptop screens without changing the email functionality.
        self.content.grid_rowconfigure(1, weight=1)
        scroll_area = FastScrollFrame(
            self.content,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#315071",
            scrollbar_button_hover_color=BLUE,
        )
        scroll_area.grid(row=1, column=0, sticky="nsew")
        scroll_area.grid_columnconfigure(0, weight=1)

        panel = self.white_panel(scroll_area)
        panel.grid(row=0, column=0, sticky="new", padx=2, pady=(0, 16))
        panel.grid_columnconfigure(1, weight=1)

        fields = {}
        labels = [
            ("SMTP host", "smtp.gmail.com"),
            ("SMTP port", "587"),
            ("Sender email", "your_email@gmail.com"),
            ("App password", "Use a Gmail App Password"),
        ]
        for i, (label, hint) in enumerate(labels):
            ctk.CTkLabel(panel, text=label, text_color=TEXT).grid(
                row=i, column=0, sticky="w", padx=18, pady=9
            )
            entry = ctk.CTkEntry(
                panel,
                placeholder_text=hint,
                show="•" if "password" in label.lower() else "",
                width=380,
            )
            entry.grid(row=i, column=1, sticky="ew", padx=18, pady=9)
            fields[label] = entry
            if "password" in label.lower():
                def toggle(e=entry):
                    e.configure(show="" if show_var.get() else "\u2022")
                show_var = ctk.BooleanVar(value=False)
                ctk.CTkCheckBox(panel, text="Show", variable=show_var, command=toggle,
                                width=60, checkbox_width=18, checkbox_height=18
                                ).grid(row=i, column=2, padx=(0, 18))

        # Restore the saved SMTP details and weekly schedule. The password is
        # stored encrypted in the local database, never as plain text.
        saved_schedule = self._load_email_automation() or {
            "enabled": False, "weekday": 4, "send_time": "16:00", "config": {}}
        saved_config = saved_schedule.get("config", {})
        for label, key in (("SMTP host", "host"), ("SMTP port", "port"),
                           ("Sender email", "sender"), ("App password", "password")):
            value = saved_config.get(key, "")
            if value:
                fields[label].insert(0, str(value))

        ctk.CTkLabel(
            panel,
            text="Recipients are read from the email field saved for each student. Add student email addresses in the Students page.",
            text_color=MUTED,
            wraplength=850,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=18, pady=10)

        # Who should receive the alerts?
        to_student = ctk.BooleanVar(value=True)
        to_parent = ctk.BooleanVar(value=True)
        who = ctk.CTkFrame(panel, fg_color="transparent")
        who.grid(row=4, column=0, columnspan=2, sticky="w", padx=18, pady=(6, 0))
        ctk.CTkLabel(who, text="Send alerts to:", text_color=TEXT).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(who, text="Student", variable=to_student).pack(side="left", padx=(0, 18))
        ctk.CTkCheckBox(who, text="Parent", variable=to_parent).pack(side="left")

        send_button = None

        def send_alerts():
            host = fields["SMTP host"].get().strip() or "smtp.gmail.com"
            try:
                port = int(fields["SMTP port"].get().strip() or "587")
            except ValueError:
                messagebox.showerror("SMTP port", "Enter a valid numeric SMTP port.")
                return

            sender = fields["Sender email"].get().strip()
            password = fields["App password"].get().replace(" ", "").strip()
            if not sender or not password:
                messagebox.showwarning("SMTP details", "Enter sender email and app password.")
                return
            if host.lower().endswith("gmail.com") and len(password) != 16:
                messagebox.showerror(
                    "App password length",
                    f"The password you entered has {len(password)} characters, but a Gmail "
                    "App Password has exactly 16 letters.\n\nClear the field and type it again "
                    "(typing by hand is safer than pasting).")
                return

            want_student, want_parent = to_student.get(), to_parent.get()
            if not (want_student or want_parent):
                messagebox.showwarning("Recipients", "Tick Student, Parent, or both.")
                return

            with db() as conn:
                rows = conn.execute(
                    "SELECT id, name, COALESCE(email,''), COALESCE(parent_email,'') FROM students"
                ).fetchall()
            due, any_address = [], False
            for sid, name, email, parent_email in rows:
                targets = []
                if want_student and email.strip():
                    targets.append((email.strip(), "student"))
                if want_parent and parent_email.strip():
                    targets.append((parent_email.strip(), "parent"))
                if not targets:
                    continue
                any_address = True
                pct = self.attendance_percentage(sid)
                threshold = self.attendance_threshold()
                if pct < threshold:
                    for address, kind in targets:
                        due.append((name, address, pct, kind))
            if not any_address:
                messagebox.showinfo(
                    "No recipients",
                    "No email addresses found for the selected recipients. Add student or "
                    "parent emails in the Students page first.")
                return
            if not due:
                messagebox.showinfo(
                    "Nothing to send",
                    "No student with an email address is below 75% attendance.")
                return

            # Try the chosen port first, then the other standard Gmail port.
            if host.lower().endswith("gmail.com"):
                ports = [465, 587]          # 465 is the most reliable for Gmail
            else:
                ports = [port] + [p for p in (587, 465) if p != port]
            trail = []
            outcome = {}
            done = threading.Event()
            flags = {"dropped": False}

            def deliver():
                last_error = None
                for attempt in ports:
                    stage = "connecting"
                    try:
                        context = ssl.create_default_context()
                        if attempt == 465:
                            server = smtplib.SMTP_SSL(host, attempt, context=context, timeout=20)
                        else:
                            server = smtplib.SMTP(host, attempt, timeout=20)
                        with server:
                            if attempt != 465:
                                stage = "starttls"
                                server.starttls(context=context)
                            stage = "login"
                            server.login(sender, password)
                            stage = "sending"
                            sent, failed = 0, []
                            for name, email, pct, kind in due:
                                try:
                                    msg = EmailMessage()
                                    msg["From"] = sender
                                    msg["To"] = email.strip()
                                    values = {"name": name, "percentage": f"{pct:.1f}", "threshold": f"{threshold:g}"}
                                    if kind == "parent":
                                        subject = self._setting("parent_email_subject", "AttendIQ: Low Attendance Alert for {name}")
                                        body = self._setting("parent_email_template", "Dear Parent/Guardian,\n\n{name}'s attendance is {percentage}%, below {threshold}%.")
                                    else:
                                        subject = self._setting("student_email_subject", "AttendIQ: Low Attendance Alert")
                                        body = self._setting("student_email_template", "Hello {name}, your attendance is {percentage}%, below {threshold}%.")
                                    msg["Subject"] = subject.format(**values)
                                    msg.set_content(body.format(**values))
                                    server.send_message(msg)
                                    sent += 1
                                except Exception as ex:
                                    failed.append(f"{email}: {ex}")
                        outcome.update(sent=sent, failed=failed, port=attempt)
                        return
                    except smtplib.SMTPAuthenticationError as ex:
                        outcome["auth"] = ex
                        return
                    except smtplib.SMTPServerDisconnected as ex:
                        # Gmail hangs up during login when the password is malformed.
                        last_error = ex
                        trail.append(f"port {attempt}, during {stage}")
                        flags["dropped"] = True
                    except (smtplib.SMTPException, OSError) as ex:
                        last_error = ex
                        trail.append(f"port {attempt}, during {stage}: {type(ex).__name__}")
                        flags["dropped"] = False
                    except Exception as ex:  # anything unexpected must still end the "Sending..." state
                        outcome["error"] = ex
                        return
                outcome["error"] = last_error

            def deliver_safe():
                try:
                    deliver()
                except BaseException as ex:
                    outcome["error"] = ex
                if not outcome:
                    outcome["error"] = RuntimeError("Sending stopped unexpectedly")
                done.set()   # only now is the result complete

            started = datetime.now()

            def finish():
                if not done.is_set():
                    if (datetime.now() - started).total_seconds() > 120:
                        outcome.clear()
                        outcome["error"] = TimeoutError("No response from the mail server after 2 minutes")
                    else:
                        self.after(150, finish)
                        return
                try:
                    if not send_button.winfo_exists():
                        return
                    send_button.configure(state="normal", text="Send Low Attendance Alerts")
                except Exception:
                    return
                if "auth" in outcome:
                    messagebox.showerror(
                        "Login failed",
                        "Gmail rejected the login.\n\nUse a 16-character Google App Password "
                        "(not your normal password). 2-Step Verification must be turned on.")
                elif "error" in outcome and flags["dropped"]:
                    messagebox.showerror(
                        "Login problem",
                        "Gmail connected but closed the connection.\n\n"
                        "Tried: " + "; ".join(trail) + "\n\n"
                        "This usually means the App Password was typed wrongly or is old/deleted. "
                        "Tick 'Show' next to the password, check all 16 letters carefully, "
                        "and try again.")
                elif "error" in outcome:
                    messagebox.showerror(
                        "Email error",
                        f"Could not connect to {host} on ports {', '.join(map(str, ports))}.\n\n"
                        f"Last error: {outcome['error']}\n\n"
                        "This usually means the network, firewall or antivirus is blocking email "
                        "(SMTP). Try a mobile hotspot, or switch off your antivirus's "
                        "'email / mail shield' temporarily.")
                else:
                    failed = outcome["failed"]
                    self.log_activity(
                        "EMAIL_ALERTS",
                        f"Sent {outcome['sent']} low-attendance alerts; failures {len(failed)}")
                    messagebox.showinfo(
                        "Email results",
                        f"Sent: {outcome['sent']}\nFailed: {len(failed)}"
                        + ("\n\n" + "\n".join(failed[:3]) if failed else ""))

            send_button.configure(state="disabled", text="Sending...")
            threading.Thread(target=deliver_safe, daemon=True).start()
            self.after(150, finish)

        send_button = ctk.CTkButton(
            panel,
            text="Send Low Attendance Alerts",
            command=send_alerts,
            fg_color=BLUE,
            text_color="#04111F",
        )
        send_button.grid(row=6, column=0, columnspan=2, pady=18)

        schedule_panel = self.white_panel(scroll_area)
        schedule_panel.grid(row=2, column=0, sticky="ew", padx=2, pady=(0, 16))
        ctk.CTkLabel(schedule_panel, text="Automatic Weekly Alerts",
                     font=("Segoe UI", 18, "bold"), text_color=TEXT).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=18, pady=(16, 5))
        ctk.CTkLabel(schedule_panel,
                     text="AttendIQ checks this schedule automatically while the application is running.",
                     text_color=MUTED, wraplength=820, justify="left").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=18, pady=(0, 12))
        auto_enabled = ctk.BooleanVar(value=saved_schedule["enabled"])
        ctk.CTkCheckBox(schedule_panel, text="Enable automatic email alerts",
                        variable=auto_enabled, text_color=TEXT).grid(
            row=2, column=0, sticky="w", padx=18, pady=10)
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                         "Friday", "Saturday", "Sunday"]
        day_menu = ctk.CTkOptionMenu(schedule_panel, values=weekday_names, width=150)
        day_menu.set(weekday_names[saved_schedule["weekday"]])
        day_menu.grid(row=2, column=1, padx=8, pady=10)
        time_entry = ctk.CTkEntry(schedule_panel, width=110, placeholder_text="HH:MM")
        time_entry.insert(0, saved_schedule["send_time"])
        time_entry.grid(row=2, column=2, padx=8, pady=10)
        # Separate recipient choices for automatic weekly notifications.
        saved_send_students = bool(saved_config.get("send_to_students", True))
        saved_send_parents = bool(saved_config.get("send_to_parents", True))
        auto_to_student = ctk.BooleanVar(value=saved_send_students)
        auto_to_parent = ctk.BooleanVar(value=saved_send_parents)
        recipient_row = ctk.CTkFrame(schedule_panel, fg_color="transparent")
        recipient_row.grid(row=3, column=0, columnspan=4, sticky="w", padx=18, pady=(4, 8))
        ctk.CTkLabel(recipient_row, text="Automatically send to:", text_color=TEXT).pack(side="left", padx=(0, 12))
        ctk.CTkCheckBox(recipient_row, text="Students", variable=auto_to_student,
                        text_color=TEXT).pack(side="left", padx=(0, 18))
        ctk.CTkCheckBox(recipient_row, text="Parents / Guardians", variable=auto_to_parent,
                        text_color=TEXT).pack(side="left")
        schedule_status = ctk.CTkLabel(schedule_panel, text="", text_color=MUTED)
        schedule_status.grid(row=4, column=0, columnspan=4, sticky="w", padx=18, pady=(0, 4))

        def save_schedule():
            try:
                scheduled_time = datetime.strptime(time_entry.get().strip(), "%H:%M").strftime("%H:%M")
            except ValueError:
                messagebox.showerror("Invalid time", "Enter time in 24-hour HH:MM format, for example 16:00.")
                return
            config = {
                "host": fields["SMTP host"].get().strip() or "smtp.gmail.com",
                "port": fields["SMTP port"].get().strip() or "587",
                "sender": fields["Sender email"].get().strip(),
                "password": "".join(fields["App password"].get().split()),
            }
            config["send_to_students"] = auto_to_student.get()
            config["send_to_parents"] = auto_to_parent.get()
            if auto_enabled.get() and not (config["send_to_students"] or config["send_to_parents"]):
                messagebox.showwarning("Recipients required", "Select Students, Parents, or both for automatic alerts.")
                return
            if auto_enabled.get() and (not config["sender"] or not config["password"]):
                messagebox.showwarning("Email details required",
                                       "Enter the sender email and Gmail App Password before enabling the schedule.")
                return
            if auto_enabled.get() and "gmail.com" in config["host"].lower() and len(config["password"]) != 16:
                messagebox.showerror("Invalid Gmail App Password",
                                     "A Gmail App Password must contain 16 characters.")
                return
            self._save_email_automation(auto_enabled.get(), weekday_names.index(day_menu.get()),
                                        scheduled_time, config)
            recipients = []
            if config["send_to_students"]:
                recipients.append("students")
            if config["send_to_parents"]:
                recipients.append("parents")
            self.log_activity("EMAIL_SCHEDULE_UPDATED",
                              f"Weekly email schedule {'enabled' if auto_enabled.get() else 'disabled'}: "
                              f"{day_menu.get()} at {scheduled_time}; recipients: {', '.join(recipients) or 'none'}")
            schedule_status.configure(text=(
                f"Enabled: every {day_menu.get()} at {scheduled_time}; recipients: {', '.join(recipients)}. Keep AttendIQ running."
                if auto_enabled.get() else "Automatic weekly emails are disabled."))
            messagebox.showinfo("Schedule saved",
                                "Your weekly email schedule has been saved. AttendIQ must remain open "
                                "and connected to the internet at the scheduled time.")

        ctk.CTkButton(schedule_panel, text="Save Weekly Schedule", command=save_schedule,
                      fg_color=BLUE, text_color="#04111F").grid(
            row=2, column=3, padx=18, pady=10)
        if saved_schedule["enabled"]:
            schedule_status.configure(text=(
                f"Enabled: every {weekday_names[saved_schedule['weekday']]} at "
                f"{saved_schedule['send_time']}. Keep AttendIQ running."))

        # Add breathing room below the panel so the scrollbar can reveal the
        # final button completely on short displays.
        ctk.CTkFrame(scroll_area, fg_color="transparent", height=24).grid(
            row=3, column=0, sticky="ew"
        )

    # ---------------- DASHBOARD ----------------

    def show_dashboard(self):
        self.clear_content()
        self.select_nav("Dashboard")
        self.header("Dashboard", "Your student and attendance overview")

        today = date.today().isoformat()
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
            recent_students = conn.execute(
                "SELECT id, name FROM students ORDER BY rowid DESC LIMIT 5"
            ).fetchall()
            counts = dict(conn.execute(
                "SELECT status, COUNT(*) FROM attendance WHERE date = ? GROUP BY status",
                (today,)).fetchall())
            low_rows = conn.execute(f"""SELECT s.id,s.name,
                SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END)*100.0/COUNT(a.id) AS pct
                FROM students s LEFT JOIN attendance a ON a.student_id=s.id AND a.status IN ('Present','Absent') AND NOT {EXCUSED_SQL}
                GROUP BY s.id HAVING COUNT(a.id)>0 AND SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END)*100.0/COUNT(a.id)<75 ORDER BY pct ASC LIMIT 8""").fetchall()
            week_rows = conn.execute("""SELECT date, SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END)*100.0/COUNT(*)
                FROM attendance WHERE date>=date('now','-6 day') AND status IN ('Present','Absent') GROUP BY date ORDER BY date""").fetchall()
            fee_summary = conn.execute("""
                SELECT COALESCE(SUM(billed),0),COALESCE(SUM(paid),0),
                       COALESCE(SUM(balance),0),COALESCE(SUM(overdue),0)
                FROM (
                    SELECT fa.id,fa.total_amount AS billed,COALESCE(SUM(fp.amount),0) AS paid,
                           MAX(0,fa.total_amount-COALESCE(SUM(fp.amount),0)) AS balance,
                           CASE WHEN fa.due_date<>'' AND fa.due_date<?
                                THEN MAX(0,fa.total_amount-COALESCE(SUM(fp.amount),0))
                                ELSE 0 END AS overdue
                    FROM student_fee_accounts fa
                    LEFT JOIN student_fee_payments fp ON fp.fee_account_id=fa.id
                    GROUP BY fa.id
                )
            """, (today,)).fetchone()
            fee_due_students = conn.execute("""
                SELECT s.id,s.name,SUM(a.balance),SUM(a.overdue)
                FROM students s JOIN (
                    SELECT fa.student_id,fa.id,
                           MAX(0,fa.total_amount-COALESCE(SUM(fp.amount),0)) AS balance,
                           CASE WHEN fa.due_date<>'' AND fa.due_date<?
                                THEN MAX(0,fa.total_amount-COALESCE(SUM(fp.amount),0))
                                ELSE 0 END AS overdue
                    FROM student_fee_accounts fa
                    LEFT JOIN student_fee_payments fp ON fp.fee_account_id=fa.id
                    GROUP BY fa.id
                ) a ON a.student_id=s.id
                GROUP BY s.id HAVING SUM(a.balance)>0.005
                ORDER BY SUM(a.overdue) DESC,SUM(a.balance) DESC,LOWER(s.name) LIMIT 5
            """, (today,)).fetchall()
        present, absent = counts.get("Present", 0), counts.get("Absent", 0)
        marked = present + absent
        rate = f"{present / marked * 100:.1f}%" if marked else "—"
        attendance_note = (f"{marked} of {total} students marked"
                           if total else "Register students to begin")

        # Keep the whole overview reachable on short windows instead of letting
        # the stats row compress and clip the card details.
        overview = FastScrollFrame(self.content, fg_color="transparent")
        overview.grid(row=1, column=0, sticky="nsew")
        overview.grid_columnconfigure(0, weight=1)

        stats = ctk.CTkFrame(overview, fg_color="transparent")
        stats.grid(row=0, column=0, sticky="ew")
        for col in range(4):
            stats.grid_columnconfigure(col, weight=1, uniform="stats")

        items = [
            ("Registered Students", total, "Total student records", BLUE),
            ("Present Today", present, attendance_note, GREEN),
            ("Absent Today", absent, "Saved absent records", RED),
            ("Attendance Rate", rate, "Based on marked records", AMBER),
        ]
        for i, item in enumerate(items):
            self.card(stats, *item).grid(
                row=0, column=i, sticky="ew",
                padx=(0 if i == 0 else 7, 7 if i < 3 else 0))

        fee_panel = self.make_panel(overview, "Fee Tracking")
        fee_panel.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        fee_panel.grid_columnconfigure(0, weight=1)
        fee_top = ctk.CTkFrame(fee_panel, fg_color="transparent")
        fee_top.pack(fill="x", padx=16, pady=(2, 10))
        for column in range(4):
            fee_top.grid_columnconfigure(column, weight=1, uniform="dashboard_fees")
        fee_values = [
            ("BILLED", fee_summary[0], BLUE),
            ("PAID", fee_summary[1], GREEN),
            ("OUTSTANDING", fee_summary[2], AMBER),
            ("PAST DUE", fee_summary[3], RED),
        ]
        for column, (label, amount, color) in enumerate(fee_values):
            item = ctk.CTkFrame(fee_top, fg_color="#14243B", corner_radius=8)
            item.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 6 if column < 3 else 0))
            ctk.CTkLabel(item, text=label, text_color=MUTED,
                         font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(9, 2))
            ctk.CTkLabel(item, text=f"₹{float(amount or 0):,.2f}", text_color=color,
                         font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=12, pady=(0, 9))
        fee_list = ctk.CTkFrame(fee_panel, fg_color="transparent")
        fee_list.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkLabel(fee_list, text="Students with outstanding balances",
                     text_color=TEXT, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))
        if fee_due_students:
            for fee_sid, fee_name, balance, overdue in fee_due_students:
                row = ctk.CTkFrame(fee_list, fg_color="#14243B", corner_radius=7)
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(row, text=f"{fee_sid}  ·  {fee_name}", text_color=TEXT,
                             font=("Segoe UI", 11)).pack(side="left", padx=10, pady=7)
                past_due = float(overdue or 0)
                amount_text = f"₹{float(balance or 0):,.2f} due"
                if past_due > 0:
                    amount_text += f"  ·  ₹{past_due:,.2f} past due"
                ctk.CTkLabel(row, text=amount_text,
                             text_color=RED if past_due > 0 else AMBER,
                             font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
                ctk.CTkButton(
                    row, text="Manage", width=82, height=28, fg_color="#1A2E49",
                    hover_color="#244364", text_color=TEXT,
                    command=self.show_fee_management,
                ).pack(side="right", padx=8, pady=4)
        else:
            ctk.CTkLabel(fee_list, text="No outstanding fee balances. Add fee items through Student Full Analysis.",
                         text_color=GREEN if fee_summary[0] else MUTED,
                         font=("Segoe UI", 11)).pack(anchor="w", pady=4)
        ctk.CTkButton(
            fee_panel, text="Open Fee Details", height=34, fg_color="#1C3554",
            text_color=TEXT, command=self.show_fee_management,
        ).pack(anchor="e", padx=16, pady=(0, 12))

        lower = ctk.CTkFrame(overview, fg_color="transparent")
        lower.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        lower.grid_columnconfigure(0, weight=3)
        lower.grid_columnconfigure(1, weight=2)

        recent = self.make_panel(lower, "Recently Registered Students")
        recent.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        for sid, name in recent_students:
            row = ctk.CTkFrame(recent, fg_color="#14243B", corner_radius=8)
            row.pack(fill="x", padx=15, pady=4)
            ctk.CTkLabel(row, text=f"  {sid}  |  {name}", font=("Segoe UI", 12),
                         text_color=TEXT).pack(anchor="w", padx=8, pady=11)
        if not recent_students:
            ctk.CTkLabel(recent, text="No students registered yet.",
                         text_color=MUTED).pack(anchor="w", padx=20, pady=12)

        quick = self.make_panel(lower, "AI Insights & Quick Actions")
        quick.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(quick,text=f"LOW ATTENDANCE WATCH  •  {len(low_rows)} students below 75%",text_color=AMBER,font=("Segoe UI",11,"bold")).pack(anchor="w",padx=15,pady=(2,8))
        if low_rows:
            for sid,nm,pct in low_rows[:4]: ctk.CTkLabel(quick,text=f"{sid}  {nm[:22]}  —  {pct:.1f}%",text_color=RED,font=("Segoe UI",10)).pack(anchor="w",padx=18,pady=2)
        else: ctk.CTkLabel(quick,text="No low-attendance alerts",text_color=GREEN).pack(anchor="w",padx=18,pady=3)
        ctk.CTkLabel(quick,text="7-DAY ATTENDANCE TREND",text_color=BLUE,font=("Segoe UI",11,"bold")).pack(anchor="w",padx=15,pady=(10,4))
        for d,pct in week_rows[-7:]:
            ctk.CTkLabel(quick,text=f"{d[5:]}   {'▰'*max(1,int(pct/10))}   {pct:.0f}%",text_color=TEXT,font=("Consolas",10)).pack(anchor="w",padx=18,pady=1)
        for label, command in [
            ("Take Attendance", self.show_attendance),
            ("Register Student", self.open_add_student),
            ("View Student Directory", self.show_students),
            ("Fee Management", self.show_fee_management),
            ("Student Full Analysis", self.show_student_analytics),
            ("Open Reports", self.show_reports),
        ]:
            ctk.CTkButton(
                quick, text=label, height=40, anchor="w", fg_color="#1A2E49",
                hover_color="#244364", text_color=TEXT, corner_radius=8, command=command
            ).pack(fill="x", padx=15, pady=5)
        ctk.CTkLabel(quick, text=f"Last refreshed: {datetime.now():%H:%M}",
                     text_color=MUTED, font=("Segoe UI", 10)
                     ).pack(anchor="w", padx=18, pady=(10, 16))

    # ---------------- FEE MANAGEMENT ----------------

    def show_fee_management(self):
        """School-wide fee ledger and payment management, separate from student analysis."""
        self.clear_content()
        self.select_nav("Fee Management")
        self.header("Fee Management", "Track tuition, transport, hostel and other fees")
        self.content.grid_rowconfigure(1, weight=1)
        area = FastScrollFrame(self.content, fg_color="transparent", corner_radius=0)
        area.grid(row=1, column=0, sticky="nsew")
        area.grid_columnconfigure(0, weight=1)

        categories = ["All Categories", "Tuition Fee", "Transport Fee", "Hostel Fee", "Others"]
        with db() as conn:
            student_filters = conn.execute("SELECT DISTINCT COALESCE(year,''),COALESCE(department,'') FROM students").fetchall()
        year_values = ["All Years"] + sorted({row[0] for row in student_filters if row[0]})
        department_values = ["All Departments"] + sorted({row[1] for row in student_filters if row[1]})
        toolbar = ctk.CTkFrame(area, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        toolbar.grid_columnconfigure(0, weight=1)
        fee_search = ctk.CTkEntry(toolbar, height=36, placeholder_text="Search student ID, name, year or fee…")
        fee_search.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        category_filter = ctk.CTkOptionMenu(
            toolbar, values=categories, width=170, height=36,
            command=lambda _value: refresh())
        category_filter.grid(row=0, column=1, padx=(0, 8))
        year_filter = ctk.CTkOptionMenu(toolbar, values=year_values, width=135, height=36,
                                        command=lambda _value: refresh())
        year_filter.grid(row=0, column=2, padx=4)
        department_filter = ctk.CTkOptionMenu(toolbar, values=department_values, width=155, height=36,
                                              command=lambda _value: refresh())
        department_filter.grid(row=0, column=3, padx=4)
        ctk.CTkButton(
            toolbar, text="＋ Add Fee", height=36, fg_color=BLUE,
            text_color="#04111F", command=lambda: add_fee()).grid(row=0, column=4, padx=4)
        ctk.CTkButton(
            toolbar, text="＋ Record Payment", height=36, fg_color="#1C3554",
            command=lambda: add_payment()).grid(row=0, column=5, padx=(4, 0))

        metrics = ctk.CTkFrame(area, fg_color="transparent")
        metrics.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for col in range(4):
            metrics.grid_columnconfigure(col, weight=1, uniform="fee_management_metrics")

        category_panel = self.make_panel(area, "Fee Breakdown by Category")
        category_panel.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        category_panel.grid_columnconfigure(0, weight=1)
        category_table = FastScrollFrame(category_panel, fg_color="transparent", height=150)
        category_table.pack(fill="x", padx=12, pady=(0, 12))

        ledger_panel = self.make_panel(area, "Student Fee Ledger")
        ledger_panel.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ledger_panel.grid_columnconfigure(0, weight=1)
        ledger_table = FastScrollFrame(ledger_panel, fg_color="transparent", height=390)
        ledger_table.pack(fill="x", padx=12, pady=(0, 12))

        payments_panel = self.make_panel(area, "Recent Payments · Latest 100")
        payments_panel.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        payments_panel.grid_columnconfigure(0, weight=1)
        payments_table = FastScrollFrame(payments_panel, fg_color="transparent", height=280)
        payments_table.pack(fill="x", padx=12, pady=(0, 12))

        def money(value):
            return f"₹{float(value or 0):,.2f}"

        def refresh(_value=None):
            today = date.today().isoformat()
            selected_category = category_filter.get()
            filters = ""
            params = []
            if selected_category != "All Categories":
                if selected_category == "Others":
                    filters = " AND fa.fee_type NOT IN ('Tuition Fee','Transport Fee','Hostel Fee')"
                else:
                    filters = " AND fa.fee_type=?"
                    params.append(selected_category)
            query = fee_search.get().strip().lower()
            if query:
                filters += " AND (CAST(fa.student_id AS TEXT) LIKE ? OR LOWER(s.name) LIKE ? OR LOWER(fa.academic_year) LIKE ? OR LOWER(fa.fee_type) LIKE ?)"
                params.extend([f"%{query}%"] * 4)
            if year_filter.get() != "All Years":
                filters += " AND s.year=?"
                params.append(year_filter.get())
            if department_filter.get() != "All Departments":
                filters += " AND UPPER(s.department)=?"
                params.append(department_filter.get().upper())
            with db() as conn:
                account_rows = conn.execute(f"""
                    SELECT fa.id,fa.student_id,s.name,fa.academic_year,fa.fee_type,
                           fa.total_amount,COALESCE(SUM(fp.amount),0),fa.due_date,
                           COALESCE(fa.notes,'')
                    FROM student_fee_accounts fa
                    JOIN students s ON s.id=fa.student_id
                    LEFT JOIN student_fee_payments fp ON fp.fee_account_id=fa.id
                    WHERE 1=1 {filters}
                    GROUP BY fa.id
                    ORDER BY CASE WHEN fa.due_date<>'' AND fa.due_date<? THEN 0 ELSE 1 END,
                             fa.due_date,LOWER(s.name),fa.fee_type
                """, (*params, today)).fetchall()
                payment_rows = conn.execute(f"""
                    SELECT fp.paid_on,s.id,s.name,fa.fee_type,fp.amount,fp.method,
                           COALESCE(fp.reference,''),COALESCE(fp.recorded_by,'')
                    FROM student_fee_payments fp
                    JOIN student_fee_accounts fa ON fa.id=fp.fee_account_id
                    JOIN students s ON s.id=fa.student_id
                    WHERE 1=1 {filters}
                    ORDER BY fp.paid_on DESC,fp.id DESC LIMIT 100
                """, params).fetchall()

            billed = sum(float(row[5] or 0) for row in account_rows)
            paid = sum(float(row[6] or 0) for row in account_rows)
            balance = sum(max(0.0, float(row[5] or 0) - float(row[6] or 0))
                          for row in account_rows)
            past_due = sum(max(0.0, float(row[5] or 0) - float(row[6] or 0))
                           for row in account_rows
                           if row[7] and row[7] < today)
            self.clear_children(metrics)
            for index, item in enumerate((
                ("Total Billed", money(billed), f"{len(account_rows)} fee item(s)", BLUE),
                ("Collected", money(paid), f"{len(payment_rows)} recent payment(s)", GREEN),
                ("Outstanding", money(balance), "All unpaid balances", AMBER if balance else GREEN),
                ("Past Due", money(past_due), "Unpaid after due date", RED if past_due else GREEN),
            )):
                self.card(metrics, *item).grid(row=0, column=index, sticky="ew",
                                               padx=(0 if index == 0 else 6, 6 if index < 3 else 0))

            for table in (category_table, ledger_table, payments_table):
                table._fill_token = object()
                self.clear_children(table)
            self.table_header(category_table,
                ["CATEGORY", "FEE ITEMS", "BILLED", "COLLECTED", "OUTSTANDING", "PAST DUE"],
                [2,1,2,2,2,2], "fee_category")
            self.table_header(ledger_table,
                ["STUDENT", "ACADEMIC YEAR", "CATEGORY", "BILLED", "PAID", "BALANCE", "DUE DATE", "STATUS", "ACTIONS"],
                [3,2,2,1,1,1,2,1,4], "fee_ledger_main")
            self.table_header(payments_table,
                ["DATE", "STUDENT", "CATEGORY", "AMOUNT", "METHOD", "REFERENCE", "RECORDED BY"],
                [2,3,2,1,2,2,2], "fee_payments_main")

            category_groups = {}
            for row in account_rows:
                category = row[4] if row[4] in {"Tuition Fee", "Transport Fee", "Hostel Fee"} else "Others"
                data = category_groups.setdefault(category, [0, 0.0, 0.0, 0.0, 0.0])
                item_balance = max(0.0, float(row[5] or 0) - float(row[6] or 0))
                data[0] += 1
                data[1] += float(row[5] or 0)
                data[2] += float(row[6] or 0)
                data[3] += item_balance
                if row[7] and row[7] < today:
                    data[4] += item_balance
            if category_groups:
                for index, category in enumerate(("Tuition Fee", "Transport Fee", "Hostel Fee", "Others"), start=1):
                    data = category_groups.get(category)
                    if not data:
                        continue
                    self.table_row(category_table, index,
                        [category, data[0], money(data[1]), money(data[2]), money(data[3]), money(data[4])],
                        [2,1,2,2,2,2], "fee_category", colors={5: RED if data[4] else MUTED}, shade=False)
            else:
                self.empty_message(category_table, "No fee items match this category yet.")

            def build_account(index, row):
                account_id, sid, name, academic_year, fee_type, amount, paid_amount, due_date, notes = row
                outstanding = max(0.0, float(amount or 0) - float(paid_amount or 0))
                if outstanding <= 0.005:
                    status, status_color = "Paid", GREEN
                elif due_date and due_date < today:
                    status, status_color = "Overdue", RED
                elif paid_amount:
                    status, status_color = "Part-paid", AMBER
                else:
                    status, status_color = "Unpaid", AMBER
                table_row = self.table_row(ledger_table, index,
                    [f"{sid} · {name}", academic_year, fee_type, money(amount), money(paid_amount),
                     money(outstanding), due_date or "—", status, None],
                    [3,2,2,1,1,1,2,1,3], "fee_ledger_main", colors={7: status_color}, shade=False)
                actions = tk.Frame(table_row, bg=table_row.cget("bg"), bd=0, highlightthickness=0)
                actions.grid(row=0, column=8, sticky="w", padx=self._px(4), pady=self._px(3))
                self.mini_button(actions, "Edit Fee", "#203754", "#2A4668", TEXT,
                    lambda aid=account_id: edit_fee(aid)
                ).pack(side="left", padx=2)
                self.mini_button(actions, "Delete Fee", "#4B2032", "#64263C", RED,
                    lambda aid=account_id, nm=name, fee=fee_type: delete_fee(aid, nm, fee)
                ).pack(side="left", padx=2)
                if outstanding > 0.005:
                    self.mini_button(actions, "Record Payment", "#123F45", "#15525A", GREEN,
                        lambda aid=account_id: add_payment(aid)).pack(side="left", padx=2)
            if account_rows:
                self.fill_batched(ledger_table, account_rows, build_account)
            else:
                self.empty_message(ledger_table, "No fee items found. Use Add Fee to create the first item.")

            def build_payment(index, row):
                paid_on, sid, name, fee_type, amount, method, reference, recorded_by = row
                self.table_row(payments_table, index,
                    [paid_on, f"{sid} · {name}", fee_type, money(amount), method,
                     reference or "—", recorded_by or "—"],
                    [2,3,2,1,2,2,2], "fee_payments_main", shade=False)
            if payment_rows:
                self.fill_batched(payments_table, payment_rows, build_payment)
            else:
                self.empty_message(payments_table, "No payments have been recorded for this selection.")

        def edit_fee(account_id):
            with db() as conn:
                record = conn.execute("""
                    SELECT fa.student_id,s.name,fa.academic_year,fa.fee_type,
                           fa.total_amount,fa.due_date,COALESCE(fa.notes,''),
                           COALESCE((SELECT SUM(amount) FROM student_fee_payments
                                     WHERE fee_account_id=fa.id),0)
                    FROM student_fee_accounts fa JOIN students s ON s.id=fa.student_id
                    WHERE fa.id=?
                """, (account_id,)).fetchone()
            if not record:
                messagebox.showerror("Fee item not found", "This fee item no longer exists.", parent=self)
                refresh()
                return
            sid, student_name, old_year, old_category, old_amount, old_due, old_notes, paid_total = record
            dialog = ctk.CTkToplevel(self)
            dialog.title("Edit Fee Details")
            dialog.geometry("500x570")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Edit Fee Details", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 5))
            ctk.CTkLabel(dialog, text=f"{sid} · {student_name}", text_color=MUTED
                         ).pack(anchor="w", padx=24, pady=(0, 12))
            standard = ["Tuition Fee", "Transport Fee", "Hostel Fee"]
            category_value = old_category if old_category in standard else "Others"
            category_menu = ctk.CTkOptionMenu(dialog,
                values=["Tuition Fee", "Transport Fee", "Hostel Fee", "Others"], width=440)
            category_menu.pack(fill="x", padx=24, pady=7)
            category_menu.set(category_value)
            other_category = ctk.CTkEntry(dialog, height=38, placeholder_text="Other fee name")
            if category_value == "Others":
                other_category.insert(0, old_category)
                other_category.pack(fill="x", padx=24, pady=7)
            def toggle_other(value):
                if value == "Others":
                    if not other_category.winfo_manager():
                        other_category.pack(fill="x", padx=24, pady=7, after=category_menu)
                else:
                    other_category.pack_forget()
            category_menu.configure(command=toggle_other)
            year_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Academic year")
            year_entry.insert(0, old_year or "")
            year_entry.pack(fill="x", padx=24, pady=7)
            amount_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Fee amount in INR")
            amount_entry.insert(0, f"{float(old_amount):.2f}")
            amount_entry.pack(fill="x", padx=24, pady=7)
            due_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Due date (YYYY-MM-DD, optional)")
            due_entry.insert(0, old_due or "")
            due_entry.pack(fill="x", padx=24, pady=7)
            notes_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Notes (optional)")
            notes_entry.insert(0, old_notes or "")
            notes_entry.pack(fill="x", padx=24, pady=7)

            def save_edit():
                fee_type = category_menu.get()
                if fee_type == "Others":
                    fee_type = other_category.get().strip()
                year_value = year_entry.get().strip()
                try:
                    amount = float(amount_entry.get().replace(",", "").replace("₹", "").strip())
                    due_date = due_entry.get().strip()
                    if due_date:
                        due_date = datetime.strptime(due_date, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Check fee details", "Enter a positive amount and valid due date.", parent=dialog)
                    return
                if not fee_type or not year_value or amount <= 0:
                    messagebox.showerror("Check fee details", "Category, academic year and positive amount are required.", parent=dialog)
                    return
                if amount + 0.005 < float(paid_total or 0):
                    messagebox.showerror("Amount below payments",
                        f"The fee amount cannot be less than the {money(paid_total)} already recorded as paid.", parent=dialog)
                    return
                with db() as conn:
                    conn.execute("""UPDATE student_fee_accounts
                        SET academic_year=?,fee_type=?,total_amount=?,due_date=?,notes=?
                        WHERE id=?""",
                        (year_value, fee_type, amount, due_date, notes_entry.get().strip(), account_id))
                self.log_activity("FEE_ITEM_EDITED", f"Student {sid}: updated {fee_type} to {amount:.2f} INR")
                dialog.destroy()
                refresh()
            ctk.CTkButton(dialog, text="Save Fee Details", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save_edit).pack(padx=24, pady=14, anchor="e")

        def delete_fee(account_id, student_name, fee_type):
            if not messagebox.askyesno(
                "Delete Fee Item",
                f"Delete {fee_type} for {student_name}?\n\n"
                "This also permanently removes payment records and receipt references linked to this fee.",
                parent=self):
                return
            with db() as conn:
                conn.execute("DELETE FROM student_fee_payments WHERE fee_account_id=?", (account_id,))
                conn.execute("DELETE FROM student_fee_accounts WHERE id=?", (account_id,))
            self.log_activity("FEE_ITEM_DELETED", f"Deleted {fee_type} fee item for {student_name}")
            refresh()

        def add_fee():
            with db() as conn:
                students = conn.execute("SELECT id,name FROM students ORDER BY LOWER(name),id").fetchall()
            if not students:
                messagebox.showinfo("No students", "Register a student before adding a fee item.", parent=self)
                return
            dialog = ctk.CTkToplevel(self)
            dialog.title("Add Student Fee")
            dialog.geometry("500x570")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Add Student Fee", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 5))
            ctk.CTkLabel(dialog, text="Create a fee ledger item for one student.", text_color=MUTED
                         ).pack(anchor="w", padx=24, pady=(0, 12))
            student_options = {f"{sid} · {name}": sid for sid, name in students}
            student_menu = ctk.CTkOptionMenu(dialog, values=list(student_options), width=440)
            student_menu.pack(fill="x", padx=24, pady=7)
            student_menu.set(next(iter(student_options)))
            category_menu = ctk.CTkOptionMenu(dialog,
                values=["Tuition Fee", "Transport Fee", "Hostel Fee", "Others"], width=440)
            category_menu.pack(fill="x", padx=24, pady=7)
            other_category = ctk.CTkEntry(dialog, height=38, placeholder_text="Other fee name (e.g. Library Fee)")
            other_category.pack(fill="x", padx=24, pady=7)
            other_category.pack_forget()
            def toggle_other(value):
                if value == "Others":
                    other_category.pack(fill="x", padx=24, pady=7, after=category_menu)
                else:
                    other_category.pack_forget()
            category_menu.configure(command=toggle_other)
            year_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Academic year (e.g. 2026-27)")
            year_entry.insert(0, f"{date.today().year}-{str(date.today().year + 1)[-2:]}")
            year_entry.pack(fill="x", padx=24, pady=7)
            amount_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Fee amount in INR")
            amount_entry.pack(fill="x", padx=24, pady=7)
            due_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Due date (YYYY-MM-DD, optional)")
            due_entry.pack(fill="x", padx=24, pady=7)
            note_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Notes (optional)")
            note_entry.pack(fill="x", padx=24, pady=7)

            def save():
                fee_type = category_menu.get()
                if fee_type == "Others":
                    fee_type = other_category.get().strip()
                year_value = year_entry.get().strip()
                try:
                    amount = float(amount_entry.get().replace(",", "").replace("₹", "").strip())
                    due_date = due_entry.get().strip()
                    if due_date:
                        due_date = datetime.strptime(due_date, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Check fee details", "Enter a positive amount and a valid due date.", parent=dialog)
                    return
                if not fee_type or not year_value or amount <= 0:
                    messagebox.showerror("Check fee details", "Choose a student, fee category, academic year and positive amount.", parent=dialog)
                    return
                sid = student_options.get(student_menu.get())
                with db() as conn:
                    conn.execute("""INSERT INTO student_fee_accounts
                        (student_id,academic_year,fee_type,total_amount,due_date,notes,created_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (sid, year_value, fee_type, amount, due_date, note_entry.get().strip(),
                         datetime.now().isoformat(timespec="seconds")))
                self.log_activity("FEE_ITEM_ADDED", f"Student {sid}: {fee_type} {amount:.2f} INR for {year_value}")
                dialog.destroy()
                refresh()
            ctk.CTkButton(dialog, text="Save Fee Item", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save).pack(padx=24, pady=14, anchor="e")

        def add_payment(account_id=None):
            today = date.today().isoformat()
            with db() as conn:
                rows = conn.execute("""
                    SELECT fa.id,fa.student_id,s.name,fa.academic_year,fa.fee_type,
                           fa.total_amount,COALESCE(SUM(fp.amount),0)
                    FROM student_fee_accounts fa JOIN students s ON s.id=fa.student_id
                    LEFT JOIN student_fee_payments fp ON fp.fee_account_id=fa.id
                    GROUP BY fa.id ORDER BY LOWER(s.name),fa.academic_year DESC,fa.fee_type
                """).fetchall()
            outstanding = [row for row in rows if float(row[5] or 0)-float(row[6] or 0) > 0.005
                           and (account_id is None or row[0] == account_id)]
            if not outstanding:
                messagebox.showinfo("No balance due", "There are no outstanding fee items to pay.", parent=self)
                return
            dialog = ctk.CTkToplevel(self)
            dialog.title("Record Fee Payment")
            dialog.geometry("500x500")
            dialog.transient(self)
            dialog.grab_set()
            ctk.CTkLabel(dialog, text="Record Fee Payment", font=("Segoe UI", 20, "bold"),
                         text_color=TEXT).pack(anchor="w", padx=24, pady=(22, 10))
            options = {f"{row[2]} · {row[4]} · Due {money(float(row[5])-float(row[6] or 0))}": row
                       for row in outstanding}
            account_menu = ctk.CTkOptionMenu(dialog, values=list(options), width=440)
            account_menu.pack(fill="x", padx=24, pady=7)
            account_menu.set(next(iter(options)))
            amount_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Payment amount in INR")
            amount_entry.pack(fill="x", padx=24, pady=7)
            def default_amount(_value=None):
                record = options.get(account_menu.get())
                if record:
                    amount_entry.delete(0, "end")
                    amount_entry.insert(0, f"{float(record[5])-float(record[6] or 0):.2f}")
            default_amount()
            account_menu.configure(command=default_amount)
            paid_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Payment date (YYYY-MM-DD)")
            paid_entry.insert(0, today)
            paid_entry.pack(fill="x", padx=24, pady=7)
            method_menu = ctk.CTkOptionMenu(dialog, values=["Cash", "UPI", "Bank transfer", "Card", "Cheque", "Other"], width=440)
            method_menu.pack(fill="x", padx=24, pady=7)
            reference_entry = ctk.CTkEntry(dialog, height=38, placeholder_text="Receipt / transaction reference (optional)")
            reference_entry.pack(fill="x", padx=24, pady=7)

            def save():
                row = options.get(account_menu.get())
                try:
                    amount = float(amount_entry.get().replace(",", "").replace("₹", "").strip())
                    paid_on = datetime.strptime(paid_entry.get().strip(), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    messagebox.showerror("Check payment details", "Enter a positive amount and a valid payment date.", parent=dialog)
                    return
                remaining = float(row[5]) - float(row[6] or 0)
                if amount <= 0 or amount > remaining + 0.005:
                    messagebox.showerror("Payment exceeds balance", f"Enter an amount up to {money(remaining)}.", parent=dialog)
                    return
                with db() as conn:
                    conn.execute("""INSERT INTO student_fee_payments
                        (fee_account_id,amount,paid_on,method,reference,recorded_by,recorded_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (row[0], amount, paid_on, method_menu.get(), reference_entry.get().strip(),
                         self.current_username(), datetime.now().isoformat(timespec="seconds")))
                self.log_activity("FEE_PAYMENT_RECORDED", f"Student {row[1]}: {amount:.2f} INR for {row[4]}")
                dialog.destroy()
                refresh()
            ctk.CTkButton(dialog, text="Save Payment", height=40, fg_color=BLUE,
                          text_color="#04111F", command=save).pack(padx=24, pady=14, anchor="e")

        refresh()
        fee_search.bind("<KeyRelease>", lambda _event: refresh())

    # ---------------- BULK STUDENT IMPORT ----------------

    def import_students_file(self):
        """Import student rows from CSV or Excel without overwriting existing students."""
        source=filedialog.askopenfilename(
            parent=self,title="Select student CSV or Excel file",
            filetypes=[("Student files","*.csv *.xlsx"),("CSV files","*.csv"),("Excel files","*.xlsx")])
        if not source:
            return
        path=Path(source)
        try:
            if path.suffix.lower()==".csv":
                # utf-8-sig handles CSV files saved by Excel with a BOM.
                with path.open("r",encoding="utf-8-sig",newline="") as handle:
                    rows=list(csv.DictReader(handle))
            elif path.suffix.lower()==".xlsx":
                try:
                    from openpyxl import load_workbook
                except ImportError:
                    messagebox.showerror("Excel support missing","Install Excel support with:\npython -m pip install openpyxl",parent=self)
                    return
                workbook=load_workbook(path,read_only=True,data_only=True)
                sheet=workbook.active
                values=sheet.iter_rows(values_only=True)
                headers=next(values,None)
                if not headers:
                    rows=[]
                else:
                    keys=[str(value or "").strip() for value in headers]
                    rows=[dict(zip(keys,row)) for row in values]
                workbook.close()
            else:
                messagebox.showerror("Unsupported file","Choose a .csv or .xlsx file.",parent=self)
                return
            if not rows:
                messagebox.showwarning("No data","The selected file has no student rows.",parent=self)
                return

            # Header matching is case-insensitive; required columns are ID and Name.
            aliases={"student id":"id","student_id":"id","roll no":"id","roll number":"id",
                     "full name":"name","student name":"name","academic year":"year",
                     "dept":"department","class":"class_name","section":"class_name","class / section":"class_name","student email":"email","student email address":"email","email address":"email","parent email address":"parent_email"}
            normalized=[]
            for raw in rows:
                item={}
                for key,value in raw.items():
                    clean=str(key or "").strip().lower()
                    clean=aliases.get(clean,clean.replace(" ","_"))
                    item[clean]="" if value is None else str(value).strip()
                normalized.append(item)

            added=0; skipped=0; errors=[]; seen=set()
            with db() as conn:
                for line_number,item in enumerate(normalized,start=2):
                    raw_id=item.get("id","").strip()
                    name=item.get("name","").strip()
                    if not raw_id or not raw_id.isdigit() or not name:
                        skipped+=1
                        if len(errors)<8: errors.append(f"Row {line_number}: ID must be numeric and Name is required.")
                        continue
                    sid=int(raw_id)
                    if sid in seen:
                        skipped+=1
                        if len(errors)<8: errors.append(f"Row {line_number}: duplicate ID {sid} in file.")
                        continue
                    seen.add(sid)
                    year=item.get("year","").strip()
                    department=item.get("department","").strip().upper()
                    email=item.get("email","").strip()
                    parent_email=item.get("parent_email","").strip()
                    student_class=item.get("class_name","").strip().upper()
                    exists=conn.execute("SELECT 1 FROM students WHERE id=?",(sid,)).fetchone()
                    if exists:
                        skipped+=1
                        if len(errors)<8: errors.append(f"Row {line_number}: ID {sid} already exists; not overwritten.")
                        continue
                    conn.execute("INSERT INTO students(id,name,year,department,class_name,email,parent_email) VALUES(?,?,?,?,?,?,?)",
                                 (sid,name,year,department,student_class,email,parent_email))
                    added+=1
            self.log_activity("STUDENT_BULK_IMPORT",f"Imported {added} students from {path.name}; skipped {skipped}")
            self.show_students()
            details=f"Successfully imported: {added}\nSkipped: {skipped}"
            if errors:
                details+="\n\nFirst issues:\n"+"\n".join(errors)
            messagebox.showinfo("Student import completed",details,parent=self)
        except Exception as error:
            messagebox.showerror("Import failed",f"Could not import this file:\n{error}",parent=self)

    # ---------------- STUDENT DIRECTORY ----------------

    def show_students(self):
        self.clear_content()
        self.select_nav("Students")
        self.header("Students", "Manage your registered student records")

        toolbar = ctk.CTkFrame(self.content, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        toolbar.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        search = ctk.CTkEntry(toolbar, height=36, fg_color=WHITE, border_color=BORDER,
                              placeholder_text="Search student ID or name...")
        search.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        year_filter = ctk.CTkOptionMenu(toolbar, values=YEARS, width=125, height=36,
                                        command=lambda _v: render())
        year_filter.grid(row=0, column=1, padx=4)
        dept_filter = ctk.CTkOptionMenu(toolbar, values=DEPARTMENTS, width=155, height=36,
                                        command=lambda _v: render())
        dept_filter.grid(row=0, column=2, padx=4)
        sort_menu = ctk.CTkOptionMenu(toolbar, values=["ID", "Year", "Department", "Name"],
                                      width=110, height=36, command=lambda _v: render())
        sort_menu.grid(row=0, column=3, padx=4)
        ctk.CTkButton(toolbar, text="Import CSV / Excel", width=150, height=36,
                      fg_color="#1C3554", command=self.import_students_file).grid(row=0, column=4,padx=(4,4))
        ctk.CTkButton(toolbar, text="+ Add Student", width=135, height=36,
                      fg_color=BLUE, command=self.open_add_student).grid(row=0, column=5)

        panel = self.white_panel(self.content)
        panel.grid(row=2, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(panel, text="Student Directory", font=("Segoe UI", 16, "bold"),
                     text_color=TEXT).grid(row=0, column=0, sticky="w", padx=20, pady=16)
        table = FastScrollFrame(panel, fg_color="transparent")
        table.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        weights = [1, 3, 2, 2, 2, 3]
        order_by = {"ID": "id", "Name": "LOWER(name), id",
                    "Year": "year, id", "Department": "department, id"}
        pending = {"id": None}

        def render():
            # Filtering and sorting happen in SQL, not by loading every row into Python.
            clauses, params = [], []
            query = search.get().strip().lower()
            if query:
                clauses.append(
                    "(CAST(id AS TEXT) || ' ' || LOWER(name) || ' ' || "
                    "LOWER(COALESCE(year,'')) || ' ' || LOWER(COALESCE(department,''))) LIKE ?")
                params.append(f"%{query}%")
            if year_filter.get() != "All Years":
                clauses.append("year = ?")
                params.append(year_filter.get())
            if dept_filter.get() != "All Departments":
                clauses.append("UPPER(department) = ?")
                params.append(dept_filter.get().upper())
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            with db() as conn:
                rows = conn.execute(
                    f"SELECT id, name, COALESCE(year,''), COALESCE(department,'') "
                    f"FROM students {where} ORDER BY {order_by[sort_menu.get()]} "
                    f"LIMIT {MAX_ROWS_SHOWN + 1}", params).fetchall()

            self.clear_children(table)
            self.table_header(table, ["STUDENT ID", "NAME", "YEAR", "DEPARTMENT",
                                      "FACE PHOTO", "ACTIONS"], weights, "stu")
            if not rows:
                self.empty_message(table, "No matching student records.")
                return

            truncated = len(rows) > MAX_ROWS_SHOWN
            rows = rows[:MAX_ROWS_SHOWN]
            with_photo = photo_student_ids()

            def build(i, rec):
                sid, name, student_year, department = rec
                has_photo = sid in with_photo
                row = self.table_row(
                    table, i,
                    [sid, name, student_year or "-", department or "-",
                     "Captured" if has_photo else "Not captured", None],
                    weights, "stu", colors={0: BLUE, 4: GREEN if has_photo else MUTED},
                    shade=False)
                actions = tk.Frame(row, bg=WHITE, bd=0, highlightthickness=0)
                actions.grid(row=0, column=5, sticky="w", padx=self._px(8), pady=self._px(5))
                self.mini_button(actions, "Edit", "#203754", "#2A4668", TEXT,
                                 lambda s=sid: self.open_edit_student(s)
                                 ).pack(side="left", padx=(0, self._px(6)))
                self.mini_button(actions, "Delete", "#4B2032", "#64263C", RED,
                                 lambda s=sid, n=name: self.delete_student(s, n)
                                 ).pack(side="left")
                self.mini_button(actions, "Analyze", "#123F45", "#15525A", GREEN,
                                 lambda s=sid: self.show_student_analytics(s)
                                 ).pack(side="left", padx=(6, 0))

            self.fill_batched(table, rows, build)

            if truncated:
                ctk.CTkLabel(
                    table, text=f"Showing the first {MAX_ROWS_SHOWN} matches. Refine your search to see more.",
                    text_color=MUTED).grid(row=len(rows) + 1, column=0, pady=10)

        def on_key(_event):
            # Debounce: wait for a pause in typing instead of re-rendering per keystroke.
            if pending["id"] is not None:
                self.after_cancel(pending["id"])
            pending["id"] = self.after(220, render)

        search.bind("<KeyRelease>", on_key)
        render()

    def delete_student(self, student_id, student_name):
        if not self.require_permission("delete_student"):
            return
        if not messagebox.askyesno(
            "Delete Student",
            f"Delete {student_name} (ID: {student_id})?\n\n"
            "This will permanently delete the student's record, attendance history, "
            "and saved face photos. This action cannot be undone.", parent=self):
            return
        try:
            with db() as conn:
                conn.execute("DELETE FROM attendance WHERE student_id = ?", (student_id,))
                conn.execute("DELETE FROM performance_records WHERE student_id = ?", (student_id,))
                conn.execute("DELETE FROM leaves WHERE student_id = ?", (student_id,))
                conn.execute(
                    "DELETE FROM student_fee_payments WHERE fee_account_id IN "
                    "(SELECT id FROM student_fee_accounts WHERE student_id = ?)",
                    (student_id,))
                conn.execute("DELETE FROM student_fee_accounts WHERE student_id = ?", (student_id,))
                cursor = conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
                if cursor.rowcount == 0:
                    messagebox.showwarning("Student Not Found",
                                           "This student record no longer exists.", parent=self)
                    self.show_students()
                    return

            self.log_activity("STUDENT_DELETE", f"Deleted student {student_id} ({student_name})")
            deleted = 0
            for photo in IMAGE_DIR.glob(f"{student_id}_*"):
                if photo.suffix.lower() in IMAGE_EXT:
                    try:
                        photo.unlink()
                        deleted += 1
                    except OSError:
                        pass
            self.show_students()
            messagebox.showinfo(
                "Student Deleted",
                f"Student {student_name} (ID: {student_id}) and their related records were deleted.\n"
                f"Removed {deleted} face photo(s).", parent=self)
        except sqlite3.Error as error:
            messagebox.showerror("Database Error", f"Could not delete the student:\n{error}", parent=self)

    # ---------------- FACE CAPTURE (shared by register + recapture) ----------------

    def capture_face_samples(self, student_id, tag, parent):
        """Open the webcam, capture one close-up crop + one full frame.
        Returns the list of saved paths (empty if cancelled or failed)."""
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        camera = open_camera()
        if camera is None:
            messagebox.showerror("Camera Error",
                                 "Cannot open webcam. Check camera permissions.", parent=parent)
            return []

        window = "AttendIQ Face Capture"
        crop = full = None
        hint = "S: Capture | Q: Cancel"
        try:
            while True:
                ok, frame = camera.read()
                if not ok:
                    break
                frame = cv2.flip(frame, 1)
                clean = frame.copy()
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(100, 100))
                for x, y, w, h in faces:
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (40, 210, 120), 2)
                cv2.putText(frame, hint, (15, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.65, (255, 255, 255), 2)
                cv2.imshow(window, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break  # window closed with the X button
                if key != ord("s"):
                    continue
                if len(faces) != 1:
                    hint = "Exactly one face must be visible"
                    continue
                x, y, w, h = faces[0]
                pad_x, pad_y = int(w * 0.30), int(h * 0.35)
                x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
                x2 = min(clean.shape[1], x + w + pad_x)
                y2 = min(clean.shape[0], y + h + pad_y)
                candidate = clean[y1:y2, x1:x2].copy()
                if face_recognition is not None:
                    try:
                        rgb = cv2.cvtColor(candidate, cv2.COLOR_BGR2RGB)
                        if not face_recognition.face_encodings(rgb, num_jitters=1):
                            hint = "Face unclear - move closer, face camera"
                            continue
                    except Exception as error:
                        hint = "Quality check failed - try again"
                        print(f"Face quality check failed: {error}")
                        continue
                crop, full = candidate, clean
                break
        finally:
            camera.release()
            cv2.destroyAllWindows()

        if crop is None:
            return []

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved = []
        for path, img in ((IMAGE_DIR / f"{student_id}_{tag}_{stamp}.jpg", crop),
                          (IMAGE_DIR / f"{student_id}_{tag}_full_{stamp}.jpg", full)):
            if cv2.imwrite(str(path), img):
                saved.append(path)
        if not saved:
            messagebox.showerror("Save Error", "Could not save the face photo.", parent=parent)
        return saved

    # ---------------- EDIT STUDENT DETAILS ----------------

    def open_edit_student(self, student_id):
        with db() as conn:
            record = conn.execute(
                "SELECT id, name, COALESCE(year,''), COALESCE(department,''), COALESCE(class_name,''), COALESCE(email,''), "
                "COALESCE(parent_email,'') FROM students WHERE id = ?", (student_id,)).fetchone()
        if not record:
            messagebox.showerror("Student Not Found",
                                 "This student record no longer exists.", parent=self)
            self.show_students()
            return

        sid, current_name, current_year, current_department, current_class, current_email, current_parent = record
        dialog = ctk.CTkToplevel(self)
        dialog.title("Edit Student Details")
        dialog.geometry("440x640")
        dialog.resizable(False, False)
        dialog.configure(fg_color=BG)
        dialog.transient(self)
        dialog.grab_set()
        form_area = ctk.CTkScrollableFrame(dialog, fg_color=BG, corner_radius=0)
        form_area.pack(fill="both", expand=True, padx=4, pady=4)

        ctk.CTkLabel(form_area, text="Edit Student Details", font=("Segoe UI", 22, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=25, pady=(24, 6))
        ctk.CTkLabel(form_area, text=f"Student ID: {sid} (cannot be changed)",
                     text_color=MUTED).pack(anchor="w", padx=25, pady=(0, 16))

        def field(label, value, placeholder="", pad=10, height=38):
            ctk.CTkLabel(form_area, text=label, text_color=TEXT).pack(anchor="w", padx=25, pady=(pad, 5))
            entry = ctk.CTkEntry(form_area, height=height, fg_color=WHITE, placeholder_text=placeholder)
            entry.pack(fill="x", padx=25)
            entry.insert(0, value)
            return entry

        name_entry = field("Full Name", current_name, pad=0, height=40)
        year_entry = field("Academic Year", current_year, "e.g. 3rd Year")
        dept_entry = field("Department", current_department, "e.g. CSE")
        class_entry = field("Class / Section", current_class, "e.g. A")
        email_entry = field("Student Email Address", current_email, "student@example.com")
        parent_entry = field("Parent Email Address", current_parent, "parent@example.com")

        def save_changes():
            new_name = name_entry.get().strip()
            if not new_name:
                messagebox.showwarning("Missing Name", "Enter the student's full name.", parent=dialog)
                return
            try:
                with db() as conn:
                    conn.execute(
                        "UPDATE students SET name = ?, year = ?, department = ?, class_name = ?, email = ?, parent_email = ? WHERE id = ?",
                        (new_name, year_entry.get().strip(), dept_entry.get().strip().upper(),
                         class_entry.get().strip().upper(), email_entry.get().strip(), parent_entry.get().strip(), sid))
                self.log_activity("STUDENT_EDIT", f"Updated student {sid} ({new_name})")
                dialog.destroy()
                self.show_students()
                messagebox.showinfo("Updated", f"Student {sid}'s details were updated.", parent=self)
            except sqlite3.Error as error:
                messagebox.showerror("Database Error", str(error), parent=dialog)

        def recapture():
            if self.capture_face_samples(sid, "recapture", dialog):
                messagebox.showinfo(
                    "Face Photo Updated",
                    "A face-focused sample was saved. It is picked up automatically the next "
                    "time you start Face Recognition.\n\nFor best accuracy, add 2-3 samples per "
                    "student in bright light, looking straight ahead and slightly left/right.",
                    parent=dialog)

        ctk.CTkButton(form_area, text="Recapture Face Photo", height=38, fg_color=GREEN,
                      hover_color="#116C56", command=recapture
                      ).pack(fill="x", padx=25, pady=(14, 8))
        ctk.CTkButton(form_area, text="Save Student Changes", height=44, fg_color=BLUE,
                      command=save_changes).pack(fill="x", padx=25, pady=(0, 8))
        ctk.CTkButton(form_area, text="Cancel", height=36, fg_color="transparent",
                      text_color=TEXT, command=dialog.destroy).pack(fill="x", padx=25, pady=(0, 16))

    # ---------------- ADD STUDENT ----------------

    def open_add_student(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Register Student")
        dialog.geometry("440x700")
        dialog.resizable(False, False)
        dialog.configure(fg_color=BG)
        dialog.transient(self)
        dialog.grab_set()
        form_area = ctk.CTkScrollableFrame(dialog, fg_color=BG, corner_radius=0)
        form_area.pack(fill="both", expand=True, padx=4, pady=4)

        ctk.CTkLabel(form_area, text="Register New Student", font=("Segoe UI", 23, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=25, pady=(24, 6))
        ctk.CTkLabel(form_area, text="Enter the student's details.",
                     text_color=MUTED).pack(anchor="w", padx=25, pady=(0, 18))

        def field(label, placeholder, pad=12):
            ctk.CTkLabel(form_area, text=label, text_color=TEXT).pack(anchor="w", padx=25, pady=(pad, 5))
            entry = ctk.CTkEntry(form_area, height=40, placeholder_text=placeholder, fg_color=WHITE)
            entry.pack(fill="x", padx=25)
            return entry

        id_entry = field("Student ID (numeric)", "Example: 101", pad=0)
        name_entry = field("Full Name", "Student's full name")
        year_entry = field("Academic Year", "e.g. 3rd Year")
        dept_entry = field("Department", "e.g. CSE")
        class_entry = field("Class / Section", "e.g. A")
        email_entry = field("Student Email Address (optional)", "student@example.com")
        parent_entry = field("Parent Email Address (optional)", "parent@example.com")

        def register(capture):
            raw_id = id_entry.get().strip()
            name = name_entry.get().strip()
            student_year = year_entry.get().strip()
            department = dept_entry.get().strip().upper()
            student_class = class_entry.get().strip().upper()

            if not raw_id.isdigit() or not name or not student_year or not department:
                messagebox.showwarning(
                    "Missing Details",
                    "Enter a numeric ID, full name, academic year, and department.", parent=dialog)
                return
            sid = int(raw_id)
            with db() as conn:
                exists = conn.execute("SELECT 1 FROM students WHERE id = ?", (sid,)).fetchone()
            if exists:
                messagebox.showerror("Duplicate ID", "This student ID already exists.", parent=dialog)
                return

            saved = []
            if capture:
                safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "student"
                dialog.withdraw()
                try:
                    saved = self.capture_face_samples(sid, safe_name, dialog)
                finally:
                    dialog.deiconify()
                if not saved:
                    return

            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO students (id, name, year, department, class_name, email, parent_email) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (sid, name, student_year, department, student_class, email_entry.get().strip(),
                         parent_entry.get().strip()))
                self.log_activity("STUDENT_ADD", f"Registered student {sid} ({name})")
            except sqlite3.Error as error:
                for path in saved:  # roll back photos if the record could not be saved
                    path.unlink(missing_ok=True)
                messagebox.showerror("Database Error", str(error), parent=dialog)
                return

            dialog.destroy()
            self.show_students()
            messagebox.showinfo("Success", f"{name} was registered successfully.", parent=self)

        ctk.CTkButton(form_area, text="Save Student + Capture Face", height=43, fg_color=BLUE,
                      command=lambda: register(True)).pack(fill="x", padx=25, pady=(20, 8))
        ctk.CTkButton(form_area, text="Save Student", height=42, fg_color="#203754",
                      text_color=TEXT, hover_color="#2A4668",
                      command=lambda: register(False)).pack(fill="x", padx=25, pady=(0, 8))
        ctk.CTkButton(form_area, text="Cancel", fg_color="transparent", text_color=TEXT,
                      command=dialog.destroy).pack(fill="x", padx=25, pady=(0, 15))

    # ---------------- TAKE ATTENDANCE ----------------

    def show_attendance(self):
        self.clear_content()
        self.select_nav("Attendance")
        self.header("Attendance Management", "Mark daily attendance and save it to your database")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(self.content, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkLabel(toolbar, text="Attendance date:", font=("Segoe UI", 12, "bold"),
                     text_color=TEXT).grid(row=0, column=0, padx=(0, 6))
        date_entry = ctk.CTkEntry(toolbar, width=140, height=36, fg_color=WHITE, border_color=BORDER)
        date_entry.insert(0, date.today().isoformat())
        date_entry.grid(row=0, column=1, sticky="w", padx=(0, 6))
        with db() as conn:
            sessions = conn.execute("SELECT id,subject,period,COALESCE(academic_year,''),COALESCE(department,''),COALESCE(class_name,'') FROM timetable WHERE active=1 ORDER BY academic_year,department,class_name,id").fetchall()
            student_meta = {sid: (year or "", department or "") for sid, year, department in
                            conn.execute("SELECT id,year,department FROM students").fetchall()}
        year_filter = ctk.CTkOptionMenu(toolbar,
            values=["All Years"] + sorted({v[0] for v in student_meta.values() if v[0]}), width=130,
            command=lambda _value: load_day())
        year_filter.grid(row=0, column=4, padx=4)
        department_filter = ctk.CTkOptionMenu(toolbar,
            values=["All Departments"] + sorted({v[1] for v in student_meta.values() if v[1]}), width=145,
            command=lambda _value: load_day())
        department_filter.grid(row=0, column=5, padx=4)
        session_map = {f"Daily (no period)" : None}
        session_map.update({f"{subj} - {per} | {yr} {dept}-{cls}": sid for sid,subj,per,yr,dept,cls in sessions})
        session_menu = ctk.CTkOptionMenu(toolbar, values=list(session_map.keys()), width=190)
        session_menu.grid(row=0,column=3,padx=8)

        panel = self.white_panel(self.content)
        panel.grid(row=2, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(panel, text="Daily Register", font=("Segoe UI", 17, "bold"),
                     text_color=TEXT).grid(row=0, column=0, sticky="w", padx=20, pady=16)
        search_entry = ctk.CTkEntry(panel, height=34, placeholder_text="Search student ID or name…")
        search_entry.grid(row=0, column=1, sticky="e", padx=16, pady=10)
        table = FastScrollFrame(panel, fg_color="transparent")
        table.grid(row=1, column=0, sticky="nsew", padx=12)
        footer = ctk.CTkFrame(panel, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=16, pady=14)
        footer.grid_columnconfigure(0, weight=1)

        weights = [1, 3, 2]
        state = {}    # Student ID -> Present / Absent / Not Mentioned
        pills = {}    # student id -> label widget (only for rows already drawn)

        def paint(sid):
            pill = pills.get(sid)
            if pill is not None:
                status = state[sid]
                styles = {
                    "Present": ("\u2714  Present", GREEN),
                    "Absent": ("\u2716  Absent", RED),
                    "Not Mentioned": ("\u2014  Not Mentioned", "#64748B"),
                }
                label, color = styles.get(status, styles["Not Mentioned"])
                pill.configure(text=label, bg=color)

        def toggle(sid):
            order = ("Not Mentioned", "Present", "Absent")
            current = state.get(sid, "Not Mentioned")
            state[sid] = order[(order.index(current) + 1) % len(order)]
            paint(sid)

        def load_day():
            day = self.valid_date(date_entry.get().strip())
            if not day:
                messagebox.showerror("Invalid Date", "Use date format YYYY-MM-DD.")
                return
            self.clear_children(table)
            state.clear()
            pills.clear()
            self.table_header(table, ["STUDENT ID", "STUDENT NAME", "ATTENDANCE STATUS"],
                              weights, "att")
            records = get_attendance(day, session_map.get(session_menu.get()))
            on_leave = self.students_on_leave(day)
            if not records:
                self.empty_message(table, "No students registered yet.")
                return
            # State is filled for EVERY student immediately, so Save / Mark All
            # work even while the rows are still being drawn in the background.
            for sid, _name, status, _t in records:
                state[sid] = status if status in ("Present", "Absent", "Not Mentioned") else "Not Mentioned"
            query = search_entry.get().strip().lower()
            visible_records = [rec for rec in records
                if (not query or query in str(rec[0]).lower() or query in rec[1].lower())
                and (year_filter.get() == "All Years" or student_meta.get(rec[0], ("", ""))[0] == year_filter.get())
                and (department_filter.get() == "All Departments" or student_meta.get(rec[0], ("", ""))[1].upper() == department_filter.get().upper())]

            def build_att(index, rec):
                sid, name, _status, _t = rec
                row = self.table_row(table, index,
                                     [sid, name + ("  (on leave)" if sid in on_leave else ""), None],
                                     weights, "att", colors={0: BLUE}, shade=False)
                pill = tk.Label(row, fg="#04111F", cursor="hand2", anchor="center",
                                font=self._font(11, "bold"), width=12,
                                padx=self._px(6), pady=self._px(6))
                pill.grid(row=0, column=2, sticky="w", padx=self._px(12), pady=self._px(4))
                pill.bind("<Button-1>", lambda _e, s=sid: toggle(s))
                pills[sid] = pill
                paint(sid)
            self.fill_batched(table, visible_records, build_att)

        def mark_all(status):
            for sid in state:
                state[sid] = status
                paint(sid)

        def save_attendance():
            day = self.valid_date(date_entry.get().strip())
            if not day:
                messagebox.showerror("Invalid Date", "Use date format YYYY-MM-DD.")
                return
            if not state:
                messagebox.showwarning("No Students", "There are no student records to save.")
                return
            if not messagebox.askyesno(
                "Save Attendance",
                f"Save attendance for {day}?\n\nExisting attendance for this date will be replaced."):
                return
            stamp = datetime.now().strftime("%H:%M:%S")
            try:
                selected_session = session_map.get(session_menu.get())
                with db() as conn:
                    if selected_session is None:
                        conn.executemany(
                            "INSERT INTO attendance (student_id, date, time, status, session_id) VALUES (?, ?, ?, ?, NULL) "
                            "ON CONFLICT(student_id, date) WHERE session_id IS NULL DO UPDATE SET status=excluded.status,time=excluded.time",
                            [(sid, day, stamp, status) for sid, status in state.items()])
                        saved_rows = conn.execute("SELECT id,student_id,date,time,status FROM attendance WHERE date=? AND session_id IS NULL",(day,)).fetchall()
                    else:
                        conn.executemany(
                            "INSERT INTO attendance (student_id,date,time,status,session_id) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(student_id,date,session_id) WHERE session_id IS NOT NULL DO UPDATE SET status=excluded.status,time=excluded.time",
                            [(sid,day,stamp,status,selected_session) for sid,status in state.items()])
                        saved_rows = conn.execute("SELECT id,student_id,date,time,status FROM attendance WHERE date=? AND session_id=?",(day,selected_session)).fetchall()
                security_errors = []
                for attendance_id, sid, saved_day, saved_time, saved_status in saved_rows:
                    try:
                        self.secure_attendance_record(
                            attendance_id, sid, saved_day, saved_time, saved_status)
                    except Exception as security_error:
                        security_errors.append(str(security_error))
                self.log_activity("MANUAL_ATTENDANCE_SAVE", f"Saved {len(saved_rows)} attendance rows for {day}, session {selected_session}")
                if security_errors:
                    messagebox.showwarning(
                        "Attendance Saved - Security Warning",
                        "Attendance was saved, but one or more security snapshots could not be recorded:\n" +
                        "\n".join(security_errors[:3]))
                else:
                    messagebox.showinfo("Attendance Saved", f"Attendance saved and secured for {day}.")
                self.show_attendance()
            except sqlite3.Error as error:
                messagebox.showerror("Database Error", str(error))

        ctk.CTkButton(toolbar, text="Load Date / Period", height=36, fg_color=BLUE, text_color="#04111F",
                      command=load_day).grid(row=0, column=2)
        ctk.CTkButton(footer, text="All Not Mentioned", width=145, fg_color="#64748B",
                      hover_color="#475569", command=lambda: mark_all("Not Mentioned")
                      ).grid(row=0, column=1, padx=5)
        ctk.CTkButton(footer, text="Mark All Present", width=145, fg_color=GREEN,
                      hover_color="#116C56", command=lambda: mark_all("Present")
                      ).grid(row=0, column=2, padx=5)
        ctk.CTkButton(footer, text="Mark All Absent", width=145, fg_color=RED,
                      hover_color="#A73737", command=lambda: mark_all("Absent")
                      ).grid(row=0, column=3, padx=5)
        ctk.CTkButton(footer, text="Save Attendance", width=150, fg_color=BLUE,
                      hover_color="#2856C6", font=("Segoe UI", 12, "bold"),
                      command=save_attendance).grid(row=0, column=4, padx=5)
        search_entry.bind("<KeyRelease>", lambda _event: load_day())
        load_day()

    # ---------------- DAY-WISE OVERVIEW ----------------

    def show_daywise_overview(self):
        self.clear_content()
        self.select_nav("Day-wise Overview")
        self.header("Day-wise Attendance Overview",
                    "Select a date to review daily attendance and each student's attendance percentage")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(self.content, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(toolbar, text="Selected date:", font=("Segoe UI", 12, "bold"),
                     text_color=TEXT).pack(side="left", padx=(0, 8))
        date_entry = ctk.CTkEntry(toolbar, width=145, height=36, fg_color=WHITE, border_color=BORDER)
        date_entry.insert(0, date.today().isoformat())
        date_entry.pack(side="left", padx=(0, 8))
        search_entry = ctk.CTkEntry(toolbar, width=240, height=36,
                                    placeholder_text="Search student ID or name…")
        search_entry.pack(side="left", padx=(0, 8))
        with db() as conn:
            student_meta = {sid: (year or "", department or "") for sid, year, department in
                            conn.execute("SELECT id,year,department FROM students").fetchall()}
        year_filter = ctk.CTkOptionMenu(toolbar,
            values=["All Years"] + sorted({v[0] for v in student_meta.values() if v[0]}), width=130,
            command=lambda _value: load_overview())
        year_filter.pack(side="left", padx=4)
        department_filter = ctk.CTkOptionMenu(toolbar,
            values=["All Departments"] + sorted({v[1] for v in student_meta.values() if v[1]}), width=145,
            command=lambda _value: load_overview())
        department_filter.pack(side="left", padx=4)

        panel = self.white_panel(self.content)
        panel.grid(row=2, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        summary_frame = ctk.CTkFrame(panel, fg_color="transparent")
        summary_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(15, 8))
        summary_labels = {}
        for col, (key, title, color) in enumerate([
            ("students", "TOTAL STUDENTS", BLUE), ("present", "PRESENT TODAY", GREEN),
            ("absent", "ABSENT TODAY", RED), ("unmarked", "NOT MARKED", AMBER),
        ]):
            summary_frame.grid_columnconfigure(col, weight=1)
            card = ctk.CTkFrame(summary_frame, fg_color="#F3F6FB", corner_radius=9)
            card.grid(row=0, column=col, sticky="ew", padx=4)
            ctk.CTkLabel(card, text=title, font=("Segoe UI", 9, "bold"),
                         text_color=MUTED).pack(anchor="w", padx=10, pady=(9, 1))
            summary_labels[key] = ctk.CTkLabel(card, text="0", font=("Segoe UI", 21, "bold"),
                                               text_color=color)
            summary_labels[key].pack(anchor="w", padx=10, pady=(0, 8))

        selected_label = ctk.CTkLabel(panel, text="", font=("Segoe UI", 13, "bold"), text_color=TEXT)
        selected_label.grid(row=1, column=0, sticky="w", padx=20, pady=(5, 8))
        table = FastScrollFrame(panel, fg_color="transparent")
        table.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

        weights = [1, 3, 2, 2, 3]

        def load_overview():
            day = self.valid_date(date_entry.get().strip())
            if not day:
                messagebox.showerror("Invalid Date", "Use date format YYYY-MM-DD.")
                return
            date_entry.delete(0, "end")
            date_entry.insert(0, day)
            try:
                with db() as conn:
                    records = conn.execute(f"""
                        SELECT s.id, s.name, today.status,
                               COALESCE(h.present_count, 0), COALESCE(h.marked_count, 0)
                        FROM students s
                        LEFT JOIN attendance today
                            ON today.student_id = s.id AND today.date = ?
                        LEFT JOIN (
                            SELECT student_id,
                                   SUM(status = 'Present') AS present_count,
                                   COUNT(*) AS marked_count
                            FROM attendance a WHERE a.date <= ? AND NOT {EXCUSED_SQL}
                            GROUP BY student_id
                        ) h ON h.student_id = s.id
                        ORDER BY s.id
                    """, (day, day)).fetchall()
            except sqlite3.Error as error:
                messagebox.showerror("Database Error", str(error))
                return

            total = len(records)
            present = sum(1 for r in records if r[2] == "Present")
            absent = sum(1 for r in records if r[2] == "Absent")
            for key, value in (("students", total), ("present", present),
                               ("absent", absent), ("unmarked", total - present - absent)):
                summary_labels[key].configure(text=str(value))
            selected_label.configure(
                text=f"Attendance for {datetime.strptime(day, '%Y-%m-%d').strftime('%A, %d %B %Y')}")

            self.clear_children(table)
            self.table_header(table, ["STUDENT ID", "STUDENT NAME", "SELECTED DAY",
                                      "PRESENT DAYS", "ATTENDANCE %"], weights, "day")
            if not records:
                self.empty_message(table, "No students registered yet.")
                return
            excused_today = self.students_on_leave(day)
            query = search_entry.get().strip().lower()
            visible_records = [rec for rec in records
                               if not query or query in str(rec[0]).lower()
                               or query in rec[1].lower()]
            visible_records = [rec for rec in visible_records
                if (year_filter.get() == "All Years" or student_meta.get(rec[0], ("", ""))[0] == year_filter.get())
                and (department_filter.get() == "All Departments" or student_meta.get(rec[0], ("", ""))[1].upper() == department_filter.get().upper())]

            def build_day(index, rec):
                sid, name, status, present_count, marked_count = rec
                pct = present_count / marked_count * 100 if marked_count else 0.0
                day_status = status if status in ("Present", "Absent", "Not Mentioned") else "Not Marked"
                if day_status == "Absent" and sid in excused_today:
                    day_status = "Excused"
                status_color = {"Present": GREEN, "Absent": RED}.get(day_status, AMBER)
                pct_color = TEXT if not marked_count else (GREEN if pct >= 75 else RED)
                self.table_row(
                    table, index,
                    [sid, name, day_status, f"{present_count}/{marked_count}", f"{pct:.1f}%"],
                    weights, "day", colors={2: status_color, 4: pct_color}, bold=(2, 4))
            self.fill_batched(table, visible_records, build_day)

        def shift_day(offset):
            day = self.valid_date(date_entry.get().strip())
            if not day:
                messagebox.showerror("Invalid Date", "Use date format YYYY-MM-DD.")
                return
            new_day = datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=offset)
            date_entry.delete(0, "end")
            date_entry.insert(0, new_day.isoformat())
            load_overview()

        ctk.CTkButton(toolbar, text="◀ Previous Day", width=112, height=36, fg_color="#203754",
                      text_color=TEXT, hover_color="#2A4668", command=lambda: shift_day(-1)
                      ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(toolbar, text="Next Day ▶", width=105, height=36, fg_color="#203754",
                      text_color=TEXT, hover_color="#2A4668", command=lambda: shift_day(1)
                      ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(toolbar, text="Load Date", width=100, height=36, fg_color=BLUE,
                      command=load_overview).pack(side="left")
        ctk.CTkLabel(
            panel,
            text="Attendance % = Present days ÷ recorded attendance days up to the selected date. "
                 "Not Marked means no record exists for that date.",
            font=("Segoe UI", 10), text_color=MUTED, wraplength=900, justify="left"
        ).grid(row=3, column=0, sticky="w", padx=18, pady=(0, 12))
        search_entry.bind("<KeyRelease>", lambda _event: load_overview())
        load_overview()

    # ---------------- DATE RANGE ANALYSIS ----------------

    def show_date_range_analysis(self):
        self.clear_content()
        self.select_nav("Date Range Analysis")
        self.header("Date Range Analysis",
                    "Filter by academic year and department, then calculate attendance between two dates")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        filters = ctk.CTkFrame(self.content, fg_color="transparent")
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(filters, text="From", text_color=TEXT).pack(side="left", padx=(0, 5))
        from_entry = ctk.CTkEntry(filters, width=125, height=36, fg_color=WHITE)
        from_entry.insert(0, date.today().replace(day=1).isoformat())
        from_entry.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(filters, text="To", text_color=TEXT).pack(side="left", padx=(0, 5))
        to_entry = ctk.CTkEntry(filters, width=125, height=36, fg_color=WHITE)
        to_entry.insert(0, date.today().isoformat())
        to_entry.pack(side="left", padx=(0, 12))
        year_menu = ctk.CTkOptionMenu(filters, values=YEARS, width=125)
        year_menu.pack(side="left", padx=4)
        dept_menu = ctk.CTkOptionMenu(filters, values=DEPARTMENTS, width=155)
        dept_menu.pack(side="left", padx=4)

        panel = self.white_panel(self.content)
        panel.grid(row=2, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        summary = ctk.CTkLabel(panel, text="Select a date range and click Calculate.",
                               text_color=TEXT, font=("Segoe UI", 13, "bold"))
        summary.grid(row=0, column=0, sticky="w", padx=18, pady=14)
        table = FastScrollFrame(panel, fg_color="transparent")
        table.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        weights = [1, 3, 2, 2, 2, 2]

        def calculate():
            start = self.valid_date(from_entry.get().strip())
            end = self.valid_date(to_entry.get().strip())
            if not start or not end:
                messagebox.showerror("Invalid Date", "Enter dates in YYYY-MM-DD format.", parent=self)
                return
            if start > end:
                messagebox.showerror("Invalid Range",
                                     "The From date must be on or before the To date.", parent=self)
                return

            clauses, params = [], [start, end]
            if year_menu.get() != "All Years":
                clauses.append("s.year = ?")
                params.append(year_menu.get())
            if dept_menu.get() != "All Departments":
                clauses.append("UPPER(s.department) = ?")
                params.append(dept_menu.get().upper())
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            with db() as conn:
                rows = conn.execute(f"""
                    SELECT s.id, s.name, COALESCE(s.year,''), COALESCE(s.department,''),
                           COALESCE(SUM(a.status = 'Present'), 0),
                           COUNT(a.id)
                    FROM students s
                    LEFT JOIN attendance a ON a.student_id = s.id
                         AND a.date BETWEEN ? AND ?
                         AND a.status IN ('Present','Absent')
                         AND NOT {EXCUSED_SQL}
                    {where}
                    GROUP BY s.id
                    ORDER BY s.department, s.year, s.name
                """, params).fetchall()

            self.clear_children(table)
            self.table_header(table, ["STUDENT ID", "STUDENT NAME", "YEAR", "DEPARTMENT",
                                      "PRESENT / MARKED", "ATTENDANCE %"], weights, "rng")
            total_present = sum(r[4] for r in rows)
            total_marked = sum(r[5] for r in rows)

            def build_range(index, rec):
                sid, name, yr, dept, present_days, marked_days = rec
                pct = present_days / marked_days * 100 if marked_days else 0.0
                self.table_row(
                    table, index,
                    [sid, name, yr or "-", dept or "-", f"{present_days} / {marked_days}",
                     f"{pct:.1f}%" if marked_days else "No records"],
                    weights, "rng",
                    colors={5: (GREEN if pct >= 75 else RED) if marked_days else TEXT}, bold=(5,))
            self.fill_batched(table, rows, build_range)
            overall = total_present / total_marked * 100 if total_marked else 0.0
            summary.configure(
                text=f"{start} to {end}  |  Students: {len(rows)}  |  "
                     f"Present records: {total_present} / {total_marked}  |  Overall: {overall:.1f}%")
            if not rows:
                self.empty_message(table, "No students match these filters.")

        ctk.CTkButton(filters, text="Calculate", width=105, height=36, fg_color=BLUE,
                      command=calculate).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            panel,
            text="Percentage = Present attendance records ÷ days with a recorded Present/Absent "
                 "status in the selected inclusive date range × 100. Unmarked dates are not counted.",
            text_color=MUTED, font=("Segoe UI", 10), wraplength=1000, justify="left"
        ).grid(row=2, column=0, sticky="w", padx=18, pady=(0, 12))
        calculate()

    # ---------------- SECURITY / BLOCKCHAIN ----------------

    @staticmethod
    def _canonical_attendance_payload(attendance_id, student_id, day, stamp, status):
        return {
            "attendance_id": int(attendance_id),
            "student_id": int(student_id),
            "date": str(day),
            "time": str(stamp),
            "status": str(status),
        }

    def secure_attendance_record(self, attendance_id, student_id, day, stamp, status):
        """Encrypt a snapshot and append its hash to the local blockchain.

        The SQLite attendance row remains in its existing query-friendly format;
        the encrypted snapshot is stored separately for verification/audit.
        """
        import hashlib
        import json

        payload = self._canonical_attendance_payload(
            attendance_id, student_id, day, stamp, status)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        encrypted_payload = encrypt_data(serialized)
        record_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

        # Save encrypted snapshot first. If blockchain append fails, the UI reports it;
        # the original attendance record is not silently rolled back or deleted.
        with db() as conn:
            conn.execute(
                "INSERT INTO attendance_security "
                "(attendance_id, encrypted_payload, record_hash, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(attendance_id) DO UPDATE SET "
                "encrypted_payload=excluded.encrypted_payload, "
                "record_hash=excluded.record_hash, created_at=excluded.created_at",
                (attendance_id, encrypted_payload, record_hash, datetime.now().isoformat()))

        AttendanceBlockchain().add_attendance(payload)

    def verify_security_records(self):
        """Verify chain integrity and all encrypted attendance snapshots."""
        import hashlib
        import json

        chain_ok = AttendanceBlockchain().verify_chain()
        with db() as conn:
            rows = conn.execute("""
                SELECT sec.attendance_id, sec.encrypted_payload, sec.record_hash,
                       a.student_id, a.date, a.time, a.status
                FROM attendance_security sec
                JOIN attendance a ON a.id = sec.attendance_id
                ORDER BY sec.attendance_id
            """).fetchall()

        encrypted_ok = True
        checked = 0
        for attendance_id, encrypted, stored_hash, sid, day, stamp, status in rows:
            try:
                decoded = decrypt_data(encrypted)
                payload = json.loads(decoded)
                expected = self._canonical_attendance_payload(
                    attendance_id, sid, day, stamp, status)
                canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"))
                actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                if payload != expected or actual_hash != stored_hash:
                    encrypted_ok = False
                checked += 1
            except Exception:
                encrypted_ok = False
                checked += 1

        return {
            "chain_valid": chain_ok,
            "encrypted_valid": encrypted_ok,
            "checked": checked,
            "total_blocks": AttendanceBlockchain().get_total_blocks(),
            "valid": chain_ok and encrypted_ok,
        }

    def show_security_center(self):
        self.clear_content()
        self.select_nav("Security Center")
        self.header("Security Center", "Encryption, blockchain integrity and attendance audit")

        cards = ctk.CTkFrame(self.content, fg_color="transparent")
        cards.grid(row=1, column=0, sticky="ew")
        for col in range(3):
            cards.grid_columnconfigure(col, weight=1, uniform="security")

        def metric_card(title, note, accent, column, padx):
            panel = self.white_panel(cards)
            panel.grid(row=0, column=column, sticky="ew", padx=padx)
            ctk.CTkLabel(panel, text=title, font=("Segoe UI", 12),
                         text_color=MUTED).pack(anchor="w", padx=18, pady=(17, 4))
            value = ctk.CTkLabel(panel, text="Checking…", font=("Segoe UI", 29, "bold"),
                                 text_color=TEXT)
            value.pack(anchor="w", padx=18)
            detail = ctk.CTkLabel(panel, text=note, font=("Segoe UI", 10),
                                  text_color=accent, wraplength=230, justify="left")
            detail.pack(anchor="w", padx=18, pady=(4, 17))
            return value, detail

        chain_value, chain_note = metric_card(
            "Blockchain Status", "Verification in progress", MUTED, 0, (0, 7))
        blocks_value, blocks_note = metric_card(
            "Total Blocks", "Reading audit chain", BLUE, 1, 7)
        records_value, records_note = metric_card(
            "Encrypted Records", "Checking saved snapshots", GREEN, 2, (7, 0))

        panel = self.white_panel(self.content)
        panel.grid(row=2, column=0, sticky="nsew", pady=(18, 0))
        panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(panel, text="Security Verification", font=("Segoe UI", 17, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=20, pady=(18, 8))
        detail_label = ctk.CTkLabel(
            panel, text="Verifying the blockchain and encrypted attendance snapshots…",
            text_color=MUTED, justify="left", font=("Segoe UI", 12), wraplength=850)
        detail_label.pack(anchor="w", padx=20, pady=(4, 16))
        verify_button = ctk.CTkButton(
            panel, text="Verifying…", height=40, width=160,
            fg_color=BLUE, state="disabled")
        verify_button.pack(anchor="w", padx=20, pady=(0, 18))
        self._security_verify_run = getattr(self, "_security_verify_run", 0) + 1
        run_state = {"id": self._security_verify_run}

        def run_verification():
            self._security_verify_run += 1
            run_state["id"] = self._security_verify_run
            run_id = run_state["id"]
            verify_button.configure(state="disabled", text="Verifying…")
            detail_label.configure(text="Verifying the blockchain and encrypted attendance snapshots…",
                                   text_color=MUTED)

            def worker():
                try:
                    result = self.verify_security_records()
                    error = None
                except Exception as exception:
                    result, error = None, exception
                try:
                    self.after(0, lambda: render_result(run_id, result, error))
                except tk.TclError:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        def render_result(run_id, result, error):
            if run_id != self._security_verify_run or not detail_label.winfo_exists():
                return
            verify_button.configure(state="normal", text="Verify Again")
            if error is not None:
                detail_label.configure(text=f"Verification could not finish: {error}", text_color=RED)
                chain_value.configure(text="ERROR", text_color=RED)
                chain_note.configure(text="Verification failed", text_color=RED)
                blocks_value.configure(text="—")
                blocks_note.configure(text="Unavailable")
                records_value.configure(text="—")
                records_note.configure(text="Unavailable")
                return

            chain_value.configure(
                text="VALID" if result["chain_valid"] else "CHECK REQUIRED",
                text_color=GREEN if result["chain_valid"] else RED)
            chain_note.configure(
                text="Hash links verified" if result["chain_valid"] else "Blockchain integrity issue",
                text_color=GREEN if result["chain_valid"] else RED)
            blocks_value.configure(text=str(result["total_blocks"]))
            blocks_note.configure(text="Includes genesis and attendance blocks")
            records_value.configure(
                text=str(result["checked"]),
                text_color=GREEN if result["encrypted_valid"] else RED)
            records_note.configure(
                text="Snapshots verified" if result["encrypted_valid"] else "Snapshot integrity issue",
                text_color=GREEN if result["encrypted_valid"] else RED)
            detail_label.configure(
                text=("Blockchain: " + ("VALID" if result["chain_valid"] else "INVALID") +
                      "\nEncrypted attendance snapshots: " +
                      ("VALID" if result["encrypted_valid"] else "INVALID") +
                      "\n\nOnly attendance records saved after security integration receive encrypted snapshots. "
                      "Older attendance rows are preserved and are not retroactively encrypted."),
                text_color=TEXT)

        verify_button.configure(command=run_verification)
        run_verification()

    # ---------------- REPORTS / EXPORT ----------------

    def show_reports(self):
        self.clear_content()
        self.select_nav("Reports")
        self.header("Reports & History", "Review and export saved attendance")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(self.content, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(toolbar, text="Export all saved attendance records",
                     text_color=TEXT, font=("Segoe UI", 12)).pack(side="left")
        ctk.CTkButton(toolbar, text="Export Excel", height=36, fg_color="#147D64",
                      command=self.export_excel).pack(side="right", padx=(6,0))
        ctk.CTkButton(toolbar, text="Export PDF", height=36, fg_color="#A34C70",
                      command=self.export_pdf).pack(side="right", padx=(6,0))
        ctk.CTkButton(toolbar, text="Export CSV", height=36, fg_color=BLUE, text_color="#04111F",
                      command=self.export_csv).pack(side="right")

        panel = self.white_panel(self.content)
        panel.grid(row=2, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(panel, text="Attendance History", font=("Segoe UI", 16, "bold"),
                     text_color=TEXT).grid(row=0, column=0, sticky="w", padx=20, pady=(12, 8))
        table = FastScrollFrame(panel, fg_color="transparent", corner_radius=8)
        table.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))

        weights = [2, 1, 2, 1, 2, 2]
        self.table_header(table, ["ATTENDANCE DATE", "STUDENT ID", "NAME", "STATUS",
                                  "TIME RECORDED", "SUBJECT / PERIOD"], weights, "hist")
        with db() as conn:
            # Only the newest rows are drawn; thousands of widgets would freeze the UI.
            records = conn.execute(f"{HISTORY_SQL} LIMIT {MAX_ROWS_SHOWN + 1}").fetchall()
            total = conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]

        if not records:
            self.empty_message(table, "No attendance records saved yet.")
            return
        def build_history(i, record):
            values = ["" if v is None else v for v in record]
            colors = {3: {"Present": GREEN, "Excused": AMBER}.get(record[3], RED)}
            self.table_row(table, i, values, weights, "hist", colors=colors, bold=(3,))
        self.fill_batched(table, records[:MAX_ROWS_SHOWN], build_history)
        if total > MAX_ROWS_SHOWN:
            ctk.CTkLabel(
                table, text=f"Showing the latest {MAX_ROWS_SHOWN} of {total} records. "
                            "Use Export CSV for the complete history.",
                text_color=MUTED).grid(row=MAX_ROWS_SHOWN + 1, column=0, pady=10)

    def export_csv(self):
        filename = filedialog.asksaveasfilename(
            title="Export Attendance", defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=f"attendance_report_{date.today().isoformat()}.csv")
        if not filename:
            return
        try:
            with db() as conn, open(filename, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["Attendance Date", "Student ID", "Student Name",
                                 "Status", "Time Recorded", "Subject / Period"])
                writer.writerows(conn.execute(HISTORY_SQL))  # streamed, not held in memory
            messagebox.showinfo("Export Complete", f"Report saved to:\n{filename}")
        except (OSError, sqlite3.Error) as error:
            messagebox.showerror("Export Error", str(error))

    def export_excel(self):
        filename=filedialog.asksaveasfilename(title="Export Excel",defaultextension=".xlsx",filetypes=[("Excel workbook","*.xlsx")],initialfile=f"attendiq_report_{date.today().isoformat()}.xlsx")
        if not filename: return
        try:
            from openpyxl import Workbook
            wb=Workbook(); ws=wb.active; ws.title="Attendance"
            ws.append(["Date","Student ID","Student Name","Status","Time","Subject / Period"])
            with db() as conn:
                for row in conn.execute(HISTORY_SQL): ws.append(list(row))
            ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
            for col,width in {"A":16,"B":14,"C":28,"D":14,"E":14,"F":22}.items(): ws.column_dimensions[col].width=width
            wb.save(filename); self.log_activity("EXPORT_EXCEL",filename); messagebox.showinfo("Export Complete",f"Excel report saved:\n{filename}")
        except Exception as error: messagebox.showerror("Excel Export Error",str(error))

    def export_pdf(self):
        filename=filedialog.asksaveasfilename(title="Export PDF",defaultextension=".pdf",filetypes=[("PDF document","*.pdf")],initialfile=f"attendiq_report_{date.today().isoformat()}.pdf")
        if not filename: return
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import landscape, A4
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
            with db() as conn: rows=conn.execute(HISTORY_SQL).fetchall()
            data=[["Date","Student ID","Student Name","Status","Time","Subject / Period"]]+[list(r) for r in rows]
            doc=SimpleDocTemplate(filename,pagesize=landscape(A4)); styles=getSampleStyleSheet()
            table=Table(data,repeatRows=1); table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#14243B")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#CBD5E1")),("FONTSIZE",(0,0),(-1,-1),8),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F1F5F9")]),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
            doc.build([Paragraph("AttendIQ Attendance Report",styles["Title"]),Spacer(1,10),table]); self.log_activity("EXPORT_PDF",filename); messagebox.showinfo("Export Complete",f"PDF report saved:\n{filename}")
        except Exception as error: messagebox.showerror("PDF Export Error",str(error))

    # ---------------- FACE RECOGNITION PAGE ----------------

    def show_camera_page(self):
        self.clear_content()
        self.select_nav("Face Recognition")
        self.header("Face Recognition", "AI face matching + blink-based liveness check before automatic attendance")

        panel = self.white_panel(self.content)
        panel.grid(row=1, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(panel, text="Live Face Attendance", font=("Segoe UI", 19, "bold"),
                     text_color=TEXT).grid(row=0, column=0, sticky="w", padx=22, pady=(20, 4))
        ctk.CTkLabel(
            panel,
            text="Register each student with a clear face photo first. Blink once when prompted. "
                 "Choose a timetable period below; attendance is recorded once per student per date and period.",
            font=("Segoe UI", 12), text_color=MUTED, wraplength=850, justify="left"
        ).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 12))

        preview = ctk.CTkFrame(panel, fg_color="#101827", corner_radius=12)
        preview.grid(row=2, column=0, sticky="nsew", padx=22, pady=(0, 14))
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(0, weight=1)
        self.recognition_frame_label = ctk.CTkLabel(
            preview, text="Camera is stopped\n\nClick  Start Face Attendance  to begin",
            text_color="#E7EDF7", font=("Segoe UI", 16), fg_color="#101827")
        self.recognition_frame_label.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 20))
        controls.grid_columnconfigure(3, weight=1)
        with db() as conn: camera_sessions = conn.execute("SELECT id,subject,period,COALESCE(academic_year,''),COALESCE(department,''),COALESCE(class_name,'') FROM timetable WHERE active=1 ORDER BY academic_year,department,class_name,id").fetchall()
        camera_session_map = {"Daily (no period)": None}
        camera_session_map.update({f"{subj} - {per} | {yr} {dept}-{cls}": sid for sid,subj,per,yr,dept,cls in camera_sessions})
        camera_session_menu = ctk.CTkOptionMenu(controls, values=list(camera_session_map.keys()), width=185,
            command=lambda value: setattr(self, "active_session_id", camera_session_map.get(value)))
        camera_session_menu.grid(row=0,column=0,padx=(0,8))
        self.active_session_id = camera_session_map.get(camera_session_menu.get())
        ctk.CTkButton(controls, text="▶  Start Face Attendance", height=42, width=210,
                      fg_color=GREEN, hover_color="#116C56",
                      command=self.start_recognition_camera).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(controls, text="■  Stop Camera", height=42, width=130, fg_color=RED,
                      hover_color="#A73737", command=self.stop_recognition_camera
                      ).grid(row=0, column=2, padx=(0, 10))

        status_frame = ctk.CTkFrame(controls, fg_color="#14243B", corner_radius=8)
        status_frame.grid(row=0, column=3, sticky="ew")
        self.recognition_status_label = ctk.CTkLabel(
            status_frame, text="Ready | Camera stopped", text_color=TEXT,
            font=("Segoe UI", 11, "bold"))
        self.recognition_status_label.pack(anchor="w", padx=12, pady=(7, 2))
        self.recognition_count_label = ctk.CTkLabel(
            status_frame, text="Recognized this session: 0", text_color=MUTED,
            font=("Segoe UI", 10))
        self.recognition_count_label.pack(anchor="w", padx=12, pady=(0, 7))
        self._last_status = self._last_count = None

    def _set_status(self, text, color=TEXT):
        """Only touch the widget when the text really changed (avoids redraws every frame)."""
        key = (text, color)
        if key != self._last_status and self.recognition_status_label is not None:
            if self.recognition_status_label.winfo_exists():
                self.recognition_status_label.configure(text=text, text_color=color)
            self._last_status = key

    def _set_count(self, count):
        if count != self._last_count and self.recognition_count_label is not None:
            if self.recognition_count_label.winfo_exists():
                self.recognition_count_label.configure(text=f"Recognized this session: {count}")
            self._last_count = count

    # ---------- encoding loading with on-disk cache ----------

    def build_face_database(self):
        """Encode registered photos. Results are cached by file mtime/size, so only
        new or changed photos are processed on later runs. Safe to run in a thread."""
        if face_recognition is None:
            raise RuntimeError(
                "The face_recognition package is not installed in this Python environment.\n\n"
                "Activate your project environment and run:\n"
                "python -m pip install face-recognition")

        names = get_student_names()
        try:
            with open(CACHE_PATH, "rb") as f:
                cache = pickle.load(f)
        except Exception:
            cache = {}

        new_cache, encodings, ids = {}, [], []
        for path in sorted(IMAGE_DIR.iterdir()):
            if path.suffix.lower() not in IMAGE_EXT:
                continue
            head = path.name.split("_", 1)[0]
            if not head.isdigit() or int(head) not in names:
                continue
            sid = int(head)
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            entry = cache.get(path.name)
            if entry and entry[0] == signature:
                encoding = entry[1]
            else:
                encoding = self._encode_photo(path)
            new_cache[path.name] = (signature, encoding)
            if encoding is not None:
                encodings.append(encoding)
                ids.append(sid)

        try:
            with open(CACHE_PATH, "wb") as f:
                pickle.dump(new_cache, f)
        except OSError:
            pass

        if not encodings:
            raise RuntimeError(
                "No usable registered face photos were found.\n\n"
                "Open Students > Add Student > Register + Capture Face, or use Edit > "
                "Recapture Face Photo for existing students. Keep the face large, clear, "
                "front-facing, and well lit.")

        # Sort by student so the best distance per student is one vectorized reduce.
        order = np.argsort(ids, kind="stable")
        sorted_ids = np.array(ids)[order]
        unique_ids, starts = np.unique(sorted_ids, return_index=True)
        return (np.array(encodings)[order], unique_ids.tolist(), starts, names)

    @staticmethod
    def _encode_photo(path):
        try:
            image = face_recognition.load_image_file(str(path))
            locations = face_recognition.face_locations(image, number_of_times_to_upsample=1, model="hog")
            if len(locations) != 1:
                print(f"Skipping {path.name}: expected one face, found {len(locations)}")
                return None
            top, right, bottom, left = locations[0]
            face_area = max(0, bottom - top) * max(0, right - left)
            if face_area / max(1, image.shape[0] * image.shape[1]) < 0.025:
                print(f"Skipping {path.name}: face too small; recapture this student.")
                return None
            found = face_recognition.face_encodings(image, known_face_locations=locations, num_jitters=1)
            return found[0] if found else None
        except Exception as error:
            print(f"Could not read face photo {path.name}: {error}")
            return None

    # ---------- start / stop ----------

    def start_recognition_camera(self):
        if self.recognition_running or self.recognition_loading:
            return
        if Image is None:
            messagebox.showerror("Display Dependency Missing",
                                 "Pillow is required for the camera preview. Run:\n"
                                 "python -m pip install pillow", parent=self)
            return

        self.recognition_loading = True
        self.recognition_generation += 1
        generation = self.recognition_generation
        self._set_status("Loading registered faces...", AMBER)
        outcome = {}

        def work():
            try:
                outcome["data"] = self.build_face_database()
            except Exception as error:
                outcome["error"] = error

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if generation != self.recognition_generation or not self.recognition_loading:
                return  # cancelled (page changed or stopped)
            if "data" not in outcome and "error" not in outcome:
                self.after(80, poll)
                return
            self.recognition_loading = False
            if "error" in outcome:
                self._set_status("Ready | Camera stopped")
                messagebox.showerror("Face Recognition Setup", str(outcome["error"]), parent=self)
                return
            (self.known_encodings, self.known_unique_ids,
             self.known_starts, self.student_names) = outcome["data"]
            self._begin_camera()

        poll()

    def _begin_camera(self):
        camera = open_camera(640, 480)
        if camera is None:
            messagebox.showerror(
                "Camera Error",
                "Cannot open the webcam. Close other camera apps and check "
                "Windows Settings > Privacy & security > Camera.", parent=self)
            self._set_status("Ready | Camera stopped")
            return
        self.recognition_camera = camera
        self.recognition_running = True
        self.recognition_seen_in_session = set()
        self.recognition_streaks = {}
        self.liveness_state = {}
        self.recognition_frame_counter = 0
        self.recognition_last_results = []
        self.recognition_busy = False
        self._set_status("Camera active | Looking for registered faces", GREEN)
        self._set_count(0)
        self._recognition_tick()

    # ---------- matching (runs in a worker thread) ----------

    def _match(self, encoding):
        """Return the best student for one face, or None if the match is not trustworthy."""
        distances = np.linalg.norm(self.known_encodings - encoding, axis=1)
        per_student = np.minimum.reduceat(distances, self.known_starts)
        order = np.argsort(per_student)
        best = float(per_student[order[0]])
        second = float(per_student[order[1]]) if len(order) > 1 else 1.0
        if best <= MATCH_THRESHOLD and (len(order) == 1 or second - best >= MATCH_MARGIN):
            return self.known_unique_ids[order[0]]
        return None

    def _recognition_worker(self, rgb_small, generation):
        try:
            locations = face_recognition.face_locations(
                rgb_small, number_of_times_to_upsample=1, model="hog")
            encodings = face_recognition.face_encodings(rgb_small, locations, num_jitters=0)
            results, seen = [], {}
            for (top, right, bottom, left), encoding in zip(locations, encodings):
                box = [int(v / RECOGNITION_SCALE) for v in (top, right, bottom, left)]
                name, sid = "Unknown", None
                candidate = self._match(encoding)
                if candidate is not None:
                    streak = self.recognition_streaks.get(candidate, 0) + 1
                    seen[candidate] = streak
                    live = self.liveness_state.setdefault(candidate, {"closed": False, "blink": False})
                    try:
                        landmarks = face_recognition.face_landmarks(rgb_small, [ (top, right, bottom, left) ])[0]
                        left_eye = self._eye_ratio(landmarks.get("left_eye", [])) if len(landmarks.get("left_eye", [])) == 6 else 1.0
                        right_eye = self._eye_ratio(landmarks.get("right_eye", [])) if len(landmarks.get("right_eye", [])) == 6 else 1.0
                        eye_ratio = (left_eye + right_eye) / 2.0
                        if eye_ratio < 0.19: live["closed"] = True
                        elif live["closed"] and eye_ratio > 0.23: live["blink"] = True
                    except Exception:
                        pass
                    if streak >= STREAK_REQUIRED and live.get("blink"):
                        sid = candidate
                        name = self.student_names.get(candidate, "Unknown")
                    elif streak >= STREAK_REQUIRED:
                        name = "Blink to verify"
                    else:
                        name = "Checking..."
                results.append((*box, name, sid))

                if sid is not None and sid not in self.recognition_seen_in_session:
                    try:
                        saved = self.mark_face_attendance(sid)
                        self.recognition_seen_in_session.add(sid)
                        print(f"Face attendance {'marked' if saved else 'already exists'}: {name} (ID {sid})")
                    except sqlite3.Error as error:
                        print(f"Could not save attendance for {name}: {error}")

            if generation == self.recognition_generation:
                self.recognition_streaks = seen
                self.recognition_last_results = results
        except Exception as error:
            print("Face recognition frame error:", error)
        finally:
            self.recognition_busy = False

    # ---------- GUI loop (stays light: no heavy work here) ----------

    def _recognition_tick(self):
        if not self.recognition_running or self.recognition_camera is None:
            return
        ok, frame = self.recognition_camera.read()
        if not ok:
            self.stop_recognition_camera(silent=True)
            self._set_status("Camera read failed", RED)
            return

        frame = cv2.flip(frame, 1)
        self.recognition_frame_counter += 1

        # Hand a frame to the worker only when it is idle, so the preview never waits.
        if not self.recognition_busy and self.recognition_frame_counter % 3 == 1:
            small = cv2.resize(frame, None, fx=RECOGNITION_SCALE, fy=RECOGNITION_SCALE,
                               interpolation=cv2.INTER_AREA)
            rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            self.recognition_busy = True
            threading.Thread(
                target=self._recognition_worker,
                args=(rgb_small, self.recognition_generation), daemon=True).start()

        names = []
        for top, right, bottom, left, name, sid in self.recognition_last_results:
            color = (35, 190, 120) if sid is not None else (60, 70, 220)
            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
            cv2.rectangle(frame, (left, max(0, top - 32)), (right, top), color, cv2.FILLED)
            cv2.putText(frame, name, (left + 5, max(18, top - 9)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            names.append(name)

        # Resize first, convert colour second: far fewer pixels to process.
        small_view = cv2.resize(frame, PREVIEW_SIZE, interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small_view, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb)
        ctk_image = ctk.CTkImage(light_image=pil_image, dark_image=pil_image, size=PREVIEW_SIZE)
        if self.recognition_frame_label is not None and self.recognition_frame_label.winfo_exists():
            self.recognition_frame_label.configure(image=ctk_image, text="")
            self.recognition_frame_label.image = ctk_image

        if names:
            recognised = any(n not in ("Unknown", "Checking...") for n in names)
            self._set_status("Camera active | " + ", ".join(names[:3]), GREEN if recognised else TEXT)
        else:
            self._set_status("Camera active | Looking for faces...", TEXT)
        self._set_count(len(self.recognition_seen_in_session))

        self.recognition_after_id = self.after(25, self._recognition_tick)

    def mark_face_attendance(self, student_id):
        """Mark a blink-verified face as Present.
        A prior Not Mentioned row is upgraded to Present; a manual Absent is preserved."""
        stamp = datetime.now().strftime("%H:%M:%S")
        day = date.today().isoformat()
        session_id = self.active_session_id
        attendance_id = None
        changed = False
        with db() as conn:
            if session_id is not None:
                eligible = conn.execute("""
                    SELECT 1 FROM students s JOIN timetable t ON t.id=?
                    WHERE s.id=?
                      AND (COALESCE(t.academic_year,'')='' OR s.year=t.academic_year)
                      AND (COALESCE(t.department,'')='' OR UPPER(s.department)=UPPER(t.department))
                      AND (COALESCE(t.class_name,'')='' OR UPPER(COALESCE(s.class_name,''))=UPPER(t.class_name))
                """, (session_id, student_id)).fetchone()
                if not eligible:
                    return False
            existing = conn.execute(
                "SELECT id, status FROM attendance "
                "WHERE student_id=? AND date=? AND session_id IS ?",
                (student_id, day, session_id)
            ).fetchone()
            if existing:
                attendance_id, existing_status = existing
                if existing_status == "Not Mentioned":
                    conn.execute(
                        "UPDATE attendance SET status='Present', time=? WHERE id=?",
                        (stamp, attendance_id)
                    )
                    changed = True
            else:
                cursor = conn.execute(
                    "INSERT INTO attendance (student_id, date, time, status, session_id) "
                    "VALUES (?, ?, ?, 'Present', ?)",
                    (student_id, day, stamp, session_id)
                )
                attendance_id = cursor.lastrowid
                changed = True
        if changed and attendance_id is not None:
            self.secure_attendance_record(attendance_id, student_id, day, stamp, "Present")
            self.log_activity(
                "FACE_ATTENDANCE",
                f"Marked student {student_id} present for {day}, session {session_id} after blink verification"
            )
        return changed

    def stop_recognition_camera(self, silent=False):
        self.recognition_running = False
        self.recognition_loading = False
        self.recognition_generation += 1  # invalidates any in-flight worker/poll

        if self.recognition_after_id is not None:
            try:
                self.after_cancel(self.recognition_after_id)
            except Exception:
                pass
            self.recognition_after_id = None

        if self.recognition_camera is not None:
            try:
                self.recognition_camera.release()
            except Exception:
                pass
            self.recognition_camera = None

        label = self.recognition_frame_label
        if label is not None:
            try:
                if label.winfo_exists():
                    label.configure(image=None,
                                    text="Camera is stopped\n\nClick  Start Face Attendance  to begin")
                    label.image = None
            except Exception:
                pass
        self._set_status("Ready | Camera stopped", TEXT)

    def on_close(self):
        self.stop_recognition_camera(silent=True)
        tunnel = self._teacher_tunnel_process
        portal = self._teacher_portal_process
        teacher_link_active = any(
            process is not None and process.poll() is None
            for process in (tunnel, portal)
        )
        if teacher_link_active and not messagebox.askyesno(
            "TeacherIQ link will stop",
            "Closing AttendIQ will stop the temporary phone website and its current QR links. "
            "To keep attendance working on student phones, choose No and leave AttendIQ running.\n\n"
            "Close AttendIQ anyway?",
            parent=self,
        ):
            return
        if self._teacher_tunnel_poll_id is not None:
            try:
                self.after_cancel(self._teacher_tunnel_poll_id)
            except Exception:
                pass
        self._stop_teacher_services()
        for callback_name in ("_email_scheduler_after",):
            callback_id = getattr(self, callback_name, None)
            if callback_id is not None:
                try:
                    self.after_cancel(callback_id)
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    app = AttendIQ()
    app.mainloop()
