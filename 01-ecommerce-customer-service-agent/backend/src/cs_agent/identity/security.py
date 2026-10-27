import hashlib
import hmac
import secrets


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return f"pbkdf2_sha256$600000${salt}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        method, count, salt, expected = encoded.split("$")
        if method != "pbkdf2_sha256" or count != "600000":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(count))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def csrf_for(token: str) -> str:
    return digest("cs-agent/csrf/v1:" + token)
