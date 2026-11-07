"""Read-only face matching against existing AttendIQ registration photos."""
from io import BytesIO
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import pickle

import face_recognition
import numpy as np
from PIL import Image, ImageOps

from web_portal import database

PROJECT_DIR = Path(__file__).resolve().parents[1]
IMAGE_DIR = PROJECT_DIR / "images"
FACE_CACHE_PATH = PROJECT_DIR / "database" / "face_cache.pkl"
MATCH_THRESHOLD = 0.55
MATCH_MARGIN = 0.025
MAX_IMAGE_PIXELS = 12_000_000
logger = logging.getLogger(__name__)
_DIAGNOSTIC_LOG_PATH = PROJECT_DIR / "reports" / "web_face_diagnostics.log"
try:
    _DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not any(
        getattr(handler, "baseFilename", None) == str(_DIAGNOSTIC_LOG_PATH.resolve())
        for handler in logger.handlers
    ):
        _diagnostic_handler = RotatingFileHandler(
            _DIAGNOSTIC_LOG_PATH, maxBytes=256 * 1024, backupCount=2, encoding="utf-8"
        )
        _diagnostic_handler.setLevel(logging.WARNING)
        _diagnostic_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(_diagnostic_handler)
except OSError:
    # Console logging remains available if local diagnostic files cannot be written.
    pass

_known_faces = None
_known_signature = None


