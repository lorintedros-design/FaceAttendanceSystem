import customtkinter as ctk


# ==========================================
# APP SETTINGS
# ==========================================

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ==========================================
# MAIN APPLICATION
# ==========================================

class FaceAttendanceApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("Face Attendance System")
        self.geometry("1200x700")
        self.minsize(1000, 600)

        # Main grid
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Create sidebar
        self.create_sidebar()

        # Create content area
        self.content_frame = ctk.CTkFrame(
            self,
            corner_radius=0,
            fg_color="transparent"
        )

        self.content_frame.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=30,
            pady=25
        )

        # Show dashboard first
        self.show_dashboard()


    # ==========================================
    # SIDEBAR
    # ==========================================

    def create_sidebar(self):

        self.sidebar = ctk.CTkFrame(
            self,
            width=230,
            corner_radius=0
        )

        self.sidebar.grid(
            row=0,
            column=0,
            sticky="nsew"
        )

        self.sidebar.grid_propagate(False)


        # Application Logo

        logo = ctk.CTkLabel(
            self.sidebar,
            text="FACE\nATTENDANCE",
            font=("Arial", 24, "bold")
        )

        logo.pack(pady=(40, 5))


        subtitle = ctk.CTkLabel(
            self.sidebar,
            text="SMART SYSTEM",
            font=("Arial", 11)
        )

        subtitle.pack(pady=(0, 35))


        # Sidebar Buttons

        self.dashboard_button = ctk.CTkButton(
            self.sidebar,
            text="🏠  Dashboard",
            height=45,
            command=self.show_dashboard
        )

        self.dashboard_button.pack(
            fill="x",
            padx=20,
            pady=6
        )


        self.students_button = ctk.CTkButton(
            self.sidebar,
            text="👤  Students",
            height=45,
            command=self.show_students
        )

        self.students_button.pack(
            fill="x",
            padx=20,
            pady=6
        )


        self.attendance_button = ctk.CTkButton(
            self.sidebar,
            text="📋  Attendance",
            height=45,
            command=self.show_attendance
        )

        self.attendance_button.pack(
            fill="x",
            padx=20,
            pady=6
        )


        self.camera_button = ctk.CTkButton(
            self.sidebar,
            text="📷  Face Recognition",
            height=45,
            command=self.show_camera
        )

        self.camera_button.pack(
            fill="x",
            padx=20,
            pady=6
        )


        self.reports_button = ctk.CTkButton(
            self.sidebar,
            text="📊  Reports",
            height=45,
            command=self.show_reports
        )

        self.reports_button.pack(
            fill="x",
            padx=20,
            pady=6
        )


        self.settings_button = ctk.CTkButton(
            self.sidebar,
            text="⚙  Settings",
            height=45,
            command=self.show_settings
        )

        self.settings_button.pack(
            fill="x",
            padx=20,
            pady=6
        )


        # Logout

        self.logout_button = ctk.CTkButton(
            self.sidebar,
            text="🚪  Logout",
            height=45,
            command=self.destroy
        )

        self.logout_button.pack(
            side="bottom",
            fill="x",
            padx=20,
            pady=25
        )


    # ==========================================
    # CLEAR CONTENT
    # ==========================================

    def clear_content(self):

        for widget in self.content_frame.winfo_children():
            widget.destroy()


    # ==========================================
    # DASHBOARD
    # ==========================================

    def show_dashboard(self):

        self.clear_content()


        title = ctk.CTkLabel(
            self.content_frame,
            text="Dashboard",
            font=("Arial", 32, "bold")
        )

        title.pack(
            anchor="w",
            pady=(10, 5)
        )


        subtitle = ctk.CTkLabel(
            self.content_frame,
            text="Face Recognition Attendance Management System",
            font=("Arial", 15)
        )

        subtitle.pack(
            anchor="w",
            pady=(0, 25)
        )


        # Statistics

        stats = ctk.CTkFrame(
            self.content_frame,
            fg_color="transparent"
        )

        stats.pack(
            fill="x"
        )


        self.create_stat_card(
            stats,
            "TOTAL STUDENTS",
            "120"
        )


        self.create_stat_card(
            stats,
            "PRESENT TODAY",
            "108"
        )


        self.create_stat_card(
            stats,
            "ABSENT TODAY",
            "12"
        )


        # Recognition Section

        recognition = ctk.CTkFrame(
            self.content_frame,
            height=300
        )

        recognition.pack(
            fill="both",
            expand=True,
            pady=30
        )


        heading = ctk.CTkLabel(
            recognition,
            text="Face Recognition",
            font=("Arial", 24, "bold")
        )

        heading.pack(
            pady=(40, 10)
        )


        text = ctk.CTkLabel(
            recognition,
            text="Start the camera to automatically recognize students.",
            font=("Arial", 15)
        )

        text.pack()


        start_button = ctk.CTkButton(
            recognition,
            text="📷  START RECOGNITION",
            width=260,
            height=50,
            font=("Arial", 15, "bold"),
            command=self.show_camera
        )

        start_button.pack(
            pady=35
        )


    # ==========================================
    # STAT CARD
    # ==========================================

    def create_stat_card(self, parent, title, value):

        card = ctk.CTkFrame(
            parent,
            height=120
        )

        card.pack(
            side="left",
            fill="both",
            expand=True,
            padx=7
        )


        ctk.CTkLabel(
            card,
            text=title,
            font=("Arial", 13)
        ).pack(
            anchor="w",
            padx=20,
            pady=(20, 5)
        )


        ctk.CTkLabel(
            card,
            text=value,
            font=("Arial", 30, "bold")
        ).pack(
            anchor="w",
            padx=20
        )


    # ==========================================
    # STUDENTS PAGE
    # ==========================================

    def show_students(self):

        self.clear_content()


        title = ctk.CTkLabel(
            self.content_frame,
            text="Students",
            font=("Arial", 32, "bold")
        )

        title.pack(
            anchor="w",
            pady=(10, 5)
        )


        subtitle = ctk.CTkLabel(
            self.content_frame,
            text="Manage registered students",
            font=("Arial", 15)
        )

        subtitle.pack(
            anchor="w",
            pady=(0, 25)
        )


        # Top controls

        controls = ctk.CTkFrame(
            self.content_frame,
            fg_color="transparent"
        )

        controls.pack(
            fill="x",
            pady=10
        )


        search = ctk.CTkEntry(
            controls,
            placeholder_text="Search student...",
            width=300,
            height=40
        )

        search.pack(
            side="left"
        )


        add_button = ctk.CTkButton(
            controls,
            text="+  Add Student",
            width=150,
            height=40,
            command=self.show_add_student
        )

        add_button.pack(
            side="right"
        )


        # Student table area

        table = ctk.CTkFrame(
            self.content_frame
        )

        table.pack(
            fill="both",
            expand=True,
            pady=20
        )


        headers = [
            "ID",
            "Student Name",
            "Department",
            "Year",
            "Status"
        ]


        for column, header in enumerate(headers):

            label = ctk.CTkLabel(
                table,
                text=header,
                font=("Arial", 14, "bold")
            )

            label.grid(
                row=0,
                column=column,
                padx=25,
                pady=20,
                sticky="w"
            )


        # Example students

        students = [
            ("001", "Lorin Tedros", "CSE", "3rd Year", "Registered"),
            ("002", "Student 02", "CSE", "3rd Year", "Registered"),
            ("003", "Student 03", "CSE", "3rd Year", "Registered")
        ]


        for row, student in enumerate(students, start=1):

            for column, value in enumerate(student):

                label = ctk.CTkLabel(
                    table,
                    text=value,
                    font=("Arial", 13)
                )

                label.grid(
                    row=row,
                    column=column,
                    padx=25,
                    pady=15,
                    sticky="w"
                )


    # ==========================================
    # ATTENDANCE PAGE
    # ==========================================

    def show_attendance(self):

        self.clear_content()


        title = ctk.CTkLabel(
            self.content_frame,
            text="Attendance",
            font=("Arial", 32, "bold")
        )

        title.pack(
            anchor="w",
            pady=(10, 5)
        )


        subtitle = ctk.CTkLabel(
            self.content_frame,
            text="View today's attendance records",
            font=("Arial", 15)
        )

        subtitle.pack(
            anchor="w",
            pady=(0, 30)
        )


        attendance_box = ctk.CTkFrame(
            self.content_frame
        )

        attendance_box.pack(
            fill="both",
            expand=True
        )


        ctk.CTkLabel(
            attendance_box,
            text="No attendance records yet.",
            font=("Arial", 18)
        ).pack(
            pady=100
        )


    # ==========================================
    # CAMERA PAGE
    # ==========================================

    def show_camera(self):

        self.clear_content()


        title = ctk.CTkLabel(
            self.content_frame,
            text="Face Recognition",
            font=("Arial", 32, "bold")
        )

        title.pack(
            anchor="w",
            pady=(10, 5)
        )


        subtitle = ctk.CTkLabel(
            self.content_frame,
            text="Recognize students and mark attendance automatically",
            font=("Arial", 15)
        )

        subtitle.pack(
            anchor="w",
            pady=(0, 20)
        )


        camera_box = ctk.CTkFrame(
            self.content_frame,
            height=400
        )

        camera_box.pack(
            fill="both",
            expand=True,
            pady=20
        )


        ctk.CTkLabel(
            camera_box,
            text="📷\n\nCAMERA PREVIEW",
            font=("Arial", 25, "bold")
        ).pack(
            expand=True
        )


        start = ctk.CTkButton(
            self.content_frame,
            text="START CAMERA",
            width=220,
            height=45
        )

        start.pack(
            pady=10
        )


    # ==========================================
    # REPORTS PAGE
    # ==========================================

    def show_reports(self):

        self.clear_content()


        title = ctk.CTkLabel(
            self.content_frame,
            text="Reports",
            font=("Arial", 32, "bold")
        )

        title.pack(
            anchor="w",
            pady=(10, 5)
        )


        subtitle = ctk.CTkLabel(
            self.content_frame,
            text="Attendance reports and statistics",
            font=("Arial", 15)
        )

        subtitle.pack(
            anchor="w",
            pady=(0, 30)
        )


        report_box = ctk.CTkFrame(
            self.content_frame
        )

        report_box.pack(
            fill="both",
            expand=True
        )


        ctk.CTkLabel(
            report_box,
            text="Reports will appear here.",
            font=("Arial", 18)
        ).pack(
            pady=100
        )


    # ==========================================
    # SETTINGS PAGE
    # ==========================================

    def show_settings(self):

        self.clear_content()


        title = ctk.CTkLabel(
            self.content_frame,
            text="Settings",
            font=("Arial", 32, "bold")
        )

        title.pack(
            anchor="w",
            pady=(10, 5)
        )


        subtitle = ctk.CTkLabel(
            self.content_frame,
            text="Application settings",
            font=("Arial", 15)
        )

        subtitle.pack(
            anchor="w",
            pady=(0, 30)
        )


        appearance = ctk.CTkFrame(
            self.content_frame
        )

        appearance.pack(
            fill="x",
            pady=10
        )


        ctk.CTkLabel(
            appearance,
            text="Appearance Mode",
            font=("Arial", 18, "bold")
        ).pack(
            anchor="w",
            padx=25,
            pady=(20, 5)
        )


        mode = ctk.CTkOptionMenu(
            appearance,
            values=["Dark", "Light", "System"],
            command=ctk.set_appearance_mode
        )

        mode.set("Dark")

        mode.pack(
            anchor="w",
            padx=25,
            pady=(5, 20)
        )


# ==========================================
# RUN APPLICATION
# ==========================================

if __name__ == "__main__":

    app = FaceAttendanceApp()

    app.mainloop()
