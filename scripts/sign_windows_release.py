"""Sign the reviewed Windows ZIP with the publisher key kept off GitHub."""
import argparse
import base64
import os
import tomllib
from pathlib import Path
from zipfile import ZipFile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from iiko_intelligence.release_trust import ARCHIVE, MODULUS, SIGNATURE, Release, archive_tree, file_hash, verify_receipt

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, type=Path)
    args = parser.parse_args()
    key_path = args.key.expanduser().resolve(strict=True)
    if key_path.is_relative_to(ROOT) or (os.name == "posix" and key_path.stat().st_mode & 0o077):
        parser.error("Use an owner-only signing key outside the checkout.")
    archive = ROOT / "dist" / ARCHIVE
    tag = "v" + tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    with ZipFile(archive) as bundle:
        project = tomllib.loads(bundle.read("iiko-intelligence/pyproject.toml").decode())
        anchor = bundle.read("iiko-intelligence/src/iiko_intelligence/release_trust.py")
        if ("v" + project["project"]["version"] != tag or
                anchor != (ROOT / "src/iiko_intelligence/release_trust.py").read_bytes()):
            parser.error("The package version or trust anchor differs from the reviewed source.")
    with archive.open("rb") as stream:
        record = Release(tag, archive.stat().st_size, file_hash(stream), archive_tree(archive))
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.public_key().public_numbers() != rsa.RSAPublicNumbers(
            65537, int.from_bytes(base64.b64decode(MODULUS), "big")):
        parser.error("Signing key does not match the installed public key.")
    message = record.message()
    raw = message + base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())) + b"\n"
    verify_receipt(raw, tag)
    target = archive.with_name(SIGNATURE)
    target.write_bytes(raw)
    print(f"Signed {tag}: {target}")


if __name__ == "__main__":
    main()
