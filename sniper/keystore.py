"""Encrypted keypair storage.

The signing key never sits on disk in plaintext. The keystore is a small JSON
file holding an scrypt-derived AES-256-GCM ciphertext of the 64-byte ed25519
secret key, plus the public key in the clear so the file can be identified
without unlocking it.

    scrypt(password, salt, n, r, p) -> 32-byte key
    AES-256-GCM(key, nonce) -> ciphertext of the secret key

The public key is bound in as GCM associated data, so an attacker cannot swap
the recorded pubkey without the decrypt failing.

Decryption happens exactly once, at startup, well off the hot path.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

KEYSTORE_VERSION = 1

# ~64 MB of scrypt memory: enough to make offline guessing expensive, small
# enough to unlock on a modest VPS in well under a second.
DEFAULT_SCRYPT_N = 1 << 16
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1


class KeystoreError(Exception):
    """Raised when a keystore cannot be read, decrypted or trusted."""


@dataclass(frozen=True, slots=True)
class ScryptParams:
    n: int = DEFAULT_SCRYPT_N
    r: int = DEFAULT_SCRYPT_R
    p: int = DEFAULT_SCRYPT_P

    def to_dict(self) -> dict:
        return {"n": self.n, "r": self.r, "p": self.p}

    @classmethod
    def from_dict(cls, data: dict) -> "ScryptParams":
        try:
            params = cls(n=int(data["n"]), r=int(data["r"]), p=int(data["p"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise KeystoreError(f"malformed kdf_params: {exc}") from exc
        if params.n < 1 << 12 or params.n & (params.n - 1):
            raise KeystoreError("kdf_params.n must be a power of two >= 4096")
        if not 1 <= params.r <= 32 or not 1 <= params.p <= 16:
            raise KeystoreError("kdf_params.r/p out of supported range")
        return params


def _derive_key(password: str, salt: bytes, params: ScryptParams) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=params.n, r=params.r, p=params.p)
    return kdf.derive(password.encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str, field: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except Exception as exc:
        raise KeystoreError(f"{field} is not valid base64") from exc


def create_keystore(
    path: Path,
    secret_key: bytes,
    password: str,
    params: Optional[ScryptParams] = None,
    overwrite: bool = False,
) -> Keypair:
    """Encrypt `secret_key` (64 raw ed25519 bytes) to `path`.

    Returns the keypair so a caller can display the public key. The file is
    created 0600.
    """
    if len(secret_key) != 64:
        raise KeystoreError(
            f"expected a 64-byte ed25519 secret key, got {len(secret_key)} bytes"
        )
    if not password:
        raise KeystoreError("refusing to create a keystore with an empty password")
    if path.exists() and not overwrite:
        raise KeystoreError(f"{path} already exists (pass --force to replace it)")

    keypair = Keypair.from_bytes(secret_key)
    pubkey = str(keypair.pubkey())

    params = params or ScryptParams()
    salt = os.urandom(32)
    nonce = os.urandom(12)
    key = _derive_key(password, salt, params)
    try:
        ciphertext = AESGCM(key).encrypt(nonce, secret_key, pubkey.encode("ascii"))
    finally:
        key = b"\x00" * 32

    document = {
        "version": KEYSTORE_VERSION,
        "pubkey": pubkey,
        "kdf": "scrypt",
        "kdf_params": params.to_dict(),
        "salt": _b64(salt),
        "cipher": "aes-256-gcm",
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the outset rather than chmod-ing after writing,
    # which would leave a window where the file is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    return keypair


def load_keypair(path: Path, password: str) -> Keypair:
    """Decrypt a keystore into a :class:`Keypair`."""
    if not path.exists():
        raise KeystoreError(f"keystore not found: {path}")

    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeystoreError(
            f"{path} is accessible to other users (mode {stat.filemode(mode)}); "
            f"run: chmod 600 {path}"
        )

    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise KeystoreError(f"{path} is not valid JSON: {exc}") from exc

    if document.get("version") != KEYSTORE_VERSION:
        raise KeystoreError(
            f"unsupported keystore version {document.get('version')!r}"
        )
    if document.get("kdf") != "scrypt" or document.get("cipher") != "aes-256-gcm":
        raise KeystoreError("unsupported kdf/cipher combination")

    pubkey = document.get("pubkey")
    if not isinstance(pubkey, str):
        raise KeystoreError("keystore is missing its pubkey field")

    params = ScryptParams.from_dict(document.get("kdf_params", {}))
    salt = _unb64(document.get("salt", ""), "salt")
    nonce = _unb64(document.get("nonce", ""), "nonce")
    ciphertext = _unb64(document.get("ciphertext", ""), "ciphertext")

    key = _derive_key(password, salt, params)
    try:
        secret = AESGCM(key).decrypt(nonce, ciphertext, pubkey.encode("ascii"))
    except InvalidTag as exc:
        raise KeystoreError(
            "could not decrypt keystore — wrong password, or the file has been "
            "modified"
        ) from exc
    finally:
        key = b"\x00" * 32

    keypair = Keypair.from_bytes(secret)
    if str(keypair.pubkey()) != pubkey:
        raise KeystoreError("keystore pubkey does not match its decrypted secret key")
    return keypair


def read_password(env_var: str, prompt: str = "Keystore password: ") -> str:
    """Take the password from `env_var`, falling back to an interactive prompt."""
    password = os.environ.get(env_var)
    if password:
        return password
    if not os.isatty(0):
        raise KeystoreError(
            f"no password available: set {env_var} or run attached to a terminal"
        )
    return getpass.getpass(prompt)


def parse_secret_key(text: str) -> bytes:
    """Accept the two formats wallets actually export.

    * base58, 64 bytes — what Phantom/Solflare "export private key" produces.
    * a JSON array of 64 integers — what `solana-keygen` writes.
    """
    text = text.strip()
    if text.startswith("["):
        try:
            values = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KeystoreError(f"not a valid JSON key array: {exc}") from exc
        if not isinstance(values, list) or len(values) != 64:
            raise KeystoreError("JSON key array must contain exactly 64 integers")
        try:
            return bytes(values)
        except (TypeError, ValueError) as exc:
            raise KeystoreError(f"JSON key array has non-byte values: {exc}") from exc

    import base58

    try:
        decoded = base58.b58decode(text)
    except Exception as exc:
        raise KeystoreError(f"not valid base58: {exc}") from exc
    if len(decoded) != 64:
        raise KeystoreError(
            f"base58 key decoded to {len(decoded)} bytes, expected 64"
        )
    return decoded
