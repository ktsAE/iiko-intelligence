"""Installation-local iiko Cloud profile, encrypted with the current Windows user's DPAPI key."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from .config import PROJECT_ROOT
from .cloud_sales import organization_token

PROFILE = PROJECT_ROOT / "data/cloud-profile.dpapi"
ENV = {"appId": "IIKO_CLOUD_APP_ID", "clientSecret": "IIKO_CLOUD_CLIENT_SECRET", "apiKey": "IIKO_CLOUD_API_KEY"}
ORG_ENV = "IIKO_CLOUD_ORGANIZATION_ID"


@dataclass(frozen=True)
class Profile:
    organization: str
    credentials: dict[str, str] = field(repr=False)


def validated(organization, credentials) -> Profile:
    try:
        org = str(UUID(organization))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Enter the organization UUID supplied by iiko.") from None
    if not isinstance(credentials, dict) or any(not isinstance(credentials.get(k), str) or
                                               not credentials[k].strip() for k in ENV):
        raise ValueError("The iiko Cloud credentials are incomplete.")
    return Profile(org, {k: credentials[k] for k in ENV})


def configured() -> bool:
    return PROFILE.is_file() or bool(os.environ.get(ORG_ENV))


def read_profile() -> Profile | None:
    if os.environ.get(ORG_ENV):
        return validated(os.environ[ORG_ENV], {k: os.environ.get(v) for k, v in ENV.items()})
    if not PROFILE.is_file():
        return None
    try:
        import win32crypt
        _, plain = win32crypt.CryptUnprotectData(PROFILE.read_bytes(), None, None, None, 1)
        data = json.loads(plain)
        return validated(data["organization"], data["credentials"])
    except Exception:
        raise ValueError("This Windows user could not open the saved profile. Run Connect-iiko-Cloud.cmd again.") from None


def save_profile(profile: Profile) -> None:
    try:
        import win32crypt
        plain = json.dumps({"organization": profile.organization, "credentials": profile.credentials}).encode()
        encrypted = win32crypt.CryptProtectData(plain, "iiko Cloud", None, None, None, 1)
    except Exception:
        raise ValueError("Could not encrypt the profile for this Windows user.") from None
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=PROFILE.parent, delete=False) as out:
        temporary = Path(out.name)
    try:
        temporary.write_bytes(encrypted)
        temporary.replace(PROFILE)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disconnect", action="store_true")
    args = parser.parse_args()
    try:
        if args.disconnect:
            PROFILE.unlink(missing_ok=True)
            print("Saved Windows profile removed.")
            return
        if sys.platform != "win32" or not sys.stdin.isatty():
            raise ValueError("Open Connect-iiko-Cloud.cmd in a Windows terminal to enter private credentials.")
        profile = validated(input("iiko organization UUID: ").strip(),
                            {key: getpass.getpass(f"{key} (hidden): ").strip() for key in ENV})
        organization_token(profile.organization, profile.credentials)
        save_profile(profile)
        print("Organization verified. Live reports will use this profile. Each report requires its own iiko permissions.")
    except (OSError, ValueError, KeyboardInterrupt, EOFError) as exc:
        parser.exit(1, f"Cloud setup incomplete. {exc}\n")


if __name__ == "__main__":
    main()
