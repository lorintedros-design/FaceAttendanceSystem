import cv2
import os

# Folder to save your images
person_name = "Lorin"
save_path = f"images/{person_name}"

os.makedirs(save_path, exist_ok=True)

camera = cv2.VideoCapture(0)

count = 0

while True:
    success, frame = camera.read()

    if not success:
        break

    cv2.imshow("Capture Faces", frame)

    key = cv2.waitKey(1)

    # Press 's' to save an image
    if key == ord('s'):
        filename = os.path.join(save_path, f"{count}.jpg")
        cv2.imwrite(filename, frame)
        print(f"Saved: {filename}")
        count += 1

    # Press 'q' to quit
    elif key == ord('q'):
        break

camera.release()
cv2.destroyAllWindows()