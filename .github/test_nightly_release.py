import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("nightly", Path(__file__).with_name("nightly_release.py"))
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)


class NightlyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, previous)
        self.env = patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo", GITHUB_SHA="b" * 40)
        self.env.start()
        self.addCleanup(self.env.stop)
        Path("dist").mkdir()
        Path("dist/DroidDeck-Patch.apk").write_bytes(b"apk")
        Path("nightly-plan.json").write_text(json.dumps({"base": "a" * 40, "version_code": 2000008}))
        self.manifest = {"upstream": "a" * 40, "manager": "b" * 40,
                         "version_name": "0.4.0", "version_code": 2000008}
        self.write_manifest()

    def write_manifest(self):
        Path("dist/jp-build.json").write_text(json.dumps(self.manifest))

    def publish(self, code=0, stdout='{"isPrerelease": true}', stderr=""):
        with patch.object(nightly.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, stderr)), patch.object(nightly, "gh") as gh:
            nightly.publish()
            return gh.call_args_list

    def test_existing_nightly_overwrites_assets_and_stays_prerelease(self):
        for _ in range(2):
            calls = [call.args for call in self.publish()]
            self.assertEqual(calls[0][:3], ("release", "upload", "0.4.0-nightly"))
            self.assertIn("--clobber", calls[0])
            self.assertIn("--prerelease", calls[1])
            self.assertIn("--latest=false", calls[1])
            self.assertFalse(any("create" in call for call in calls))

    def test_missing_nightly_is_created_as_draft_before_upload(self):
        calls = [call.args for call in self.publish(1, "", "release not found")]
        self.assertEqual(calls[0][:3], ("release", "create", "0.4.0-nightly"))
        self.assertIn("--draft", calls[0])
        self.assertIn("--prerelease", calls[0])
        self.assertEqual(calls[1][1], "upload")

    def test_api_failure_does_not_create_release(self):
        with self.assertRaises(RuntimeError):
            self.publish(1, "", "HTTP 403")

    def test_stable_release_is_protected(self):
        with self.assertRaises(RuntimeError):
            self.publish(stdout='{"isPrerelease": false}')

    def test_provenance_mismatch_prevents_upload(self):
        self.manifest["upstream"] = "c" * 40
        self.write_manifest()
        with patch.object(nightly, "gh") as gh, self.assertRaises(RuntimeError):
            nightly.publish()
        gh.assert_not_called()

    def test_plan_resolves_main_once(self):
        with patch.dict(os.environ, GITHUB_RUN_ID="123", GITHUB_OUTPUT="output"), patch.object(
                nightly, "gh", side_effect=[json.dumps({"sha": "a" * 40}),
                                           json.dumps({"created_at": "2026-10-15T00:00:00Z"})]) as gh:
            nightly.plan()
        self.assertEqual(gh.call_args_list[0].args, ("api", "repos/Droid-Deck/DroidDeck/commits/main"))
        self.assertEqual(json.loads(Path("nightly-plan.json").read_text())["base"], "a" * 40)
        self.assertEqual(json.loads(Path("nightly-plan.json").read_text())["version_code"],
                         nightly.workflow_version_code("2026-10-15T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
