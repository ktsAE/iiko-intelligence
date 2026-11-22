"""Publisher trust anchor and the signed Windows release format (v1)."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

REPOSITORY = "ktsAE/iiko-intelligence"
ARCHIVE = "iiko-intelligence-windows-x64.zip"
SIGNATURE = ARCHIVE + ".sig"
DOMAIN = "iiko-manager-windows-release-v1"
MODULUS = "2vMeeuov28EUNJvCPV61TzpEKP+8nfs/7xr1H+6vijf0itjas8y1cYY8TEEIKmpseCwfx2nruYScEc64zZG8XjvMHZUrPo9MX3XzN0gmOUkY3xvHOW1cbQeY3zOt94yXih1v0HTWH3SNv6xmnzqSE355ZBv3HTFM0zwIBrMmAFDS8J2p+TDUcWO9icCmDR/OFWBAX4al54cVwFyiwN9aXn/IrBQfEP6GbhTyc0HPigby3FS/DMjwEsOskhj4xyXV8T2KcpycXMv6hamwb2QcJdQC5JZaRDvVoPX8d4HipbWm4M5Mzugm1BWUMVe2MirdVjdlRD97kD/k09GLVYPPBe2PrBWHSVWpqkl80dY+xb2IH/8COrrHZT0+kTJl+SrzwIMa3miq2LZJbKAmyJ+hqttTUInwpoYa0CkiLWJUD9MVUGOjwZKDc29fsbNz+2bLiGCD0bf3W8anJ+gV0SI4Qy5cA6GlEf5Bn7MQuyL3XO/yWEpyMbkXCeQmPXGkQwJr"
TAG = re.compile(r"v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
HEX = re.compile(r"[0-9a-f]{64}")
MAX_RECEIPT = 1024
MAX_BYTES = 160 * 1024 * 1024
MAX_EXPANDED = 640 * 1024 * 1024
MAX_ENTRIES = 30000


@dataclass(frozen=True)
class Release:
    tag: str
    size: int
    sha256: str
    tree: str

    def message(self) -> bytes:
        if (not TAG.fullmatch(self.tag) or not (0 < self.size <= MAX_BYTES)
                or not HEX.fullmatch(self.sha256) or not HEX.fullmatch(self.tree)):
            raise ValueError("Invalid signed release fields.")
        return f"{DOMAIN}\n{REPOSITORY}\n{self.tag}\n{ARCHIVE}\n{self.size}\n{self.sha256}\n{self.tree}\n".encode("ascii")


def verify_receipt(raw: bytes, tag: str) -> Release:
    if len(raw) > MAX_RECEIPT or not raw.endswith(b"\n"):
        raise ValueError("Invalid release signature file.")
    try:
        domain, repo, signed_tag, name, size, digest, tree, signature = raw[:-1].decode("ascii").split("\n")
        if (domain, repo, signed_tag, name) != (DOMAIN, REPOSITORY, tag, ARCHIVE) or not re.fullmatch(r"[1-9][0-9]{0,8}", size):
            raise ValueError("Release identity mismatch.")
        record = Release(tag, int(size), digest, tree)
        key = rsa.RSAPublicNumbers(65537, int.from_bytes(base64.b64decode(MODULUS), "big")).public_key()
        key.verify(base64.b64decode(signature, validate=True), record.message(), padding.PKCS1v15(), hashes.SHA256())
        return record
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("Publisher signature verification failed.") from exc


def file_hash(stream) -> str:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_digest(entries: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(entries):
        h.update(name.encode("utf-8") + b"\0" + entries[name].encode("ascii") + b"\n")
    return h.hexdigest()


def archive_tree(archive: Path) -> str:
    entries = {}
    with ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            prefix = "iiko-intelligence/"
            if not member.filename.startswith(prefix) or member.filename[len(prefix):] in entries:
                raise ValueError("Unexpected release file.")
            with bundle.open(member) as stream:
                entries[member.filename[len(prefix):]] = file_hash(stream)
    return tree_digest(entries)


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def verify_tree(root: Path, record: Release) -> None:
    # Check all files, including extras which Python or the Windows DLL loader could import.
    if is_link(root.lstat()) or is_link(root.parent.lstat()):
        raise ValueError("Linked update folders are unsupported.")
    entries: dict[str, str] = {}
    directories = [root]
    total = count = 0
    while directories:
        folder = directories.pop()
        for child in folder.iterdir():
            count += 1
            info = child.lstat()
            if is_link(info) or count > MAX_ENTRIES:
                raise ValueError("Invalid installed release tree.")
            if stat.S_ISDIR(info.st_mode):
                directories.append(child)
                continue
            total += info.st_size
            if not stat.S_ISREG(info.st_mode) or total > MAX_EXPANDED:
                raise ValueError("Invalid installed release file.")
            with child.open("rb") as stream:
                entries[child.relative_to(root).as_posix()] = file_hash(stream)
    if tree_digest(entries) != record.tree:
        raise ValueError("Installed files differ from the signed release.")
