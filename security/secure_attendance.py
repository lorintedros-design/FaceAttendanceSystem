
import sqlite3
import json
from datetime import datetime
from pathlib import Path

from .encryption import encrypt_data, decrypt_data
from security.blockchain import AttendIQBlockchain
from security.verification import verify_attendance_record


# Locate the existing AttendIQ database
PROJECT_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_DIR / "database" / "attendance.db"


def create_secure_table():
    """Create a separate table for encrypted attendance records."""

    connection = sqlite3.connect(DATABASE_PATH)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS secure_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            encrypted_data TEXT NOT NULL,
            block_index INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()

    print("Secure attendance table created successfully!")


def record_secure_attendance(student_id, student_name, status="Present"):
    """Encrypt attendance, save it in SQLite, and add its hash to blockchain."""

    now = datetime.now()

    attendance_data = {
        "student_id": student_id,
        "student_name": student_name,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "status": status
    }

    # Convert attendance details into JSON text
    attendance_json = json.dumps(attendance_data)

    # Encrypt attendance details
    encrypted_data = encrypt_data(attendance_json)

    # Add the encrypted record's hash to blockchain
    blockchain = AttendIQBlockchain()
    block_index = len(blockchain.chain)

    blockchain.add_attendance_record(encrypted_data)

    # Store encrypted data and its blockchain block index
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.execute("""
            INSERT INTO secure_attendance
            (encrypted_data, block_index, created_at)
            VALUES (?, ?, ?)
        """, (
            encrypted_data,
            block_index,
            now.isoformat()
        ))

        connection.commit()
        print("Secure attendance recorded successfully!")

    finally:
        connection.close()


def get_secure_attendance():
    """Retrieve and decrypt attendance records."""

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        records = connection.execute("""
            SELECT id, encrypted_data, block_index, created_at
            FROM secure_attendance
            ORDER BY id DESC
        """).fetchall()

    finally:
        connection.close()

    decrypted_records = []

    for record_id, encrypted_data, block_index, created_at in records:
        try:
            decrypted_data = json.loads(decrypt_data(encrypted_data))

            decrypted_records.append({
                "id": record_id,
                "attendance": decrypted_data,
                "block_index": block_index,
                "created_at": created_at
            })

        except Exception as error:
            decrypted_records.append({
                "id": record_id,
                "error": str(error)
            })

    return decrypted_records


def verify_secure_attendance():
    """Verify all saved encrypted attendance records."""

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        records = connection.execute("""
            SELECT id, encrypted_data, block_index
            FROM secure_attendance
        """).fetchall()

    finally:
        connection.close()

    results = []

    for record_id, encrypted_data, block_index in records:
        valid, message = verify_attendance_record(
            encrypted_data,
            block_index
        )

        results.append({
            "id": record_id,
            "valid": valid,
            "message": message
        })

    return results


if __name__ == "__main__":
    create_secure_table()
    print("AttendIQ secure attendance module is ready!")
