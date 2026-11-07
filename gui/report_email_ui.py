"""Attendance report emails for AttendIQ.

Pick a From and To date, choose Students, Parents or Both, and each recipient
gets an individual email with their own Present, Absent, total recorded
sessions and attendance percentage for that period.

Reuses the SMTP details saved in Settings & Backup.
"""
import queue
import smtplib
import sqlite3
import ssl
import threading
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from tkinter import StringVar, messagebox

import customtkinter as ctk

# Same database the main app uses. Defined here so this module never imports app.py.
DB_PATH = Path(__file__).resolve().parent.parent / "database" / "attendance.db"

TEXT = "#EAF4FF"
MUTED = "#91A9C8"
GREEN = "#00D6A0"
RED = "#FF5B7F"
BLUE = "#00C8F0"
WHITE = "#101D32"
BORDER = "#203754"

# Absent rows covered by approved leave are excluded, same rule as the rest of the app.
EXCUSED_SQL = ("(a.status = 'Absent' AND EXISTS (SELECT 1 FROM leaves lv "
               "WHERE lv.student_id = a.student_id AND a.date BETWEEN lv.start_date AND lv.end_date))")

REPORT_SUBJECT = "AttendIQ: Attendance Report for {student_name} ({from_date} to {to_date})"
REPORT_BODY = """Dear {greeting},

Here is the attendance report for {student_name}.
Period: {from_date} to {to_date}

Present: {present}
Absent: {absent}
Total recorded sessions: {total}
Attendance percentage: {percentage}

Please contact the faculty advisor if you have any questions.

AttendIQ
"""


def _alive(widget):
    try:
        return widget is not None and widget.winfo_exists()
    except Exception:
        return False


