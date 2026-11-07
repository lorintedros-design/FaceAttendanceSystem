"""Leave and excuse management page.

Used as a mixin:  class AttendIQ(AuthMixin, LeaveMixin, ctk.CTk)

Rule: an Absent record that falls inside a student's approved leave is
"Excused" and is left out of attendance percentages. A Present record on a
leave day still counts as present.
"""
from datetime import date
from tkinter import messagebox

import customtkinter as ctk

from security import auth

# Same palette as app.py
WHITE = "#101D32"
TEXT = "#EAF4FF"
MUTED = "#91A9C8"
BLUE = "#00C8F0"
AMBER = "#FFC15A"
RED = "#FF5B7F"

LEAVE_TYPES = ["Medical", "Approved Leave", "Other"]
MAX_LEAVE_ROWS = 120


class LeaveMixin:
    def students_on_leave(self, day):
        """IDs of students whose approved leave covers the given YYYY-MM-DD date."""
        with auth.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT student_id FROM leaves WHERE ? BETWEEN start_date AND end_date",
                (day,)).fetchall()
        return {r[0] for r in rows}

    def show_leave_management(self):
        self.clear_content()
        self.select_nav("Leave Management")
        self.header("Leave & Excuse Management",
                    "Approved leave days are not counted against a student's attendance percentage")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=0)
        self.content.grid_rowconfigure(3, weight=1)

        with auth.connect() as conn:
            students = conn.execute("SELECT id,name,COALESCE(year,''),COALESCE(department,'') FROM students ORDER BY name").fetchall()

        form = self.white_panel(self.content)
        form.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        if not students:
            ctk.CTkLabel(form, text="Register students first.", text_color=TEXT
                         ).pack(padx=20, pady=20)
            return

        student_map = {f"{sid} \u2014 {name}": sid for sid, name, _year, _department in students}
        student_menu = ctk.CTkOptionMenu(form, values=list(student_map), width=240)
        student_menu.grid(row=0, column=0, padx=(12, 6), pady=(14, 6))
        type_menu = ctk.CTkOptionMenu(form, values=LEAVE_TYPES, width=150)
        type_menu.grid(row=0, column=1, padx=6, pady=(14, 6))
        from_entry = ctk.CTkEntry(form, width=120)
        from_entry.insert(0, date.today().isoformat())
        from_entry.grid(row=0, column=2, padx=6, pady=(14, 6))
        to_entry = ctk.CTkEntry(form, width=120)
        to_entry.insert(0, date.today().isoformat())
        to_entry.grid(row=0, column=3, padx=6, pady=(14, 6))
        ctk.CTkLabel(form, text="From / To: YYYY-MM-DD", text_color=MUTED,
                     font=("Segoe UI", 10)).grid(row=0, column=4, padx=6, pady=(14, 6))
        reason_entry = ctk.CTkEntry(form, placeholder_text="Reason or note (optional)")
        reason_entry.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(12, 6), pady=(0, 14))

        table = ctk.CTkScrollableFrame(self.content, fg_color=WHITE)
        search_bar = ctk.CTkFrame(self.content, fg_color="transparent")
        search_bar.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        search_entry = ctk.CTkEntry(search_bar, height=36,
                                    placeholder_text="Search student ID, name, leave type or reason…")
        search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        search_bar.grid_columnconfigure(0, weight=1)
        year_filter = ctk.CTkOptionMenu(search_bar,
            values=["All Years"] + sorted({row[2] for row in students if row[2]}), width=130,
            command=lambda _value: refresh())
        year_filter.grid(row=0, column=1, padx=4)
        department_filter = ctk.CTkOptionMenu(search_bar,
            values=["All Departments"] + sorted({row[3] for row in students if row[3]}), width=145,
            command=lambda _value: refresh())
        department_filter.grid(row=0, column=2, padx=4)
        table.grid(row=3, column=0, sticky="nsew")
        ctk.CTkLabel(
            self.content,
            text="An Absent record on a leave day shows as 'Excused' and is left out of the "
                 "percentage. A Present record on a leave day still counts as present.",
            text_color=MUTED, font=("Segoe UI", 10), wraplength=1000, justify="left"
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))

        weights = [1, 3, 2, 2, 2, 1, 4, 2]

        def refresh():
            self.clear_children(table)
            self.table_header(
                table, ["ID", "STUDENT", "TYPE", "FROM", "TO", "DAYS", "REASON", "ACTIONS"],
                weights, "leave")
            with auth.connect() as conn:
                rows = conn.execute("""
                    SELECT l.id, l.student_id, s.name, l.leave_type, l.start_date, l.end_date,
                           COALESCE(l.reason, '')
                    FROM leaves l LEFT JOIN students s ON s.id = l.student_id
                    ORDER BY l.start_date DESC, l.id DESC LIMIT ?
                """, (MAX_LEAVE_ROWS + 1,)).fetchall()
            query = search_entry.get().strip().lower()
            if query:
                rows = [row for row in rows if any(
                    query in str(value or "").lower() for value in row[:7])]
            student_meta = {sid: (year or "", department or "") for sid, _name, year, department in students}
            rows = [row for row in rows
                    if (year_filter.get() == "All Years" or student_meta.get(row[1], ("", ""))[0] == year_filter.get())
                    and (department_filter.get() == "All Departments" or student_meta.get(row[1], ("", ""))[1].upper() == department_filter.get().upper())]
            if not rows:
                self.empty_message(table, "No leave recorded yet.")
                return
            truncated = len(rows) > MAX_LEAVE_ROWS
            rows = rows[:MAX_LEAVE_ROWS]
            today = date.today().isoformat()

            def build(i, rec):
                leave_id, sid, name, kind, start, end, reason = rec
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                except ValueError:
                    days = "-"
                active = start <= today <= end
                short = reason if len(reason) <= 60 else reason[:60] + "\u2026"
                row = self.table_row(
                    table, i,
                    [leave_id, f"{sid} \u2014 {name or 'Deleted student'}",
                     kind + (" (now)" if active else ""), start, end, days, short, None],
                    weights, "leave", colors={0: BLUE, 2: AMBER if active else TEXT},
                    shade=False)
                ctk.CTkButton(
                    row, text="Delete", width=72, height=30, fg_color="#4B2032",
                    hover_color="#64263C", text_color=RED,
                    command=lambda lid=leave_id, s=sid, n=name: delete(lid, s, n)
                ).grid(row=0, column=7, sticky="w", padx=8, pady=5)

            self.fill_batched(table, rows, build)
            if truncated:
                ctk.CTkLabel(
                    table, text=f"Showing the latest {MAX_LEAVE_ROWS} leave records.",
                    text_color=MUTED).grid(row=len(rows) + 1, column=0, pady=10)

        def delete(leave_id, sid, name):
            if not messagebox.askyesno(
                    "Delete Leave",
                    f"Delete this leave record for {name or sid}?\n\n"
                    "Their attendance percentage will be recalculated without it.",
                    parent=self):
                return
            with auth.connect() as conn:
                conn.execute("DELETE FROM leaves WHERE id = ?", (leave_id,))
            self.log_activity("LEAVE_DELETE", f"Deleted leave {leave_id} for student {sid}")
            refresh()

        def add():
            start = self.valid_date(from_entry.get().strip())
            end = self.valid_date(to_entry.get().strip())
            if not start or not end:
                messagebox.showerror("Invalid Date", "Enter dates as YYYY-MM-DD.", parent=self)
                return
            if start > end:
                messagebox.showerror("Invalid Range",
                                     "The From date must be on or before the To date.", parent=self)
                return
            sid = student_map[student_menu.get()]
            kind = type_menu.get()
            with auth.connect() as conn:
                clash = conn.execute(
                    "SELECT start_date, end_date FROM leaves "
                    "WHERE student_id = ? AND start_date <= ? AND end_date >= ? LIMIT 1",
                    (sid, end, start)).fetchone()
                if clash:
                    messagebox.showwarning(
                        "Overlapping Leave",
                        f"This student already has leave from {clash[0]} to {clash[1]} "
                        "that overlaps these dates. Delete or adjust it first.", parent=self)
                    return
                conn.execute(
                    "INSERT INTO leaves (student_id, start_date, end_date, leave_type, reason, "
                    "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, start, end, kind, reason_entry.get().strip(),
                     self.current_username(), date.today().isoformat()))
            self.log_activity("LEAVE_ADD", f"Student {sid}: {kind} {start} to {end}")
            reason_entry.delete(0, "end")
            refresh()

        ctk.CTkButton(form, text="+ Add Leave", command=add, fg_color=BLUE,
                      text_color="#04111F").grid(row=1, column=3, columnspan=2, sticky="w",
                                                 padx=6, pady=(0, 14))
        refresh()
        search_entry.bind("<KeyRelease>", lambda _event: refresh())
