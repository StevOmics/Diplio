"""
AES-256-GCM encryption for backups.

Design:
- One master key is derived from the backup password via PBKDF2-HMAC-SHA256,
  using a salt generated once when the password is first set (not secret,
  just needs to be fixed).
- Each file gets its own encryption key, derived deterministically from the
  master key + that file's SHA-256 (HMAC-SHA256). Nothing per-file needs to be
  stored to decrypt later - just the password (or its key version) and the
  file's hash (already in the catalog), so there's no separate key store to
  keep in sync or lose.
- A file is encrypted into one blob (see the blob section below) *before* it
  is clumped or split into archives, in independently authenticated chunks, so
  a corrupt or tampered chunk fails on its own with InvalidTag rather than
  silently producing wrong bytes.
- AES-GCM needs a unique (key, nonce) pair per encryption. The key is fixed per
  file, so each chunk uses nonce = <4-byte random prefix, fresh per blob><8-byte
  big-endian chunk index>; re-backing up a changed file gets a fresh prefix, so
  nonces never repeat for a given key. The prefix isn't secret and is stored in
  the blob's header.
"""
import base64
import hashlib
import hmac
import json
import os
import struct
from pathlib import Path
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KDF_ITERATIONS = 200_000
NONCE_PREFIX_SIZE = 4
NONCE_COUNTER_SIZE = 8

ProgressCallback = Optional[Callable[[int, int], None]]


def derive_master_key(password: str, kdf_salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), kdf_salt, KDF_ITERATIONS, dklen=32)


def derive_file_key(master_key: bytes, file_uuid: str) -> bytes:
    return hmac.new(master_key, b"mediabridge-backup-file-key:" + file_uuid.encode(), hashlib.sha256).digest()


def derive_content_id(master_key: bytes, sha256: str) -> str:
    """Keyed content identifier used anywhere a hash is exposed outside the
    database (index JSON keys, encrypted tar member names). A label-separated
    HMAC from derive_file_key, not a shared derivation - unlike plain SHA-256,
    no one without master_key can compute this for a candidate file, so it
    can't be used to confirm we hold a given piece of content."""
    return hmac.new(master_key, b"mediabridge-content-id:" + sha256.encode(), hashlib.sha256).hexdigest()


def derive_key_check(master_key: bytes) -> str:
    """A short, per-key verifier stored once per archive (not per entry) so a
    candidate password can be confirmed against an archive without a
    database and without trial-decrypting a real entry. Doesn't introduce a
    new offline-guessing oracle beyond what already exists: sha256_enc's AEAD
    tag already fails loudly on a wrong key, so trial-decryption was already
    a functioning, if slower, oracle. This just makes checking a candidate
    cheap and explicit instead of requiring a real sealed entry to test
    against."""
    return hmac.new(master_key, b"mediabridge-key-check", hashlib.sha256).hexdigest()[:16]


def _nonce_for_chunk(nonce_prefix: bytes, index: int) -> bytes:
    return nonce_prefix + index.to_bytes(NONCE_COUNTER_SIZE, "big")


# --- Single-blob format (v2 pipeline, docs/backup-plan/steps/10-encryption.md) ---
#
# The v2 pipeline encrypts each file into ONE blob before tarring/splitting:
#
#   header (20 bytes) | chunk 0 | chunk 1 | ...
#   header = b"MBE1" | nonce_prefix (4) | chunk_size u32 | plain_size u64
#   chunk i = AES-256-GCM(key, nonce_prefix||i, plaintext[i*chunk_size:(i+1)*chunk_size], aad=header)
#
# Every chunk but the last is exactly chunk_size + GCM_TAG_SIZE bytes, so a
# blob can be cut into parts at header_size + k * encrypted_chunk_size and
# each cut lands on a chunk boundary (see packer.split_part_sizes' align
# args). The header is the AAD of every chunk, so a tampered plain_size or
# chunk_size fails authentication instead of silently truncating the output.
BLOB_MAGIC = b"MBE1"
BLOB_HEADER_SIZE = 20
GCM_TAG_SIZE = 16
# Smaller than the legacy CHUNK_SIZE so a ~1 GiB part holds many chunks
# rather than wasting most of its size on alignment slack.
BLOB_CHUNK_SIZE = 64 * 1024 * 1024
_HEADER_STRUCT = struct.Struct(">4s4sIQ")


def encrypted_chunk_size(chunk_size: int = BLOB_CHUNK_SIZE) -> int:
    return chunk_size + GCM_TAG_SIZE


def blob_size_for(plain_size: int, chunk_size: int = BLOB_CHUNK_SIZE) -> int:
    chunks = -(-plain_size // chunk_size)
    return BLOB_HEADER_SIZE + plain_size + chunks * GCM_TAG_SIZE


def encrypt_blob(source_path: Path, dest_path: Path, file_key: bytes, chunk_size: int = BLOB_CHUNK_SIZE) -> int:
    """Encrypts source_path into the single-blob format at dest_path. Returns
    the blob's size in bytes. Every call uses a fresh random nonce prefix, so
    the same file+key encrypted twice never repeats a (key, nonce) pair."""
    plain_size = source_path.stat().st_size
    nonce_prefix = os.urandom(NONCE_PREFIX_SIZE)
    header = _HEADER_STRUCT.pack(BLOB_MAGIC, nonce_prefix, chunk_size, plain_size)
    aesgcm = AESGCM(file_key)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".tmp")
    written = 0
    with source_path.open("rb") as src, tmp_path.open("wb") as dst:
        dst.write(header)
        index = 0
        remaining = plain_size
        while remaining > 0:
            chunk = src.read(min(chunk_size, remaining))
            if not chunk:
                raise RuntimeError(f"{source_path} shrank while being encrypted")
            dst.write(aesgcm.encrypt(_nonce_for_chunk(nonce_prefix, index), chunk, header))
            remaining -= len(chunk)
            index += 1
        written = dst.tell()
        if src.read(1):
            raise RuntimeError(f"{source_path} grew while being encrypted")
    tmp_path.replace(dest_path)
    return written