def _fetch_rows(from_day, to_day):
    sql = f"""
        SELECT s.id, s.name, COALESCE(s.email, ''), COALESCE(s.parent_email, ''),
               COALESCE(SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN a.status = 'Absent' AND NOT {EXCUSED_SQL} THEN 1 ELSE 0 END), 0)
        FROM students s
        LEFT JOIN attendance a ON a.student_id = s.id AND a.date BETWEEN ? AND ?
        GROUP BY s.id, s.name, s.email, s.parent_email
        ORDER BY s.name COLLATE NOCASE
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        return conn.execute(sql, (from_day, to_day)).fetchall()
    finally:
        conn.close()


def _build_jobs(rows, who):
    """One job per recipient address. Returns (jobs, skipped_count)."""
    jobs, skipped = [], 0
    for _sid, name, student_email, parent_email, present, absent in rows:
        total = present + absent
        pct = f"{present / total * 100:.1f}%" if total else "N/A (no sessions recorded)"
        targets = []
        if who in ("Students", "Both"):
            targets.append(("Student", student_email.strip(), name))
        if who in ("Parents", "Both"):
            targets.append(("Parent", parent_email.strip(), f"Parent/Guardian of {name}"))
        for kind, address, greeting in targets:
            if not address:
                skipped += 1
                continue
            jobs.append({"kind": kind, "address": address, "name": name, "greeting": greeting,
                         "present": present, "absent": absent, "total": total, "percentage": pct})
    return jobs, skipped


def _build_message(sender, job, from_day, to_day):
    values = {"greeting": job["greeting"], "student_name": job["name"],
              "from_date": from_day, "to_date": to_day, "present": job["present"],
              "absent": job["absent"], "total": job["total"], "percentage": job["percentage"]}
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = job["address"]
    msg["Subject"] = REPORT_SUBJECT.format(**values)
    msg.set_content(REPORT_BODY.format(**values))
    return msg


def _open_server(host, port, sender, password):
    """Connect and log in, trying the sensible ports in turn. Returns (server, port)."""
    if host.lower().endswith("gmail.com"):
        ports = [465, 587]
    else:
        ports = [port] + [p for p in (587, 465) if p != port]
    last_error = None
    for attempt in ports:
        server = None
        try:
            context = ssl.create_default_context()
            if attempt == 465:
                server = smtplib.SMTP_SSL(host, attempt, context=context, timeout=20)
            else:
                server = smtplib.SMTP(host, attempt, timeout=20)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(sender, password)
            return server, attempt
        except smtplib.SMTPAuthenticationError:
            raise
        except (smtplib.SMTPException, OSError) as error:
            last_error = error
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
    raise ConnectionError(f"Could not connect to {host} on ports {ports}: {last_error}")


def _send_worker(config, jobs, from_day, to_day, out):
    """Runs in a background thread. Never touches Tk widgets; reports through a queue."""
    host = (config.get("host") or "smtp.gmail.com").strip()
    try:
        port = int(config.get("port") or 587)
    except (TypeError, ValueError):
        port = 587
    sender = (config.get("sender") or "").strip()
    password = "".join((config.get("password") or "").split())

    try:
        server, used_port = _open_server(host, port, sender, password)
    except smtplib.SMTPAuthenticationError:
        out.put(("error", "SMTP login was rejected. Check the saved Gmail App Password in Settings & Backup."))
        return
    except ConnectionError as error:
        out.put(("error", str(error)))
        return

    sent, failed = 0, []
    try:
        for index, job in enumerate(jobs, start=1):
            try:
                server.send_message(_build_message(sender, job, from_day, to_day))
                sent += 1
            except Exception as error:
                failed.append(f"{job['name']} ({job['kind']}, {job['address']}): {error}")
            out.put(("progress", index, len(jobs)))
    finally:
        try:
            server.quit()
        except Exception:
            pass
    out.put(("done", {"sent": sent, "failed": failed, "port": used_port}))


class ReportEmailMixin:
    """Mixed into AttendIQ. Uses its helpers: clear_content, select_nav, header,
    white_panel, content, _load_email_automation, log_activity."""

    _report_running = False
    _report_meta = None
    _report_ui = None

    # ---------- page ----------

    def show_report_email_page(self):
        self.clear_content()
        self.select_nav("Send Report Emails")
        self.header("Attendance Report Emails",
                    "Send each student or parent their own attendance summary for a chosen period")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(2, weight=1)

        # --- form ---
        form = self.white_panel(self.content)
        form.grid(row=1, column=0, sticky="ew", padx=2, pady=(0, 14))
        form.grid_columnconfigure((1, 3), weight=1)

        today = date.today()
        ctk.CTkLabel(form, text="From date", text_color=TEXT).grid(row=0, column=0, sticky="w", padx=18, pady=(18, 6))
        from_entry = ctk.CTkEntry(form, placeholder_text="YYYY-MM-DD", height=36)
        from_entry.insert(0, today.replace(day=1).isoformat())
        from_entry.grid(row=0, column=1, sticky="ew", padx=(0, 18), pady=(18, 6))
        ctk.CTkLabel(form, text="To date", text_color=TEXT).grid(row=0, column=2, sticky="w", padx=(0, 10), pady=(18, 6))
        to_entry = ctk.CTkEntry(form, placeholder_text="YYYY-MM-DD", height=36)
        to_entry.insert(0, today.isoformat())
        to_entry.grid(row=0, column=3, sticky="ew", padx=(0, 18), pady=(18, 6))

        quick = ctk.CTkFrame(form, fg_color="transparent")
        quick.grid(row=1, column=0, columnspan=4, sticky="w", padx=18, pady=(0, 10))

        def set_range(start, end):
            from_entry.delete(0, "end")
            from_entry.insert(0, start.isoformat())
            to_entry.delete(0, "end")
            to_entry.insert(0, end.isoformat())

        ctk.CTkButton(quick, text="This month", width=120, height=30, fg_color="#1E3556",
                      command=lambda: set_range(today.replace(day=1), today)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(quick, text="Last 30 days", width=120, height=30, fg_color="#1E3556",
                      command=lambda: set_range(today - timedelta(days=29), today)).pack(side="left")

        who_var = StringVar(value="Both")
        ctk.CTkLabel(form, text="Send to", text_color=TEXT).grid(row=2, column=0, sticky="w", padx=18, pady=8)
        ctk.CTkSegmentedButton(form, values=["Students", "Parents", "Both"], variable=who_var).grid(
            row=2, column=1, columnspan=3, sticky="w", padx=(0, 18), pady=8)

        config = self._report_config()
        if config.get("sender") and config.get("password"):
            smtp_text, smtp_color = f"Sending from {config['sender']} (saved SMTP details)", MUTED
        else:
            smtp_text, smtp_color = "No SMTP details saved yet. Add them in Settings & Backup.", RED
        ctk.CTkLabel(form, text=smtp_text, text_color=smtp_color, font=("Segoe UI", 11)).grid(
            row=3, column=0, columnspan=4, sticky="w", padx=18, pady=(0, 8))

        buttons = ctk.CTkFrame(form, fg_color="transparent")
        buttons.grid(row=4, column=0, columnspan=4, sticky="w", padx=18, pady=(4, 18))
        send_btn = ctk.CTkButton(buttons, text="Send Report Emails", width=190, height=38,
                                 fg_color="#00A7C8", text_color="#04111F",
                                 state="disabled" if self._report_running else "normal",
                                 command=lambda: self._start_report_send(from_entry, to_entry, who_var))
        send_btn.pack(side="left", padx=(0, 10))
        ctk.CTkButton(buttons, text="Preview recipients", width=160, height=38, fg_color="#1E3556",
                      command=lambda: self._preview_report(from_entry, to_entry, who_var)).pack(side="left")

        # --- results ---
        results = self.white_panel(self.content)
        results.grid(row=2, column=0, sticky="nsew", padx=2, pady=(0, 8))
        results.grid_columnconfigure(0, weight=1)
        results.grid_rowconfigure(2, weight=1)

        counters = ctk.CTkFrame(results, fg_color="transparent")
        counters.grid(row=0, column=0, sticky="w", padx=18, pady=(16, 4))
        sent_label = ctk.CTkLabel(counters, text="Sent: 0", font=("Segoe UI", 16, "bold"), text_color=GREEN)
        sent_label.pack(side="left", padx=(0, 26))
        failed_label = ctk.CTkLabel(counters, text="Failed: 0", font=("Segoe UI", 16, "bold"), text_color=RED)
        failed_label.pack(side="left")

        status_label = ctk.CTkLabel(results, text="Choose a period and recipients, then preview or send.",
                                    text_color=MUTED, font=("Segoe UI", 11))
        status_label.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 6))

        log_box = ctk.CTkTextbox(results, height=220, fg_color="#0B1A2E", text_color=TEXT,
                                 font=("Consolas", 11))
        log_box.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 18))
        log_box.configure(state="disabled")

        self._report_ui = {"send_btn": send_btn, "sent": sent_label, "failed": failed_label,
                           "status": status_label, "log": log_box}

        # A send that is still running reports to this new page.
        if self._report_running:
            self._set_report_busy(True)

    # ---------- helpers ----------

    def _report_config(self):
        saved = self._load_email_automation() or {}
        return saved.get("config", {}) or {}

    def _read_report_range(self, from_entry, to_entry):
        try:
            start = datetime.strptime(from_entry.get().strip(), "%Y-%m-%d").date()
            end = datetime.strptime(to_entry.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("Invalid date", "Use the format YYYY-MM-DD, for example 2026-11-01.")
            return None
        if start > end:
            messagebox.showerror("Invalid range", "The From date must be on or before the To date.")
            return None
        return start.isoformat(), end.isoformat()

    def _write_report_log(self, lines):
        box = (self._report_ui or {}).get("log")
        if not _alive(box):
            return
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", "\n".join(lines))
        box.configure(state="disabled")

    def _set_report_busy(self, busy):
        ui = self._report_ui or {}
        if _alive(ui.get("send_btn")):
            ui["send_btn"].configure(state="disabled" if busy else "normal")

    # ---------- preview ----------

    def _preview_report(self, from_entry, to_entry, who_var):
        rng = self._read_report_range(from_entry, to_entry)
        if not rng:
            return
        jobs, skipped = _build_jobs(_fetch_rows(*rng), who_var.get())
        lines = []
        for job in jobs:
            lines.append(f"{job['kind']:<8} {job['name']}  ->  {job['address']}")
            lines.append(f"         Present {job['present']}   Absent {job['absent']}   "
                         f"Total {job['total']}   {job['percentage']}")
        header = f"{len(jobs)} email(s) ready, {skipped} recipient(s) skipped (no email address)."
        self._write_report_log([header, ""] + lines if lines else [header])
        ui = self._report_ui or {}
        if _alive(ui.get("status")):
            ui["status"].configure(text="Preview only. Nothing has been sent.", text_color=MUTED)

    # ---------- sending ----------

    def _start_report_send(self, from_entry, to_entry, who_var):
        if self._report_running:
            return
        rng = self._read_report_range(from_entry, to_entry)
        if not rng:
            return
        from_day, to_day = rng
        who = who_var.get()

        config = self._report_config()
        if not config.get("sender") or not config.get("password"):
            messagebox.showwarning("SMTP details", "Save your sender email and App Password in Settings & Backup first.")
            return

        jobs, skipped = _build_jobs(_fetch_rows(from_day, to_day), who)
        if not jobs:
            messagebox.showinfo("No recipients",
                                "No recipient has an email address for this selection. "
                                "Add student or parent emails in the Students page.")
            return

        if not messagebox.askyesno(
                "Send emails",
                f"Send {len(jobs)} individual report email(s)?\n\n"
                f"Period: {from_day} to {to_day}\nRecipients: {who}\n"
                f"Skipped (no address): {skipped}"):
            return

        self._report_running = True
        self._report_meta = (from_day, to_day, who)
        self._set_report_busy(True)
        ui = self._report_ui or {}
        if _alive(ui.get("status")):
            ui["status"].configure(text=f"Sending 0 of {len(jobs)}...", text_color=BLUE)
        self._write_report_log([f"Starting: {len(jobs)} email(s), period {from_day} to {to_day}."])

        out = queue.Queue()
        threading.Thread(target=_send_worker, args=(config, jobs, from_day, to_day, out),
                         daemon=True).start()
        self.after(150, lambda: self._poll_report_send(out))

    def _poll_report_send(self, out):
        finished = None
        while True:
            try:
                item = out.get_nowait()
            except queue.Empty:
                break
            if item[0] == "progress":
                ui = self._report_ui or {}
                if _alive(ui.get("status")):
                    ui["status"].configure(text=f"Sending {item[1]} of {item[2]}...", text_color=BLUE)
            else:
                finished = item
                break

        if finished is None:
            self.after(150, lambda: self._poll_report_send(out))
            return

        self._report_running = False
        from_day, to_day, who = self._report_meta or ("", "", "")
        ui = self._report_ui or {}
        self._set_report_busy(False)

        if finished[0] == "error":
            message = finished[1]
            if _alive(ui.get("status")):
                ui["status"].configure(text="Sending stopped: " + message, text_color=RED)
            self._write_report_log([message])
            self.log_activity("REPORT_EMAIL_FAILED", f"{from_day} to {to_day}, {who}: {message}")
            return

        result = finished[1]
        sent, failed = result["sent"], result["failed"]
        if _alive(ui.get("sent")):
            ui["sent"].configure(text=f"Sent: {sent}")
            ui["failed"].configure(text=f"Failed: {len(failed)}")
        if _alive(ui.get("status")):
            ui["status"].configure(
                text=f"Finished. {sent} delivered, {len(failed)} failed (port {result['port']}).",
                text_color=GREEN if not failed else RED)

        lines = [f"Finished: period {from_day} to {to_day}, recipients {who}",
                 f"Sent: {sent}    Failed: {len(failed)}"]
        if failed:
            lines += ["", "Failed deliveries:"] + [f"  - {item}" for item in failed]
        self._write_report_log(lines)
        self.log_activity("REPORT_EMAIL",
                          f"Attendance report {from_day} to {to_day}, recipients {who}: "
                          f"sent {sent}, failed {len(failed)}")