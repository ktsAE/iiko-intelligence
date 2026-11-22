import hashlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from iiko_intelligence import updates


class UpdateTests(unittest.TestCase):
    tag = "v0.5.0"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / ".iiko-updates"
        self.blob = self.package()
        self.release = self.metadata(self.blob)
        self.probes = 0

    def package(self, extra=None):
        stream = io.BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr("iiko-intelligence/runtime/python.exe", b"test fixture")
            archive.writestr("iiko-intelligence/pyproject.toml", '[project]\nname="iiko-intelligence-trial"\nversion="0.5.0"\n[tool.iiko]\nupdate_layout=1\n')
            if extra:
                archive.writestr(extra, "fixture")
        return stream.getvalue()

    def metadata(self, blob):
        return {"tag_name": self.tag, "prerelease": False, "draft": False, "assets": [{
            "name": updates.ARCHIVE, "digest": "sha256:" + hashlib.sha256(blob).hexdigest(), "size": len(blob),
            "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{self.tag}/{updates.ARCHIVE}"}]}

    def refresh(self, **kwargs):
        def retrieve(release, path):
            updates.download(release, path, lambda *a, **k: io.BytesIO(self.blob))

        def verify(candidate, tag):
            self.assertEqual(candidate.parent, self.folder)
            self.assertEqual(tag, self.tag)
            self.assertTrue((candidate / "runtime/python.exe").is_file())
            self.probes += 1
        opts = dict(fetch=lambda: self.release, retrieve=retrieve, verify=verify, force=True)
        opts.update(kwargs)
        return updates.refresh(self.root, "v0.4.1", **opts)

    def test_verified_activation_reopen_and_preservation(self):
        private = ["data/manager.sqlite", "data/cloud-profile.dpapi", ".codex/config.toml", "reports/summary.txt"]
        for name in private:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"original")
        result = self.refresh()
        self.assertEqual((result["status"], result["selected"]), ("ready", self.tag))
        self.assertEqual(self.probes, 1)
        with patch.object(updates, "managed", return_value=True), patch.object(updates.config, "PROJECT_ROOT", self.root):
            self.assertEqual(updates.update_status()["status"], "restart_required")
            with patch.object(updates, "__version__", self.tag[1:]):
                self.assertEqual(updates.update_status()["status"], "updated")
        self.assertTrue(all((self.root / name).read_bytes() == b"original" for name in private))
        self.refresh(force=False, fetch=lambda: self.fail("rechecked within cache interval"))
        self.release["tag_name"] = "v0.3.1"
        self.assertEqual(self.refresh()["selected"], self.tag)
        self.assertEqual(self.probes, 1)

    def test_failed_update_leaves_last_good_code_selected(self):
        self.folder.mkdir()
        old = self.folder / "v0.4.2/runtime/python.exe"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        updates.save_state(self.folder, {"selected": "v0.4.2"})
        self.release["assets"][0]["digest"] = "sha256:" + "0"*64
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.release = self.metadata(self.blob)
        def fail(candidate, tag):
            raise ValueError("health check failed")
        self.assertEqual(self.refresh(verify=fail)["status"], "check_failed")
        self.assertFalse((self.folder / self.tag).exists())
        self.assertEqual(updates.selected(self.folder, "v0.4.1", updates.read_state(self.folder)), "v0.4.2")
        self.assertEqual(old.read_bytes(), b"old")

    def test_offline_archive_boundaries_and_stable_release(self):
        def offline():
            raise OSError("offline")
        self.assertEqual(self.refresh(fetch=offline)["status"], "check_failed")
        for path in ("iiko-intelligence/../outside", "iiko-intelligence/con.txt", "unexpected/file"):
            self.blob = self.package(path)
            self.release = self.metadata(self.blob)
            self.assertEqual(self.refresh()["status"], "check_failed")
        self.release["assets"][0]["browser_download_url"] = "https://example.org/other"
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.release["prerelease"] = True
        with self.assertRaisesRegex(ValueError, "stable"):
            updates.latest_release(lambda *a, **k: io.BytesIO(json.dumps(self.release).encode()))
        self.assertEqual(self.probes, 0)

    def test_server_schedules_checks_and_report_carries_ready_notice(self):
        with patch.object(updates, "managed", return_value=True), patch.dict(os.environ, {"IIKO_NO_UPDATE_CHECK": "0"}), \
                patch.object(updates.threading, "Thread") as thread:
            updates.start_checks()
            thread.return_value.start.assert_called_once()
            with patch.object(updates, "refresh") as refresh, patch.object(updates.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    thread.call_args.kwargs["target"]()
                refresh.assert_called_once()
            with patch.dict(os.environ, {"IIKO_NO_UPDATE_CHECK": "1"}):
                updates.start_checks()
                thread.return_value.start.assert_called_once()
        self.refresh()
        with patch.object(updates, "managed", return_value=True), patch.object(updates.config, "PROJECT_ROOT", self.root):
            reply = updates.with_update_notice({"report": "still available"})
            self.assertEqual((reply["report"], reply["software_update"]["ready"]), ("still available", self.tag))

    def test_concurrent_checks_select_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.refresh(force=False), range(2)))
        self.assertTrue(all(r["selected"] == self.tag for r in results))
        self.assertEqual(self.probes, 1)
