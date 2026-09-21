import cv2
import face_recognition
import pickle
import csv
import os
from datetime import datetime

# Load saved face encodings
with open("face_encodings.pkl", "rb") as file:
    data = pickle.load(file)

known_faces = data["faces"]
known_names = data["names"]

# Attendance file
attendance_file = "attendance/attendance.csv"
os.makedirs("attendance", exist_ok=True)

# Create attendance file if it doesn't exist
if not os.path.exists(attendance_file):
    with open(attendance_file, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Student ID", "Name", "Date", "Time"])


def mark_attendance(student_name):

    student_id, name = student_name.split("_", 1)

    today = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M:%S")

    # Check if already marked today
    with open(attendance_file, "r", newline="") as file:
        reader = csv.reader(file)
        next(reader, None)

        for row in reader:
            if row and row[0] == student_id and row[2] == today:
                return

    # Add attendance
    with open(attendance_file, "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([student_id, name, today, current_time])

    print(f"Attendance marked: {student_id} - {name}")


# Open camera
camera = cv2.VideoCapture(0)

print("Camera started.")
print("Look at the camera.")
print("Press Q to quit.")

while True:

    success, frame = camera.read()

    if not success:
        print("Could not read camera.")
        break

    # Convert image
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    # Find faces
    face_locations = face_recognition.face_locations(rgb_frame)

    # Create encodings
    face_encodings = face_recognition.face_encodings(
        rgb_frame,
        face_locations
    )

    # Check every detected face
    for face_encoding, face_location in zip(
        face_encodings,
        face_locations
    ):

        matches = face_recognition.compare_faces(
            known_faces,
            face_encoding,
            tolerance=0.5
        )

        name = "Unknown"

        if True in matches:
            index = matches.index(True)
            name = known_names[index]

            # Automatically mark attendance
            mark_attendance(name)

        # Face position
        top, right, bottom, left = face_location

        # Draw rectangle
        cv2.rectangle(
            frame,
            (left, top),
            (right, bottom),
            (255, 0, 0),
            2
        )

        # Display name
        cv2.putText(
            frame,
            name,
            (left, top - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2
        )

    # Show camera
    cv2.imshow("Face Attendance System", frame)

    # Press Q to quit
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

camera.release()
cv2.destroyAllWindows()