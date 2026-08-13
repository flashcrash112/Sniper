import json
import os
import stat

import pytest
from solders.keypair import Keypair

from sniper.keystore import (
    KeystoreError,
    ScryptParams,
    create_keystore,
    load_keypair,
    parse_secret_key,
)

# Cheap KDF parameters so the suite stays fast; production uses n=2**16.
FAST = ScryptParams(n=1 << 12, r=8, p=1)


@pytest.fixture
def keypair():
    return Keypair()


def test_round_trip(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "correct horse", params=FAST)
    loaded = load_keypair(path, "correct horse")
    assert loaded.pubkey() == keypair.pubkey()


def test_file_is_created_private(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)
    mode = path.stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_secret_key_is_not_on_disk_in_the_clear(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)
    blob = path.read_bytes()
    assert bytes(keypair) not in blob
    assert bytes(keypair)[:32] not in blob


def test_wrong_password_is_rejected(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "right", params=FAST)
    with pytest.raises(KeystoreError, match="wrong password"):
        load_keypair(path, "wrong")


def test_tampering_with_the_recorded_pubkey_is_detected(tmp_path, keypair):
    """The pubkey is GCM associated data, so it cannot be swapped."""
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)

    document = json.loads(path.read_text())
    document["pubkey"] = str(Keypair().pubkey())
    path.write_text(json.dumps(document))

    with pytest.raises(KeystoreError):
        load_keypair(path, "pw")


def test_tampering_with_the_ciphertext_is_detected(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)

    document = json.loads(path.read_text())
    raw = bytearray(__import__("base64").b64decode(document["ciphertext"]))
    raw[0] ^= 0xFF
    document["ciphertext"] = __import__("base64").b64encode(bytes(raw)).decode()
    path.write_text(json.dumps(document))

    with pytest.raises(KeystoreError):
        load_keypair(path, "pw")


def test_world_readable_keystore_is_refused(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)
    os.chmod(path, 0o644)
    with pytest.raises(KeystoreError, match="accessible to other users"):
        load_keypair(path, "pw")


def test_empty_password_is_refused(tmp_path, keypair):
    with pytest.raises(KeystoreError, match="empty password"):
        create_keystore(tmp_path / "key.json", bytes(keypair), "", params=FAST)


def test_existing_file_is_not_clobbered(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)
    with pytest.raises(KeystoreError, match="already exists"):
        create_keystore(path, bytes(Keypair()), "pw", params=FAST)
    create_keystore(path, bytes(Keypair()), "pw", params=FAST, overwrite=True)


def test_weak_kdf_params_are_refused(tmp_path, keypair):
    path = tmp_path / "key.json"
    create_keystore(path, bytes(keypair), "pw", params=FAST)
    document = json.loads(path.read_text())
    document["kdf_params"]["n"] = 2
    path.write_text(json.dumps(document))
    with pytest.raises(KeystoreError, match="power of two"):
        load_keypair(path, "pw")


# --- key import formats ----------------------------------------------------


def test_parse_base58_secret_key(keypair):
    import base58

    encoded = base58.b58encode(bytes(keypair)).decode()
    assert parse_secret_key(encoded) == bytes(keypair)


def test_parse_json_array_secret_key(keypair):
    encoded = json.dumps(list(bytes(keypair)))
    assert parse_secret_key(encoded) == bytes(keypair)


def test_parse_rejects_a_32_byte_key(keypair):
    import base58

    encoded = base58.b58encode(bytes(keypair)[:32]).decode()
    with pytest.raises(KeystoreError, match="32 bytes"):
        parse_secret_key(encoded)


def test_parse_rejects_junk():
    with pytest.raises(KeystoreError):
        parse_secret_key("this is not a key")
