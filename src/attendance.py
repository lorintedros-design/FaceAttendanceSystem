
import csv
import sys
from pathlib import Path
from datetime import datetime

# Locate the main AttendIQ project folder
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Ensure the project folder is available for imports
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from security.secure_attendance import (
    create_secure_table,
    record_secure_attendance
)

# CSV attendance file
ATTENDANCE_FILE = PROJECT_DIR / "attendance" / "attendance.csv"

# Create attendance folder
ATTENDANCE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Create CSV file if it does not exist
if not ATTENDANCE_FILE.exists():
    with open(ATTENDANCE_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["Student ID", "Name", "Date", "Time"])


def mark_attendance(student_name):

    # Get student ID and name
    try:
        student_id, name = student_name.split("_", 1)
    except ValueError:
        print("Invalid student format. Expected ID_Name.")
        return

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M:%S")

    # Check whether attendance is already marked today
    with open(ATTENDANCE_FILE, "r", newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        next(reader, None)

        for row in reader:
            if row and row[0] == student_id and row[2] == today:
                print("Attendance already marked.")
                return

    # First, save the encrypted record and blockchain entry
    try:
        create_secure_table()
        record_secure_attendance(
            student_id,
            name,
            status="Present"
        )

    except Exception as error:
        print("Secure attendance failed:", error)
        print("Attendance was not added to the CSV.")
        return

    # Save the normal attendance record
    with open(ATTENDANCE_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            student_id,
            name,
            today,
            current_time
        ])

    print("Attendance marked successfully!")
    print("Encrypted attendance saved successfully!")
    print("Blockchain record added successfully!")
