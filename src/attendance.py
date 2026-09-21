import csv
import os
from datetime import datetime

attendance_file = "attendance/attendance.csv"

# Create attendance folder
os.makedirs("attendance", exist_ok=True)

# Create CSV file if it doesn't exist
if not os.path.exists(attendance_file):
    with open(attendance_file, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Student ID", "Name", "Date", "Time"])

def mark_attendance(student_name):
    # Get ID and name
    student_id, name = student_name.split("_", 1)

    today = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M:%S")

    # Check if already marked today
    with open(attendance_file, "r", newline="") as file:
        reader = csv.reader(file)
        next(reader, None)

        for row in reader:
            if row and row[0] == student_id and row[2] == today:
                print("Attendance already marked.")
                return

    # Add attendance
    with open(attendance_file, "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([student_id, name, today, current_time])

    print("Attendance marked successfully!")


