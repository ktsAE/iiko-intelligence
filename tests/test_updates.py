import base64
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

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from iiko_intelligence import release_trust as trust, updates


class UpdateTests(unittest.TestCase):
    tag = "v0.5.0"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / ".iiko-updates"
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        modulus = base64.b64encode(self.key.public_key().public_numbers().n.to_bytes(384, "big")).decode()
        pin = patch.object(trust, "MODULUS", modulus)
        pin.start()
        self.addCleanup(pin.stop)
        self.blob = self.package()
        self.release = self.metadata(self.blob)
        self.probes = 0

    def package(self, extra=None):
        stream = io.BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr("iiko-intelligence/runtime/python.exe", b"test fixture")
            archive.writestr("iiko-intelligence/pyproject.toml", '[project]\nname="iiko-intelligence-trial"\nversion="0.5.0"\n[tool.iiko]\nupdate_layout=2\n')
            if extra:
                archive.writestr(extra, "fixture")
        return stream.getvalue()

    def metadata(self, blob):
        digest = hashlib.sha256(blob).hexdigest()
        try:
            tree = trust.archive_tree(io.BytesIO(blob))
        except ValueError:
            tree = "0" * 64  # Invalid layout fixture still exercises archive validation.
        message = trust.Release(self.tag, len(blob), digest, tree).message()
        self.receipt = message + base64.b64encode(self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())) + b"\n"
        assets = []
        for name, data in ((updates.ARCHIVE, blob), (trust.SIGNATURE, self.receipt)):
            assets.append({"name": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                           "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{self.tag}/{name}"})
        return {"tag_name": self.tag, "prerelease": False, "draft": False, "immutable": True, "assets": assets}

    def refresh(self, **kwargs):
        def retrieve(release, path):
            return updates.download(release, path, lambda url, **k: io.BytesIO(self.receipt if url.endswith(".sig") else self.blob))

        def verify(candidate, tag):
            self.assertEqual(candidate.parent.parent, self.folder)
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

    def test_cannot_replace_package_using_only_github_digests(self):
        self.blob = self.package("iiko-intelligence/another-file.txt")
        self.release["assets"][0].update(size=len(self.blob), digest="sha256:" + hashlib.sha256(self.blob).hexdigest())
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.release = self.metadata(self.blob)
        self.receipt = self.receipt.replace(b"v0.5.0", b"v0.6.0")
        self.release["assets"][1].update(digest="sha256:" + hashlib.sha256(self.receipt).hexdigest())
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.release["assets"].pop()
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.assertEqual(self.probes, 0)

    def test_mutable_release_and_cross_host_redirects_are_refused(self):
        self.release["immutable"] = False
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.assertEqual(self.probes, 0)
        redirect = updates.ReleaseRedirect()
        for url in ["https://example.org/download", "http://github.com/download"]:
            with self.assertRaisesRegex(ValueError, "redirect"):
                redirect.redirect_request(None, None, 302, "", {}, url)

    def test_malformed_metadata_keeps_reporting_available(self):
        self.release["assets"] = ["invalid"]
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.release["tag_name"] = {"invalid": "version"}
        state = self.refresh()
        self.assertEqual(state["status"], "check_failed")
        self.assertEqual(state["latest"], self.tag)
        self.assertEqual(self.probes, 0)

    def test_probe_uses_disposable_data_and_an_allowlisted_environment(self):
        with patch.dict(os.environ, {"IIKO_CLOUD_CLIENT_SECRET": "synthetic", "EXAMPLE_TOKEN": "fixture"}), \
                patch.object(updates.subprocess, "run") as run:
            run.return_value.returncode = 0
            updates.probe(self.root, self.tag)
            kwargs = run.call_args.kwargs
            self.assertNotIn("IIKO_CLOUD_CLIENT_SECRET", kwargs["env"])
            self.assertNotIn("EXAMPLE_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["env"]["USERPROFILE"], kwargs["cwd"])
            self.assertEqual(Path(run.call_args.args[0][-1]), Path(kwargs["cwd"]) / "demo.sqlite")

    def test_self_signed_update_and_post_install_tampering_are_rejected(self):
        trusted = self.key
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        self.release = self.metadata(self.blob)
        self.assertEqual(self.refresh()["status"], "check_failed")
        self.assertEqual(self.probes, 0)
        self.key = trusted
        self.release = self.metadata(self.blob)
        self.refresh()
        with patch.object(updates, "managed", return_value=True), patch.object(updates.config, "PROJECT_ROOT", self.root), \
                patch.object(updates.config, "CODE_ROOT", self.root), patch.object(updates, "_rejected_tag", None), \
                patch.object(updates.subprocess, "call", return_value=0) as launch:
            (self.folder / self.tag / "unexpected.txt").write_text("local change")
            updates.dispatch("mcp_server")
            self.assertEqual(updates.update_status()["status"], "installed_files_rejected")
            launch.assert_not_called()

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