def _photo_signature(paths):
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((path.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return tuple(result)


def _registered_ids():
    connection = database._connect_readonly()
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(students)")}
        if "id" not in columns:
            return set()
        return {int(row[0]) for row in connection.execute("SELECT id FROM students")}
    finally:
        connection.close()


def _load_known_faces():
    global _known_faces, _known_signature
    if not IMAGE_DIR.is_dir():
        return {}
    paths = sorted(
        path for path in IMAGE_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    signature = _photo_signature(paths)
    if _known_faces is not None and signature == _known_signature:
        return _known_faces

    registered = _registered_ids()
    try:
        with FACE_CACHE_PATH.open("rb") as cache_file:
            saved_cache = pickle.load(cache_file)
        if not isinstance(saved_cache, dict):
            saved_cache = {}
    except Exception:
        saved_cache = {}

    by_student = {}
    for path in paths:
        prefix = path.name.split("_", 1)[0]
        if not prefix.isdigit() or int(prefix) not in registered:
            continue
        student_id = int(prefix)
        encoding = None
        try:
            stat = path.stat()
            cached = saved_cache.get(path.name)
            if cached and cached[0] == (stat.st_mtime_ns, stat.st_size):
                encoding = cached[1]
            if encoding is None:
                image = face_recognition.load_image_file(str(path))
                locations = face_recognition.face_locations(
                    image, number_of_times_to_upsample=1, model="hog"
                )
                if len(locations) != 1:
                    continue
                encodings = face_recognition.face_encodings(
                    image, known_face_locations=locations, num_jitters=0
                )
                if not encodings:
                    continue
                encoding = encodings[0]
            by_student.setdefault(student_id, []).append(
                np.asarray(encoding, dtype=np.float64)
            )
        except Exception:
            continue

    _known_faces = by_student
    _known_signature = signature
    return by_student


def _face_locations_with_retry(rgb):
    """Retry a missed face at higher detail; never relax the one-face rule."""
    locations = face_recognition.face_locations(
        rgb, number_of_times_to_upsample=1, model="hog"
    )
    if not locations:
        locations = face_recognition.face_locations(
            rgb, number_of_times_to_upsample=2, model="hog"
        )
    return locations


def _matches_student_encoding(student_id: int, probe) -> bool:
    known = _load_known_faces()
    if not known:
        return False
    distances = {
        sid: float(np.min(face_recognition.face_distance(references, probe)))
        for sid, references in known.items()
    }
    ranking = sorted(distances.items(), key=lambda item: item[1])
    best_id, best_distance = ranking[0]
    second_distance = ranking[1][1] if len(ranking) > 1 else 1.0
    return (
        best_id == student_id
        and best_distance <= MATCH_THRESHOLD
        and (len(ranking) == 1 or second_distance - best_distance >= MATCH_MARGIN)
    )


def prepare_face_registration_photo(image_bytes: bytes) -> bytes:
    """Validate and normalize a staff-captured photo for an existing student profile."""
    if not image_bytes:
        raise ValueError("Take a photo before saving.")
    if len(image_bytes) > 4 * 1024 * 1024:
        raise ValueError("Photo is too large. Please take another photo.")
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            if source.format not in {"JPEG", "PNG"}:
                raise ValueError("Use a JPEG or PNG photo.")
            image = ImageOps.exif_transpose(source).convert("RGB")
        if image.width < 80 or image.height < 80 or image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("Photo dimensions are not supported. Please take another photo.")
        rgb = np.asarray(image)
        locations = _face_locations_with_retry(rgb)
        if len(locations) != 1:
            raise ValueError("The photo must show exactly one clear face.")
        top, right, bottom, left = locations[0]
        face_area = max(0, bottom - top) * max(0, right - left)
        if face_area / max(1, rgb.shape[0] * rgb.shape[1]) < 0.025:
            raise ValueError("Move closer so the student's face is larger in the photo.")
        if not face_recognition.face_encodings(
            rgb, known_face_locations=locations, num_jitters=1
        ):
            raise ValueError("The face is not clear enough. Use bright light and face the camera.")
        output = BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()
    except ValueError:
        raise
    except Exception as error:
        logger.warning("Mobile face photo validation could not process an image.")
        raise ValueError("Could not check this photo. Please take it again.") from error

def verify_student_face(student_id: int, image_bytes: bytes) -> bool:
    """Return whether one clear face matches the claimed existing student ID."""
    if student_id <= 0 or not image_bytes or len(image_bytes) > 3 * 1024 * 1024:
        return False
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format not in {"JPEG", "PNG"}:
                return False
            if image.width < 80 or image.height < 80:
                return False
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return False
            rgb = np.asarray(image.convert("RGB"))
    except Exception:
        return False

    try:
        locations = _face_locations_with_retry(rgb)
        if len(locations) != 1:
            return False
        top, right, bottom, left = locations[0]
        face_area = max(0, bottom - top) * max(0, right - left)
        if face_area / max(1, rgb.shape[0] * rgb.shape[1]) < 0.025:
            return False
        encodings = face_recognition.face_encodings(
            rgb, known_face_locations=locations, num_jitters=0
        )
        if not encodings:
            return False
    except Exception:
        return False

    return _matches_student_encoding(student_id, encodings[0])


def _eye_ratio(points):
    if len(points) != 6:
        return None
    vertical = (
        np.linalg.norm(np.asarray(points[1]) - np.asarray(points[5]))
        + np.linalg.norm(np.asarray(points[2]) - np.asarray(points[4]))
    )
    horizontal = 2 * np.linalg.norm(np.asarray(points[0]) - np.asarray(points[3]))
    return float(vertical / horizontal) if horizontal else None


def _blink_detected(ratios):
    values = [float(ratio) for ratio in ratios if ratio is not None and np.isfinite(ratio)]
    if len(values) < 5:
        return False
    ordered = sorted(values)
    open_baseline = ordered[int((len(ordered) - 1) * 0.75)]
    if open_baseline < 0.14:
        return False
    closed_threshold = min(0.20, open_baseline * 0.82)
    open_threshold = max(0.16, open_baseline * 0.80)
    delta_threshold = max(0.025, open_baseline * 0.12)
    for index, ratio in enumerate(values):
        if ratio > closed_threshold or index == 0 or index == len(values) - 1:
            continue
        before_open = max(values[:index], default=0.0)
        after_open = max(values[index + 1:], default=0.0)
        if (before_open >= open_threshold and after_open >= open_threshold
                and min(before_open, after_open) - ratio >= delta_threshold):
            return True
    return False


def _reject_liveness(reason: str) -> bool:
    """Log a local, non-identifying reason while keeping the student response generic."""
    logger.warning("AttendIQ web face verification rejected: %s", reason)
    return False


def verify_student_liveness(student_id: int, frame_bytes: list[bytes]) -> bool:
    """Match every captured frame to the claimed student and require a blink."""
    if student_id <= 0 or not 4 <= len(frame_bytes) <= 16:
        return _reject_liveness("invalid_capture_count")
    ratios = []
    for data in frame_bytes:
        if not data or len(data) > 512 * 1024:
            return _reject_liveness("frame_size_limit")
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG"} or image.width * image.height > MAX_IMAGE_PIXELS:
                    return _reject_liveness("frame_format_or_dimensions")
                rgb = np.asarray(image.convert("RGB"))
            locations = _face_locations_with_retry(rgb)
            if len(locations) != 1:
                return _reject_liveness(
                    f"expected_one_face_per_frame_count_{len(locations)}"
                )
            top, right, bottom, left = locations[0]
            if max(0, bottom - top) * max(0, right - left) / max(1, rgb.shape[0] * rgb.shape[1]) < 0.025:
                return _reject_liveness("face_too_small_in_frame")
            encodings = face_recognition.face_encodings(
                rgb, known_face_locations=locations, num_jitters=0
            )
            if not encodings:
                return _reject_liveness("face_encoding_unavailable")
            if not _matches_student_encoding(student_id, encodings[0]):
                return _reject_liveness("face_did_not_match_registered_student")
            landmarks = face_recognition.face_landmarks(rgb, locations)
            if not landmarks:
                return _reject_liveness("eye_landmarks_unavailable")
            left_eye = _eye_ratio(landmarks[0].get("left_eye", []))
            right_eye = _eye_ratio(landmarks[0].get("right_eye", []))
            if left_eye is None or right_eye is None:
                return _reject_liveness("eye_landmarks_unavailable")
            ratios.append((left_eye + right_eye) / 2)
        except Exception:
            return _reject_liveness("frame_processing_error")
    if not _blink_detected(ratios):
        finite_ratios = [ratio for ratio in ratios if np.isfinite(ratio)]
        spread = (f"min_{min(finite_ratios):.3f}_max_{max(finite_ratios):.3f}"
                  if finite_ratios else "no_valid_ratios")
        return _reject_liveness(f"blink_not_detected_{spread}")
    return True
