"""Build the offline Windows distribution from the Python release and locked wheels."""
from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist/iiko-intelligence-windows-x64.zip"
PYTHON_VERSION = "3.14.10"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=OUTPUT.parent) as temporary:
        work = Path(temporary)
        requirements = work / "requirements.txt"
        vendor = work / "site-packages"
        subprocess.run(["uv", "export", "--locked", "--no-dev", "--no-emit-project",
                        "--output-file", str(requirements)], cwd=ROOT, stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["uv", "pip", "install", "--target", str(vendor), "--python-platform", "windows",
                        "--python-version", PYTHON_VERSION, "--only-binary", ":all:", "--require-hashes",
                        "--no-deps", "-r", str(requirements)], cwd=ROOT, check=True)
        with urlopen(PYTHON_URL, timeout=60) as response:
            runtime = response.read(20 * 1024 * 1024)
        if hashlib.sha256(runtime).hexdigest() != PYTHON_SHA256:
            raise ValueError("Windows Python release checksum mismatch")

        package = "iiko-intelligence/"
        archive = work / "release.zip"
        with ZipFile(archive, "w", ZIP_DEFLATED) as bundle, ZipFile(io.BytesIO(runtime)) as python:
            for name in python.namelist():
                data = python.read(name)
                if name == "python314._pth":
                    data = b"python314.zip\r\n.\r\n../src\r\nimport site\r\n"
                bundle.writestr(package + "runtime/" + name, data)
            for file in sorted(vendor.rglob("*")):
                relative = file.relative_to(vendor)
                if (file.is_file() and relative.parts[0] not in {"bin", "Scripts", ".lock"}
                        and "__pycache__" not in relative.parts and file.suffix != ".pyc"):
                    bundle.write(file, package + "runtime/Lib/site-packages/" + relative.as_posix())
            for name in ("README.md", "WINDOWS.md", "INSTALL.md", "IIKO_DATA.md", "Install-iiko.ps1",
                         "pyproject.toml", "uv.lock", "examples/sales-example.csv",
                         "examples/stock-template.csv", "examples/purchases-template.csv",
                         "examples/writeoffs-template.csv"):
                bundle.write(ROOT / name, package + name)
            guide = (ROOT / "WINDOWS.md").read_text(encoding="utf-8")
            bundle.write(ROOT / "windows/AGENTS.md", package + "AGENTS.md")
            plain = guide.replace("## ", "").replace("# ", "").replace("**", "").replace("`", "")
            bundle.writestr(package + "START-HERE.txt", plain.replace("\n", "\r\n").encode("utf-8-sig"))
            for file in sorted((ROOT / "src/iiko_intelligence").glob("*.py")):
                bundle.write(file, package + file.relative_to(ROOT).as_posix())
            for file in sorted((ROOT / "windows").glob("*.cmd")):
                bundle.writestr(package + file.name, file.read_text(encoding="ascii").replace("\n", "\r\n"))
        archive.replace(OUTPUT)
    print(f"{OUTPUT}\nsha256 {hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
