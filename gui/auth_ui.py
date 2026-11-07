"""Login screen, sign-out, account tools and the Manage Users page.

Used as a mixin:  class AttendIQ(AuthMixin, ctk.CTk)
It relies on helpers that already exist in AttendIQ (clear_content, header,
white_panel, table_header, table_row, log_activity, ...).
"""
from tkinter import messagebox

import customtkinter as ctk

from security import auth

# Same palette as app.py
BG = "#07101F"
WHITE = "#101D32"
TEXT = "#EAF4FF"
MUTED = "#91A9C8"
BLUE = "#00C8F0"
GREEN = "#00D6A0"
RED = "#FF5B7F"
BORDER = "#203754"


class AuthMixin:
    current_user = None

    # ---------------- setup ----------------

    def init_auth(self):
        self.current_user = None
        self.login_overlay = None
        self.login_throttle = auth.LoginThrottle()
        with auth.connect() as conn:
            auth.init_users_table(conn)

    def current_username(self):
        return self.current_user["username"] if self.current_user else "System"

    # ---------------- permission checks ----------------

    def can_do(self, action):
        return bool(self.current_user) and auth.can(self.current_user["role"], action)

    def require_permission(self, action):
        if self.can_do(action):
            return True
        self.log_activity("ACCESS_DENIED", action)
        messagebox.showerror("Access denied",
                             "Your account does not have permission to do this.", parent=self)
        return False

    def navigate_to(self, page, command):
        """Sidebar click handler: opens the page only if the role allows it."""
        if not self.current_user:
            return
        if not auth.can_open(self.current_user["role"], page):
            self.log_activity("ACCESS_DENIED", f"Tried to open {page}")
            messagebox.showerror("Access denied",
                                 f"{page} is available to Admin accounts only.", parent=self)
            return
        command()

    def apply_permissions(self):
        role = self.current_user["role"]
        for name, button in self.nav_buttons.items():
            allowed = auth.can_open(role, name)
            button.configure(state="normal" if allowed else "disabled",
                             text_color_disabled="#4D5F78")
        self.account_label.configure(text=f"{self.current_user['username']}\n{role}")

    # ---------------- sidebar account box ----------------

    def build_account_box(self, parent):
        box = ctk.CTkFrame(parent, fg_color="#0C192B", corner_radius=10)
        box.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 4))
        self.account_label = ctk.CTkLabel(
            box, text="Not signed in", font=("Segoe UI", 11, "bold"),
            text_color=TEXT, justify="left")
        self.account_label.pack(anchor="w", padx=12, pady=(10, 6))
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(0, 10))
        ctk.CTkButton(row, text="Password", width=80, height=28, fg_color="#203754",
                      hover_color="#2A4668", text_color=TEXT,
                      command=self.change_own_password).pack(side="left", padx=(4, 4))
        ctk.CTkButton(row, text="Sign out", width=80, height=28, fg_color="#4B2032",
                      hover_color="#64263C", text_color=RED,
                      command=self.sign_out).pack(side="left")

    # ---------------- login / first-run screen ----------------

    def show_login_screen(self):
        self.stop_recognition_camera(silent=True)
        self.current_user = None
        if self.login_overlay is not None:
            self.login_overlay.destroy()
        self.sidebar.grid_remove()
        if self.content is not None:
            self.content.grid_remove()

        with auth.connect() as conn:
            first_run = auth.user_count(conn) == 0

        overlay = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        overlay.grid(row=0, column=0, columnspan=2, sticky="nsew")
        overlay.tkraise()
        self.login_overlay = overlay

        card = ctk.CTkFrame(overlay, fg_color=WHITE, corner_radius=16,
                            border_width=1, border_color=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(card, text="AttendIQ", font=("Segoe UI", 28, "bold"),
                     text_color=TEXT).pack(padx=44, pady=(34, 2))
        ctk.CTkLabel(
            card, font=("Segoe UI", 13), text_color=MUTED,
            text="Create the first Admin account" if first_run else "Sign in to continue"
        ).pack(pady=(0, 18))

        def entry(placeholder, secret=False):
            e = ctk.CTkEntry(card, width=320, height=40, placeholder_text=placeholder,
                             show="\u2022" if secret else "")
            e.pack(padx=44, pady=6)
            return e

        user_entry = entry("Username")
        pass_entry = entry("Password (min 8 characters)" if first_run else "Password", True)
        confirm_entry = entry("Confirm password", True) if first_run else None

        show_var = ctk.BooleanVar(value=False)

        def toggle_show():
            mark = "" if show_var.get() else "\u2022"
            pass_entry.configure(show=mark)
            if confirm_entry is not None:
                confirm_entry.configure(show=mark)

        ctk.CTkCheckBox(card, text="Show password", variable=show_var, command=toggle_show,
                        checkbox_width=18, checkbox_height=18, text_color=MUTED,
                        font=("Segoe UI", 12)).pack(anchor="w", padx=46, pady=(6, 0))
        message = ctk.CTkLabel(card, text="", text_color=RED, wraplength=320, justify="left")
        message.pack(padx=44, pady=(6, 0))

        def submit(_event=None):
            username = user_entry.get().strip()
            password = pass_entry.get()
            if first_run:
                if password != confirm_entry.get():
                    message.configure(text="Passwords do not match.")
                    return
                try:
                    with auth.connect() as conn:
                        if auth.user_count(conn) != 0:
                            raise ValueError("An account already exists. Restart the app.")
                        user_id = auth.create_user(conn, username, password, auth.ROLE_ADMIN)
                except ValueError as error:
                    message.configure(text=str(error))
                    return
                user = {"id": user_id, "username": username, "role": auth.ROLE_ADMIN}
                self.log_activity("USER_CREATE", f"First Admin '{username}' created",
                                  actor=username)
            else:
                wait = self.login_throttle.locked_for(username)
                if wait > 0:
                    message.configure(text=f"Too many attempts. Try again in {wait} seconds.")
                    return
                with auth.connect() as conn:
                    user = auth.authenticate(conn, username, password)
                if user is None:
                    self.login_throttle.record_failure(username)
                    self.log_activity("LOGIN_FAILED",
                                      f"Failed sign-in for '{username[:40]}'", actor="Unknown")
                    wait = self.login_throttle.locked_for(username)
                    message.configure(
                        text=(f"Too many attempts. Try again in {wait} seconds." if wait
                              else "Incorrect username or password."))
                    pass_entry.delete(0, "end")
                    return
                self.login_throttle.reset(username)
            self.complete_login(user)

        ctk.CTkButton(
            card, text="Create Admin Account" if first_run else "Sign In",
            width=320, height=42, fg_color=BLUE, text_color="#04111F",
            font=("Segoe UI", 13, "bold"), command=submit
        ).pack(padx=44, pady=(12, 34))
        for widget in (user_entry, pass_entry, confirm_entry):
            if widget is not None:
                widget.bind("<Return>", submit)
        self.after(150, user_entry.focus)

    def complete_login(self, user):
        self.current_user = user
        self.log_activity("LOGIN", f"{user['username']} signed in ({user['role']})")
        self.login_overlay.destroy()
        self.login_overlay = None
        self.sidebar.grid()
        self.apply_permissions()
        self.show_dashboard()

    def sign_out(self):
        if not self.current_user:
            return
        self.log_activity("LOGOUT", f"{self.current_user['username']} signed out")
        self.show_login_screen()

    # ---------------- password dialogs ----------------

    def _password_dialog(self, title, subtitle, ask_current, apply, done_text):
        """apply(current_or_None, new_password) must raise ValueError to reject."""
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("420x440" if ask_current else "420x380")
        dialog.resizable(False, False)
        dialog.configure(fg_color=BG)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text=title, font=("Segoe UI", 21, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=25, pady=(24, 4))
        ctk.CTkLabel(dialog, text=subtitle, text_color=MUTED).pack(anchor="w", padx=25, pady=(0, 14))

        def entry(placeholder):
            e = ctk.CTkEntry(dialog, height=40, placeholder_text=placeholder,
                             show="\u2022", fg_color=WHITE)
            e.pack(fill="x", padx=25, pady=5)
            return e

        current = entry("Current password") if ask_current else None
        new = entry("New password (min 8 characters)")
        confirm = entry("Confirm new password")
        fields = [e for e in (current, new, confirm) if e is not None]
        show_var = ctk.BooleanVar(value=False)

        def toggle_show():
            for e in fields:
                e.configure(show="" if show_var.get() else "\u2022")

        ctk.CTkCheckBox(dialog, text="Show passwords", variable=show_var, command=toggle_show,
                        checkbox_width=18, checkbox_height=18, text_color=MUTED
                        ).pack(anchor="w", padx=27, pady=(6, 0))
        message = ctk.CTkLabel(dialog, text="", text_color=RED, wraplength=370, justify="left")
        message.pack(anchor="w", padx=25, pady=(6, 0))

        def submit():
            if new.get() != confirm.get():
                message.configure(text="The new passwords do not match.")
                return
            try:
                apply(current.get() if current else None, new.get())
            except ValueError as error:
                message.configure(text=str(error))
                return
            dialog.destroy()
            messagebox.showinfo(title, done_text, parent=self)

        ctk.CTkButton(dialog, text="Save Password", height=42, fg_color=BLUE,
                      text_color="#04111F", command=submit).pack(fill="x", padx=25, pady=(14, 8))
        ctk.CTkButton(dialog, text="Cancel", height=36, fg_color="transparent",
                      text_color=TEXT, command=dialog.destroy).pack(fill="x", padx=25)

    def change_own_password(self):
        if not self.current_user:
            return
        me = dict(self.current_user)

        def apply(current, new):
            with auth.connect() as conn:
                if auth.authenticate(conn, me["username"], current, record_login=False) is None:
                    raise ValueError("Current password is incorrect.")
                auth.set_password(conn, me["id"], new)
            self.log_activity("PASSWORD_CHANGE", f"{me['username']} changed their own password")

        self._password_dialog("Change Password", f"Signed in as {me['username']}",
                              True, apply, "Your password was updated.")

    # ---------------- Manage Users page (Admin only) ----------------

    def show_manage_users(self):
        self.clear_content()
        self.select_nav("Manage Users")
        self.header("Manage Users", "Create accounts and control who can change records")
        self.content.grid_rowconfigure(1, weight=0)
        self.content.grid_rowconfigure(2, weight=1)

        form = self.white_panel(self.content)
        form.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        username = ctk.CTkEntry(form, placeholder_text="Username", width=170)
        username.grid(row=0, column=0, padx=10, pady=14)
        password = ctk.CTkEntry(form, placeholder_text="Password (min 8)", show="\u2022", width=190)
        password.grid(row=0, column=1, padx=6, pady=14)
        role = ctk.CTkOptionMenu(form, values=list(auth.ROLES), width=120)
        role.set(auth.ROLE_TEACHER)
        role.grid(row=0, column=2, padx=6, pady=14)

        table = ctk.CTkScrollableFrame(self.content, fg_color=WHITE)
        table.grid(row=2, column=0, sticky="nsew")
        weights = [1, 3, 2, 2, 3, 6]

        def run(action, log_action, log_detail, failure_title="Not allowed"):
            """Run a database change; show the reason if it is rejected."""
            if not self.require_permission("manage_users"):
                return
            try:
                with auth.connect() as conn:
                    action(conn)
            except ValueError as error:
                messagebox.showwarning(failure_title, str(error), parent=self)
                return False
            self.log_activity(log_action, log_detail)
            refresh()
            return True

        def refresh():
            self.clear_children(table)
            self.table_header(table, ["ID", "USERNAME", "ROLE", "STATUS", "LAST LOGIN", "ACTIONS"],
                              weights, "usr")
            with auth.connect() as conn:
                users = auth.list_users(conn)
            for i, u in enumerate(users, 1):
                is_me = u["id"] == self.current_user["id"]
                row = self.table_row(
                    table, i,
                    [u["id"], u["username"] + ("  (you)" if is_me else ""), u["role"],
                     "Active" if u["active"] else "Disabled", u["last_login"] or "Never", None],
                    weights, "usr", colors={0: BLUE, 3: GREEN if u["active"] else RED},
                    shade=False)
                actions = ctk.CTkFrame(row, fg_color="transparent")
                actions.grid(row=0, column=5, sticky="w", padx=8, pady=5)
                make_buttons(actions, u, is_me)

        def make_buttons(parent, u, is_me):
            def btn(text, width, command, fg="#203754", color=TEXT, hover="#2A4668"):
                ctk.CTkButton(parent, text=text, width=width, height=30, fg_color=fg,
                              hover_color=hover, text_color=color, command=command
                              ).pack(side="left", padx=(0, 6))

            def reset():
                def apply(_current, new):
                    if not self.require_permission("manage_users"):
                        raise ValueError("Permission denied.")
                    with auth.connect() as conn:
                        auth.set_password(conn, u["id"], new)
                    self.log_activity("PASSWORD_RESET", f"Reset password for {u['username']}")
                self._password_dialog("Reset Password", f"New password for {u['username']}",
                                      False, apply, "Password was reset.")

            def switch_role():
                if is_me:
                    messagebox.showwarning("Not allowed", "You cannot change your own role.", parent=self)
                    return
                new_role = auth.ROLE_TEACHER if u["role"] == auth.ROLE_ADMIN else auth.ROLE_ADMIN
                run(lambda c: auth.set_role(c, u["id"], new_role), "USER_ROLE",
                    f"{u['username']} -> {new_role}")

            def toggle():
                if is_me:
                    messagebox.showwarning("Not allowed", "You cannot disable your own account.", parent=self)
                    return
                enable = not u["active"]
                run(lambda c: auth.set_active(c, u["id"], enable),
                    "USER_ENABLE" if enable else "USER_DISABLE", u["username"])

            def delete():
                if is_me:
                    messagebox.showwarning("Not allowed", "You cannot delete your own account.", parent=self)
                    return
                if messagebox.askyesno("Delete User", f"Delete the account '{u['username']}'?\n\n"
                                       "This cannot be undone.", parent=self):
                    run(lambda c: auth.delete_user(c, u["id"]), "USER_DELETE", u["username"])

            btn("Reset", 58, reset)
            btn("Role", 52, switch_role)
            btn("Enable" if not u["active"] else "Disable", 68, toggle)
            btn("Delete", 62, delete, fg="#4B2032", color=RED, hover="#64263C")

        def add():
            name = username.get().strip()
            created = run(lambda c: auth.create_user(c, name, password.get(), role.get()),
                          "USER_CREATE", f"Created {role.get()} account '{name}'",
                          failure_title="Cannot create user")
            if created:
                username.delete(0, "end")
                password.delete(0, "end")

        ctk.CTkButton(form, text="+ Add User", command=add, fg_color=BLUE,
                      text_color="#04111F").grid(row=0, column=3, padx=12, pady=14)
        refresh()