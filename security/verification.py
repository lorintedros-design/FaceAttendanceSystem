
import hashlib
import json

from blockchain import AttendanceBlockchain


class AttendanceVerification:

    def __init__(self):
        self.blockchain = AttendanceBlockchain()

    # ----------------------------------------
    # 1. VERIFY BLOCKCHAIN
    # ----------------------------------------
    def verify_blockchain(self):
        """Verify all blockchain hashes and connections."""

        return self.blockchain.verify_chain()

    # ----------------------------------------
    # 2. GENERATE ATTENDANCE HASH
    # ----------------------------------------
    def generate_record_hash(self, attendance_record):
        """Generate SHA-256 hash for an attendance record."""

        if not isinstance(attendance_record, dict):
            raise TypeError("Attendance record must be a dictionary")

        record_text = json.dumps(
            attendance_record,
            sort_keys=True,
            separators=(",", ":")
        )

        return hashlib.sha256(
            record_text.encode("utf-8")
        ).hexdigest()

    # ----------------------------------------
    # 3. VERIFY ATTENDANCE RECORD
    # ----------------------------------------
    def verify_attendance_record(
        self,
        attendance_record,
        stored_hash
    ):
        """Compare an attendance record with its stored hash."""

        current_hash = self.generate_record_hash(
            attendance_record
        )

        return current_hash == stored_hash

    # ----------------------------------------
    # 4. GET BLOCKCHAIN STATUS
    # ----------------------------------------
    def get_status(self):

        total_blocks = self.blockchain.get_total_blocks()

        is_valid = self.verify_blockchain()

        return {
            "status": "VALID" if is_valid else "INVALID",
            "total_blocks": total_blocks,
            "message": (
                "Blockchain integrity verified."
                if is_valid
                else "Blockchain modification detected."
            )
        }


# --------------------------------------------
# TEST VERIFICATION
# --------------------------------------------
if __name__ == "__main__":

    verifier = AttendanceVerification()

    result = verifier.get_status()

    print("\n========== ATTENDIQ SECURITY CHECK ==========")

    print("Blockchain Status:", result["status"])

    print("Total Blocks:", result["total_blocks"])

    print("Message:", result["message"])

    print("=============================================\n")
