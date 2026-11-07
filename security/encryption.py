
from cryptography.fernet import Fernet, InvalidToken
from pathlib import Path

# Store the encryption key inside the security folder
KEY_FILE = Path(__file__).parent / "secret.key"


def load_key():
    """Load an existing encryption key or create a new one."""

    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()

    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)

    return key


# Initialize encryption
fernet = Fernet(load_key())


def encrypt_data(data):
    """Encrypt text data."""

    if not isinstance(data, str):
        raise TypeError("Data must be a string")

    encrypted = fernet.encrypt(data.encode("utf-8"))
    return encrypted.decode("utf-8")


def decrypt_data(encrypted_data):
    """Decrypt previously encrypted text."""

    try:
        decrypted = fernet.decrypt(
            encrypted_data.encode("utf-8")
        )
        return decrypted.decode("utf-8")

    except InvalidToken:
        raise ValueError("Invalid data or encryption key")


if __name__ == "__main__":
    original = "AttendIQ Student Information"

    encrypted = encrypt_data(original)
    decrypted = decrypt_data(encrypted)

    print("Original:", original)
    print("Encrypted:", encrypted)
    print("Decrypted:", decrypted)