def parse_blob_header(header: bytes) -> tuple[bytes, int, int]:
    """(nonce_prefix, chunk_size, plain_size) from a blob's first
    BLOB_HEADER_SIZE bytes. ValueError if it isn't a blob header. Used to
    sample a blob's first/last chunk without reading the whole thing."""
    if len(header) != BLOB_HEADER_SIZE:
        raise ValueError("encrypted blob is truncated (short header)")
    magic, nonce_prefix, chunk_size, plain_size = _HEADER_STRUCT.unpack(header)
    if magic != BLOB_MAGIC:
        raise ValueError("not an encrypted blob (bad magic)")
    if chunk_size <= 0:
        raise ValueError("encrypted blob has an invalid chunk size")
    return nonce_prefix, chunk_size, plain_size


def decrypt_blob_chunk(file_key: bytes, header: bytes, index: int, ciphertext: bytes) -> bytes:
    """Decrypts one chunk (0-based `index`) of a blob whose header is `header`.
    Raises InvalidTag on a corrupt chunk, wrong key, or a chunk in the wrong
    position (the index is part of the nonce)."""
    nonce_prefix, _, _ = parse_blob_header(header)
    return AESGCM(file_key).decrypt(_nonce_for_chunk(nonce_prefix, index), ciphertext, header)


def decrypt_blob(blob_path: Path, output_path: Path, file_key: bytes) -> int:
    """Decrypts a single-blob file to output_path. Raises InvalidTag on a
    corrupt/tampered chunk or wrong key, ValueError on a structurally bad
    blob (bad magic, truncated, trailing bytes). Returns plaintext size."""
    aesgcm = AESGCM(file_key)
    with blob_path.open("rb") as src:
        header = src.read(BLOB_HEADER_SIZE)
        if len(header) != BLOB_HEADER_SIZE:
            raise ValueError("encrypted blob is truncated (short header)")
        magic, nonce_prefix, chunk_size, plain_size = _HEADER_STRUCT.unpack(header)
        if magic != BLOB_MAGIC:
            raise ValueError("not an encrypted blob (bad magic)")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_name(output_path.name + ".mbcopy")
        try:
            with tmp_path.open("wb") as dst:
                index = 0
                remaining = plain_size
                while remaining > 0:
                    want = min(chunk_size, remaining) + GCM_TAG_SIZE
                    ciphertext = src.read(want)
                    if len(ciphertext) != want:
                        raise ValueError("encrypted blob is truncated (missing chunk data)")
                    dst.write(aesgcm.decrypt(_nonce_for_chunk(nonce_prefix, index), ciphertext, header))
                    remaining -= want - GCM_TAG_SIZE
                    index += 1
            if src.read(1):
                raise ValueError("encrypted blob has trailing bytes")
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    tmp_path.replace(output_path)
    return plain_size


def encrypt_paths(file_key: bytes, sha256: str, paths: list[str]) -> str:
    """Encrypts a file's real relative path(s) for the plaintext index. Bound
    to the file's hash via AAD so a token can't be moved to another entry."""
    nonce = os.urandom(12)
    sealed = AESGCM(file_key).encrypt(nonce, json.dumps(paths).encode(), sha256.encode())
    return base64.b64encode(nonce + sealed).decode()


def decrypt_paths(file_key: bytes, sha256: str, token: str) -> list[str]:
    raw = base64.b64decode(token)
    return json.loads(AESGCM(file_key).decrypt(raw[:12], raw[12:], sha256.encode()))


def derive_sha256_seal_key(master_key: bytes) -> bytes:
    """Key for sealing the real sha256 inside a content-id-keyed index entry
    (the "sha256_enc" field - see backup_index.py and
    docs/backup-plan/DEVIATIONS.md). Unlike derive_file_key, this can't be
    derived from the file's sha256 itself - recovering that sha256 is the
    whole point, so deriving its key from it would be circular. It comes
    straight from master_key with its own label instead, so bucket-inventory
    discovery (sync_run.py) can recover a file's real identity with the
    master key alone, with no database row to start from - while anyone
    without the key still can't compute or confirm anything from content_id."""
    return hmac.new(master_key, b"mediabridge-sha256-seal-key", hashlib.sha256).digest()


def encrypt_sha256(seal_key: bytes, content_id: str, sha256: str) -> str:
    """Seals a file's real sha256 for storage in a content-id-keyed index
    entry. Bound to the entry's content_id via AAD so a token can't be moved
    to another entry."""
    nonce = os.urandom(12)
    sealed = AESGCM(seal_key).encrypt(nonce, sha256.encode(), content_id.encode())
    return base64.b64encode(nonce + sealed).decode()


def decrypt_sha256(seal_key: bytes, content_id: str, token: str) -> str:
    raw = base64.b64decode(token)
    return AESGCM(seal_key).decrypt(raw[:12], raw[12:], content_id.encode()).decode()
