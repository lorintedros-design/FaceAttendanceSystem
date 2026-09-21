import face_recognition
import os
import pickle

# Folder containing student photos
image_folder = "images"

known_faces = []
known_names = []

# Read all images
for filename in os.listdir(image_folder):

    if filename.endswith(".jpg"):

        image_path = os.path.join(image_folder, filename)

        # Load image
        image = face_recognition.load_image_file(image_path)

        # Find face
        encodings = face_recognition.face_encodings(image)

        if len(encodings) == 1:

            known_faces.append(encodings[0])

            # Get student name from filename
            name = filename.replace(".jpg", "")
            known_names.append(name)

            print("Face encoded:", name)

        else:
            print("Could not find exactly one face:", filename)

# Save encodings
data = {
    "faces": known_faces,
    "names": known_names
}

with open("face_encodings.pkl", "wb") as file:
    pickle.dump(data, file)

print("Face encoding completed!")