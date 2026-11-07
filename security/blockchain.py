
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone


# Blockchain JSON storage
BLOCKCHAIN_FILE = Path(__file__).parent / "blockchain.json"


class AttendanceBlockchain:

    def __init__(self):
        self.chain = []
        self.load_chain()

    # ----------------------------------------
    # 1. CALCULATE BLOCK HASH
    # ----------------------------------------
    def calculate_hash(self, block):
        """
        Calculate a block hash.
        Supports both old data_hash and new record_hash formats.
        """

        if "record_hash" in block:
            hash_field = "record_hash"
            separators = (",", ":")

        elif "data_hash" in block:
            hash_field = "data_hash"
            separators = None

        else:
            raise ValueError("Invalid block: Missing record hash")

        block_data = {
            "index": block["index"],
            "timestamp": block["timestamp"],
            hash_field: block[hash_field],
            "previous_hash": block["previous_hash"]
        }

        encoded_data = json.dumps(
            block_data,
            sort_keys=True,
            separators=separators
        ).encode("utf-8")

        return hashlib.sha256(encoded_data).hexdigest()

    # ----------------------------------------
    # 2. CREATE GENESIS BLOCK
    # ----------------------------------------
    def create_genesis_block(self):

        block = {
            "index": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "record_hash": "GENESIS",
            "previous_hash": "0"
        }

        block["hash"] = self.calculate_hash(block)

        return block

    # ----------------------------------------
    # 3. LOAD BLOCKCHAIN
    # ----------------------------------------
    def load_chain(self):

        if not BLOCKCHAIN_FILE.exists():
            self.chain = [self.create_genesis_block()]
            self.save_chain()
            return

        try:
            with open(
                BLOCKCHAIN_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                self.chain = json.load(file)

            if not isinstance(self.chain, list) or not self.chain:
                raise ValueError("Invalid blockchain data")

        except (json.JSONDecodeError, ValueError) as error:

            raise RuntimeError(
                "Blockchain file is invalid. "
                "Please back up and inspect blockchain.json."
            ) from error

    # ----------------------------------------
    # 4. SAVE BLOCKCHAIN
    # ----------------------------------------
    def save_chain(self):

        with open(
            BLOCKCHAIN_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                self.chain,
                file,
                indent=4
            )

    # ----------------------------------------
    # 5. ADD ATTENDANCE RECORD
    # ----------------------------------------
    def add_attendance(self, attendance_record):

        if not isinstance(attendance_record, dict):
            raise TypeError(
                "Attendance record must be a dictionary"
            )

        # Convert attendance data into a consistent string
        record_text = json.dumps(
            attendance_record,
            sort_keys=True,
            separators=(",", ":")
        )

        # Generate attendance record hash
        record_hash = hashlib.sha256(
            record_text.encode("utf-8")
        ).hexdigest()

        previous_block = self.chain[-1]

        # Create new block
        block = {
            "index": len(self.chain),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "record_hash": record_hash,
            "previous_hash": previous_block["hash"]
        }

        # Generate block hash
        block["hash"] = self.calculate_hash(block)

        # Add block to chain
        self.chain.append(block)

        # Save blockchain
        self.save_chain()

        return block

    # ----------------------------------------
    # 6. VERIFY BLOCKCHAIN
    # ----------------------------------------
    def verify_chain(self):

        if not self.chain:
            return False

        try:

            for index, block in enumerate(self.chain):

                # Check required fields
                if not all(
                    key in block
                    for key in (
                        "index",
                        "timestamp",
                        "previous_hash",
                        "hash"
                    )
                ):
                    return False

                if "record_hash" not in block and "data_hash" not in block:
                    return False

                # Check block index
                if block["index"] != index:
                    return False

                # Check current block hash
                calculated_hash = self.calculate_hash(block)

                if block["hash"] != calculated_hash:
                    return False

                # Check first block
                if index == 0:

                    if block["previous_hash"] != "0":
                        return False

                    genesis_hash = block.get(
                        "record_hash",
                        block.get("data_hash")
                    )

                    if genesis_hash != "GENESIS":
                        return False

                else:

                    previous_block = self.chain[index - 1]

                    # Check connection to previous block
                    if block["previous_hash"] != previous_block["hash"]:
                        return False

        except (KeyError, TypeError, ValueError):
            return False

        return True

    # ----------------------------------------
    # 7. GET ALL BLOCKS
    # ----------------------------------------
    def get_chain(self):
        return self.chain

    # ----------------------------------------
    # 8. GET TOTAL BLOCKS
    # ----------------------------------------
    def get_total_blocks(self):
        return len(self.chain)


# --------------------------------------------
# TEST BLOCKCHAIN
# --------------------------------------------
if __name__ == "__main__":

    blockchain = AttendanceBlockchain()

    print("\n========== ATTENDIQ BLOCKCHAIN ==========")

    print(
        "Total Blocks:",
        blockchain.get_total_blocks()
    )

    if blockchain.verify_chain():

        print("Blockchain Verification: SUCCESS")
        print("All block hashes and links are valid.")

    else:

        print("Blockchain Verification: FAILED")
        print("Possible data modification detected.")

    print("=========================================\n")
