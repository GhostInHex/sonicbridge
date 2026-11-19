"""Optional authenticated encryption for the entire message envelope."""
from hashlib import scrypt
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"\xffSB1\0"


class LockedMessage(ValueError):
    pass


def cipher(passphrase, salt):
    return AESGCM(scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1,
                         dklen=32, maxmem=64 * 1024 * 1024))


def seal(payload, passphrase, transfer):
    if not passphrase:
        return payload
    salt, nonce = token_bytes(16), token_bytes(12)
    return MAGIC + salt + nonce + cipher(passphrase, salt).encrypt(
        nonce, payload, MAGIC + transfer.to_bytes(8, "big"))


def unseal(payload, passphrase, transfer):
    if not payload.startswith(MAGIC):
        if passphrase:
            raise LockedMessage("Receiver requires encryption for this session")
        return payload
    if not passphrase or len(payload) < 49:
        raise LockedMessage("Enter the shared passphrase on the receiving device")
    salt, nonce = payload[5:21], payload[21:33]
    try:
        return cipher(passphrase, salt).decrypt(
            nonce, payload[33:], MAGIC + transfer.to_bytes(8, "big"))
    except InvalidTag as exc:
        raise LockedMessage("Encrypted message failed authentication") from exc
