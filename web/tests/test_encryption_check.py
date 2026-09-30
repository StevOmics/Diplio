"""check_key_match: pure logic over synthetic index.json contents, no bucket
or database - same style as test_cloud_inventory.py."""
import base64
import hashlib
import json

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.encryption_check import check_key_match, derive_file_key, derive_key_check, derive_master_key, derive_sha256_seal_key

RIGHT_PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "wrong password entirely"
SALT = b"\x01" * 16


def _seal(master_key: bytes, sha256: str, paths: list[str]) -> str:
    """Mirrors worker/app/encryption.py:encrypt_paths, without importing it -
    the web side only ever needs to decrypt, this just builds test fixtures."""
    file_key = derive_file_key(master_key, sha256)
    nonce = b"\x00" * 12
    sealed = AESGCM(file_key).encrypt(nonce, json.dumps(paths).encode(), sha256.encode())
    return base64.b64encode(nonce + sealed).decode()


def _sha(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _candidates(*passwords: str):
    return [(i, derive_master_key(p, SALT)) for i, p in enumerate(passwords)]


def _reader(objects: dict[str, bytes]):
    return lambda key: objects.get(key)


def test_no_content_when_no_index_files():
    assert check_key_match([], _reader({}), _candidates(RIGHT_PASSWORD)) == "no_content"


def test_unencrypted_index():
    sha = _sha("a")
    index = {"archive_id": "arc-1", "object": "vault/a.tar", "files": {sha: {"size": 1, "paths": ["a.txt"]}}}
    objects = {"vault/a.json": json.dumps(index).encode()}
    assert check_key_match(["vault/a.json"], _reader(objects), _candidates(RIGHT_PASSWORD)) == "unencrypted"


def test_match_with_correct_key():
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    sha = _sha("a")
    token = _seal(right, sha, ["a.txt"])
    index = {"archive_id": "arc-1", "object": "vault/a.tar", "files": {sha: {"size": 1, "paths_enc": token}}}
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(RIGHT_PASSWORD))
    assert result == "match"


def test_no_match_with_wrong_key():
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    sha = _sha("a")
    token = _seal(right, sha, ["a.txt"])
    index = {"archive_id": "arc-1", "object": "vault/a.tar", "files": {sha: {"size": 1, "paths_enc": token}}}
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(WRONG_PASSWORD))
    assert result == "no_match"


def test_mixed_when_one_archive_matches_and_another_does_not():
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    wrong = derive_master_key(WRONG_PASSWORD, SALT)
    sha_a, sha_b = _sha("a"), _sha("b")
    index_a = {
        "archive_id": "arc-a",
        "object": "vault/a.tar",
        "files": {sha_a: {"size": 1, "paths_enc": _seal(right, sha_a, ["a.txt"])}},
    }
    index_b = {
        "archive_id": "arc-b",
        "object": "vault/b.tar",
        "files": {sha_b: {"size": 1, "paths_enc": _seal(wrong, sha_b, ["b.txt"])}},
    }
    objects = {
        "vault/a.json": json.dumps(index_a).encode(),
        "vault/b.json": json.dumps(index_b).encode(),
    }
    result = check_key_match(["vault/a.json", "vault/b.json"], _reader(objects), _candidates(RIGHT_PASSWORD))
    assert result == "mixed"


def test_missing_or_malformed_index_is_ignored():
    assert check_key_match(["vault/missing.json"], _reader({}), _candidates(RIGHT_PASSWORD)) == "no_content"
    objects = {"vault/bad.json": b"not json"}
    assert check_key_match(["vault/bad.json"], _reader(objects), _candidates(RIGHT_PASSWORD)) == "no_content"


def _seal_sha256(seal_key: bytes, content_id: str, sha256: str) -> str:
    """Mirrors worker/app/encryption.py:encrypt_sha256, without importing it."""
    nonce = b"\x00" * 12
    sealed = AESGCM(seal_key).encrypt(nonce, sha256.encode(), content_id.encode())
    return base64.b64encode(nonce + sealed).decode()


def test_match_with_correct_key_for_content_id_keyed_index():
    # A content-id-keyed index's dict key is a keyed HMAC, not the real
    # sha256 - the fix must decrypt "sha256_enc" (keyed off the master key
    # directly), not treat the dict key as if it were sha256.
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    content_id = "c" * 64
    token = _seal_sha256(derive_sha256_seal_key(right), content_id, _sha("a"))
    index = {
        "archive_id": "arc-1",
        "object": "vault/a.tar",
        "key_scheme": "content_id_v1",
        "encrypted": True,
        "files": {content_id: {"size": 1, "paths_enc": "irrelevant", "sha256_enc": token}},
    }
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(RIGHT_PASSWORD))
    assert result == "match"


def test_no_match_with_wrong_key_for_content_id_keyed_index():
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    content_id = "c" * 64
    token = _seal_sha256(derive_sha256_seal_key(right), content_id, _sha("a"))
    index = {
        "archive_id": "arc-1",
        "object": "vault/a.tar",
        "key_scheme": "content_id_v1",
        "encrypted": True,
        "files": {content_id: {"size": 1, "paths_enc": "irrelevant", "sha256_enc": token}},
    }
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(WRONG_PASSWORD))
    assert result == "no_match"


def test_match_via_key_check_without_a_real_sealed_entry():
    # key_check lets a candidate be confirmed even when no real sha256_enc
    # entry is present to trial-decrypt against.
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    index = {
        "archive_id": "arc-1",
        "object": "vault/a.tar",
        "key_scheme": "content_id_v1",
        "key_check": derive_key_check(right),
        "encrypted": True,
        "files": {"c" * 64: {"size": 1, "paths_enc": "irrelevant", "sha256_enc": "irrelevant-too"}},
    }
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(RIGHT_PASSWORD))
    assert result == "match"


def test_no_match_via_key_check_for_wrong_candidate():
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    index = {
        "archive_id": "arc-1",
        "object": "vault/a.tar",
        "key_scheme": "content_id_v1",
        "key_check": derive_key_check(right),
        "encrypted": True,
        "files": {"c" * 64: {"size": 1, "paths_enc": "irrelevant", "sha256_enc": "irrelevant-too"}},
    }
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(WRONG_PASSWORD))
    assert result == "no_match"


def test_falls_back_to_trial_decrypt_when_key_check_absent():
    # An index written before key_check existed (D14, no key_check field)
    # must still resolve via the old sha256_enc trial-decrypt path.
    right = derive_master_key(RIGHT_PASSWORD, SALT)
    content_id = "c" * 64
    token = _seal_sha256(derive_sha256_seal_key(right), content_id, _sha("a"))
    index = {
        "archive_id": "arc-1",
        "object": "vault/a.tar",
        "key_scheme": "content_id_v1",
        "encrypted": True,
        "files": {content_id: {"size": 1, "paths_enc": "irrelevant", "sha256_enc": token}},
    }
    objects = {"vault/a.json": json.dumps(index).encode()}
    result = check_key_match(["vault/a.json"], _reader(objects), _candidates(RIGHT_PASSWORD))
    assert result == "match"
