"""Fernet symmetric encryption for stored secrets (currently: GitHub PATs).

Key source: NOTED_GIT_FERNET_KEY env var. If missing, generates one on first
boot and persists to /opt/noted-sync/data/fernet.key — the systemd unit
should set Environment=NOTED_GIT_FERNET_KEY=... in production to keep the
key under explicit configuration management. The on-disk file is a fallback
so we don't crash on first run if the operator hasn't set the env var yet.

key_version (passed through from callers) lets us rotate keys without a
one-shot re-encrypt of every existing row. Each credential row stores which
key version encrypted it; MultiFernet tries each in order. Operator rotation
flow: bump NOTED_GIT_FERNET_KEYS to comma-separate new,old; new writes use
key_version=2; old rows still decrypt with key 1; lazy re-encrypt happens on
any UPDATE.

Threat model: single-user droplet. Key blast radius = same as the
NOTED_SYNC_TOKEN already in env. If the droplet is owned, attacker reads
both. If the key file is lost (droplet rebuild without preserving env), all
encrypted PATs become unreadable garbage — operator just re-pastes them.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet

KEY_FILE = Path(os.environ.get("NOTED_GIT_FERNET_KEYFILE", "/opt/noted-sync/data/fernet.key"))


def _load_keys() -> MultiFernet:
    """Load all known Fernet keys in priority order (newest first)."""
    keys: list[bytes] = []
    # Primary: comma-separated list (for rotation). Falls back to singular.
    multi = os.environ.get("NOTED_GIT_FERNET_KEYS")
    single = os.environ.get("NOTED_GIT_FERNET_KEY")
    if multi:
        keys = [k.strip().encode() for k in multi.split(",") if k.strip()]
    elif single:
        keys = [single.encode()]
    elif KEY_FILE.exists():
        keys = [KEY_FILE.read_bytes().strip()]
    else:
        # First boot, no key configured. Generate, persist, warn.
        new_key = Fernet.generate_key()
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_bytes(new_key)
        os.chmod(KEY_FILE, 0o600)
        keys = [new_key]
        print(f"WARN: no NOTED_GIT_FERNET_KEY env var; generated key at {KEY_FILE}. "
              "Move to env var for production.")
    return MultiFernet([Fernet(k) for k in keys])


_FERNET: MultiFernet | None = None


def get_fernet() -> MultiFernet:
    global _FERNET
    if _FERNET is None:
        _FERNET = _load_keys()
    return _FERNET


def encrypt(plaintext: str) -> bytes:
    return get_fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    return get_fernet().decrypt(ciphertext).decode()


def current_key_version() -> int:
    """Which key version (1 = newest) is encrypting new writes. Used to stamp
    git_credentials.key_version so rotation can re-encrypt old rows lazily."""
    return 1
