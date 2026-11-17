import base64
from cryptography.fernet import Fernet
from .config import get_settings


def token_cipher() -> Fernet:
    key = get_settings().app_encryption_key
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY must be set before storing Mercado Livre tokens")
    return Fernet(key.encode())


def encrypt(value: str) -> str: return token_cipher().encrypt(value.encode()).decode()
def decrypt(value: str) -> str: return token_cipher().decrypt(value.encode()).decode()
