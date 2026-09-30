from app.encryption import (
    decrypt_sha256,
    derive_content_id,
    derive_file_key,
    derive_key_check,
    derive_master_key,
    derive_sha256_seal_key,
    encrypt_sha256,
)


def test_master_key_is_deterministic_for_same_password_and_salt():
    salt = b"fixed-salt-16by."
    assert derive_master_key("hunter2", salt) == derive_master_key("hunter2", salt)


def test_master_key_differs_for_different_passwords():
    salt = b"fixed-salt-16by."
    assert derive_master_key("hunter2", salt) != derive_master_key("different", salt)


def test_master_key_differs_for_different_salts():
    assert derive_master_key("hunter2", b"salt-one........") != derive_master_key("hunter2", b"salt-two........")


def test_file_key_is_deterministic_and_unique_per_uuid():
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    key_a = derive_file_key(master_key, "uuid-a")
    key_a_again = derive_file_key(master_key, "uuid-a")
    key_b = derive_file_key(master_key, "uuid-b")

    assert key_a == key_a_again
    assert key_a != key_b


def test_file_key_never_equals_master_key():
    # The derived per-file key must not just be the master key reused verbatim.
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    assert derive_file_key(master_key, "some-uuid") != master_key


def test_content_id_is_deterministic_and_unique_per_sha256():
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    sha_a = "a" * 64
    sha_b = "b" * 64

    id_a = derive_content_id(master_key, sha_a)
    id_a_again = derive_content_id(master_key, sha_a)
    id_b = derive_content_id(master_key, sha_b)

    assert id_a == id_a_again
    assert id_a != id_b


def test_content_id_differs_across_key_versions():
    # Simulates a key rotation: same content, different master key -> different id.
    key_v1 = derive_master_key("hunter2", b"fixed-salt-16by.")
    key_v2 = derive_master_key("new-password!!!", b"fixed-salt-16by.")
    sha = "c" * 64

    assert derive_content_id(key_v1, sha) != derive_content_id(key_v2, sha)


def test_content_id_differs_from_file_key_for_same_inputs():
    # Label separation: content_id and the per-file AES key must not collide
    # even though both are HMAC-SHA256 over (master_key, same sha256 string).
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    sha = "d" * 64
    assert derive_content_id(master_key, sha) != derive_file_key(master_key, sha).hex()


def test_sha256_seal_roundtrips():
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    seal_key = derive_sha256_seal_key(master_key)
    content_id = "e" * 64
    sha256 = "f" * 64

    token = encrypt_sha256(seal_key, content_id, sha256)
    assert decrypt_sha256(seal_key, content_id, token) == sha256


def test_sha256_seal_rejects_wrong_content_id_as_aad():
    import pytest
    from cryptography.exceptions import InvalidTag

    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    seal_key = derive_sha256_seal_key(master_key)
    token = encrypt_sha256(seal_key, "e" * 64, "f" * 64)

    with pytest.raises(InvalidTag):
        decrypt_sha256(seal_key, "0" * 64, token)


def test_sha256_seal_key_differs_from_file_key_and_content_id():
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    seal_key = derive_sha256_seal_key(master_key)
    assert seal_key != master_key
    assert seal_key != derive_file_key(master_key, "a" * 64)


def test_key_check_is_deterministic_for_same_key():
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    assert derive_key_check(master_key) == derive_key_check(master_key)


def test_key_check_differs_across_keys():
    key_v1 = derive_master_key("hunter2", b"fixed-salt-16by.")
    key_v2 = derive_master_key("new-password!!!", b"fixed-salt-16by.")
    assert derive_key_check(key_v1) != derive_key_check(key_v2)


def test_key_check_differs_from_related_derivations_for_same_key():
    # Label separation: key_check must not collide with any other derivation
    # off the same master_key.
    master_key = derive_master_key("hunter2", b"fixed-salt-16by.")
    sha = "a" * 64
    key_check = derive_key_check(master_key)
    assert key_check != derive_content_id(master_key, sha)
    assert key_check != derive_file_key(master_key, sha).hex()[:16]
    assert key_check != derive_sha256_seal_key(master_key).hex()[:16]


