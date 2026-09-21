import cv2
import os

# Get student details
student_id = input("Enter Student ID: ")
student_name = input("Enter Student Name: ")

# Create images folder if it doesn't exist
os.makedirs("images", exist_ok=True)

# Image file name
filename = f"images/{student_id}_{student_name}.jpg"

# Load face detection model
face_detector = cv2.CascadeClassifier(
    "models/haarcascade_frontalface_default.xml"
)

# Check model
if face_detector.empty():
    print("ERROR: Face detection model not found!")
    exit()

# Open webcam
camera = cv2.VideoCapture(0)

if not camera.isOpened():
    print("ERROR: Camera could not be opened!")
    exit()

print("\nCamera started.")
print("Look at the camera.")
print("Press S to save your photo.")
print("Press Q to quit.")

while True:

    success, frame = camera.read()

    if not success:
        print("Could not read camera.")
        break

    # Convert to grayscale
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Detect faces
    faces = face_detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(30, 30)
    )

    # Draw rectangle around detected faces
    for (x, y, w, h) in faces:
        cv2.rectangle(
            frame,
            (x, y),
            (x + w, y + h),
            (255, 0, 0),
            2
        )

    # Show number of detected faces
    cv2.putText(
        frame,
        f"Faces detected: {len(faces)}",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 255, 0),
        2
    )

    # Show camera
    cv2.imshow("Student Registration", frame)

    # Get keyboard input
    key = cv2.waitKey(1) & 0xFF

    # Press S
    if key == ord("s"):

        print("S key pressed!")

        if len(faces) == 1:

            # Save the complete camera frame
            success_save = cv2.imwrite(filename, frame)

            if success_save:
                print("\n================================")
                print("PHOTO SAVED SUCCESSFULLY!")
                print("File:", filename)
                print("================================")
            else:
                print("ERROR: Photo could not be saved.")

            break

        elif len(faces) == 0:
            print("No face detected. Please look at the camera.")

        else:
            print("Multiple faces detected. Only one person should be visible.")

    # Press Q
    elif key == ord("q"):
        print("Registration cancelled.")
        break

# Close camera
camera.release()
cv2.destroyAllWindows()