
import cv2
import face_recognition
import pickle
import sys
from pathlib import Path

# ==========================================
# 1. PROJECT PATH
# ==========================================

PROJECT_DIR = Path(__file__).resolve().parent.parent

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.attendance import mark_attendance


# ==========================================
# 2. LOAD FACE ENCODINGS
# ==========================================

possible_files = [
    PROJECT_DIR / "models" / "face_encodings.pkl",
    PROJECT_DIR / "face_encodings.pkl",
    PROJECT_DIR / "src" / "face_encodings.pkl"
]

ENCODING_FILE = next(
    (path for path in possible_files if path.exists()),
    None
)

if ENCODING_FILE is None:
    raise FileNotFoundError("Face encodings file not found!")

with open(ENCODING_FILE, "rb") as file:
    data = pickle.load(file)

known_faces = data["faces"]
known_names = data["names"]

print("Face encodings loaded successfully!")
print("Registered faces:", len(known_names))


# ==========================================
# 3. CAMERA SETTINGS
# ==========================================

camera = cv2.VideoCapture(0)

# Request a reasonable camera resolution
camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

if not camera.isOpened():
    print("Could not open camera.")
    raise SystemExit

print("\nAttendIQ Camera Started")
print("Look at the camera.")
print("Press Q to quit.")

# Recognition settings
FRAME_SCALE = 0.25
PROCESS_EVERY = 2

frame_count = 0
processed_students = set()

# Remember the previous recognition results
face_locations = []
face_names = []


# ==========================================
# 4. FACE RECOGNITION LOOP
# ==========================================

try:

    while True:

        success, frame = camera.read()

        if not success:
            print("Could not read camera.")
            break

        frame_count += 1

        # Process recognition only on selected frames
        if frame_count % PROCESS_EVERY == 0:

            # Resize frame to 25%
            small_frame = cv2.resize(
                frame,
                (0, 0),
                fx=FRAME_SCALE,
                fy=FRAME_SCALE
            )

            # Convert BGR to RGB
            rgb_frame = cv2.cvtColor(
                small_frame,
                cv2.COLOR_BGR2RGB
            )

            # Detect faces on smaller frame
            face_locations = face_recognition.face_locations(
                rgb_frame,
                model="hog"
            )

            # Generate face encodings
            face_encodings = face_recognition.face_encodings(
                rgb_frame,
                face_locations
            )

            face_names = []

            for face_encoding in face_encodings:

                name = "Unknown"

                if len(known_faces) > 0:

                    face_distances = face_recognition.face_distance(
                        known_faces,
                        face_encoding
                    )

                    best_match_index = face_distances.argmin()

                    if face_distances[best_match_index] <= 0.5:

                        name = known_names[best_match_index]

                        if name not in processed_students:

                            # Attempt attendance recording
                            mark_attendance(name)

                            processed_students.add(name)

                face_names.append(name)

        # ==================================
        # 5. DISPLAY RECOGNITION RESULTS
        # ==================================

        for face_location, name in zip(
            face_locations,
            face_names
        ):

            # Convert small-frame coordinates
            # back to original-frame coordinates
            top, right, bottom, left = face_location

            top = int(top / FRAME_SCALE)
            right = int(right / FRAME_SCALE)
            bottom = int(bottom / FRAME_SCALE)
            left = int(left / FRAME_SCALE)

            color = (
                (0, 255, 0)
                if name != "Unknown"
                else (0, 0, 255)
            )

            cv2.rectangle(
                frame,
                (left, top),
                (right, bottom),
                color,
                2
            )

            cv2.putText(
                frame,
                name,
                (left, max(top - 10, 20)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                color,
                2
            )

        cv2.imshow(
            "AttendIQ - Optimized Face Recognition",
            frame
        )

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:

    camera.release()
    cv2.destroyAllWindows()

    print("\nAttendIQ Camera Stopped.")
