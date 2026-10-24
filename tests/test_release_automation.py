import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


planner = module("release_plan")
publisher = module("publish_release")


def release(tag="0.3.1", identity=1, date="2026-10-05T00:00:00Z", apk=True, **flags):
    return dict(id=identity, tag_name=tag, published_at=date,
                assets=[{"name": "app.apk" if apk else "gamescope.tzst"}], **flags)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.release = release()
        self.lock = {"repository": "upstream", "base": "a" * 40,
                     "upstream_release": planner.release_identity(self.release)}
        self.resolve = Mock(return_value="b" * 40)

    def plan(self, event, candidate=None, number=12):
        return planner.make_plan(self.lock, candidate or self.release, event, number, self.resolve)

    def test_native_component_and_prerelease_are_not_app_updates(self):
        selected = planner.latest_apk_release([
            self.release, release("gamescope-123", 2, "2026-10-06T00:00:00Z", apk=False),
            release("0.4.0-preview", 3, "2026-10-07T00:00:00Z", prerelease=True),
            release("draft", 4, "2026-10-08T00:00:00Z", draft=True)])
        self.assertEqual(selected["id"], self.release["id"])

    def test_schedule_does_nothing_when_upstream_is_unchanged(self):
        plan = self.plan("schedule")
        self.assertFalse(plan["build"])
        self.assertFalse(plan["upstream_changed"])
        self.resolve.assert_not_called()

    def test_own_code_update_builds_without_downgrading_pinned_base(self):
        plan = self.plan("push")
        self.assertTrue(plan["build"])
        self.assertEqual(plan["lock"]["base"], "a" * 40)
        self.assertEqual(plan["tag"], "0.3.1")
        self.assertEqual(plan["version_code"], 1000012)

    def test_new_release_gets_exact_commit_and_retries_until_recorded(self):
        new = release("0.3.2", 2, "2026-10-06T00:00:00Z")
        plan = self.plan("schedule", new)
        self.assertTrue(plan["build"])
        self.assertTrue(plan["upstream_changed"])
        self.assertEqual(plan["lock"]["base"], "b" * 40)
        self.assertEqual(self.lock["base"], "a" * 40)
        self.assertTrue(self.plan("schedule", new)["build"])

    def test_older_release_does_not_roll_back(self):
        old = release("0.3.0", 0, "2026-10-01T00:00:00Z")
        self.assertFalse(self.plan("schedule", old)["build"])

    def test_pull_requests_never_publish_even_with_new_upstream(self):
        self.assertFalse(self.plan("pull_request",
                                   release("0.3.2", 2, "2026-10-06T00:00:00Z"))["build"])

    def test_build_codes_advance_across_rebuilds(self):
        self.assertEqual(self.plan("push", number=13)["tag"], self.plan("push", number=12)["tag"])
        self.assertGreater(self.plan("push", number=13)["version_code"],
                           self.plan("push", number=12)["version_code"])

    def test_public_schedule_rebuilds_changed_private_source(self):
        plan = self.plan("schedule")
        planner.public_rebuild(plan, "c" * 40, "<!-- jp-source: " + hashlib.sha256(("d" * 40).encode()).hexdigest() + " -->", 1)
        self.assertTrue(plan["build"])

    def test_public_schedule_skips_already_published_source(self):
        plan = self.plan("schedule")
        planner.public_rebuild(plan, "c" * 40, "<!-- jp-source: " + hashlib.sha256(("c" * 40).encode()).hexdigest() + " -->", 1)
        self.assertFalse(plan["build"])

    def test_public_version_code_never_downgrades_existing_apk(self):
        plan = self.plan("push")
        planner.public_rebuild(plan, "c" * 40, "- APK versionCode: 3000000", 1)
        self.assertEqual(plan["version_code"], 3000001)

    def test_return_to_private_release_keeps_publicly_shipped_upgrade_order(self):
        plan = self.plan("workflow_dispatch", number=1)
        planner.public_rebuild(plan, "c" * 40, "- APK versionCode: 1000012", 1)
        self.assertEqual(plan["version_code"], 2000007)

    def test_annotated_tags_use_peeled_commit(self):
        output = type("Result", (), {"stdout": "a" * 40 + "\trefs/tags/0.3.2\n" +
                                    "b" * 40 + "\trefs/tags/0.3.2^{}\n"})()
        with patch.object(planner.subprocess, "run", return_value=output):
            self.assertEqual(planner.resolve_tag("upstream", "0.3.2"), "b" * 40)

    def test_stable_can_upgrade_previously_published_nightly(self):
        code = planner.workflow_version_code("2026-10-24T05:00:00Z")
        self.assertGreater(code, 1792806631)
        self.assertGreater(code, 2000062)
        self.assertEqual(code, planner.workflow_version_code("2026-10-24T14:00:00+09:00"))
        self.assertGreater(planner.workflow_version_code("2026-10-24T05:00:01Z"), code)

    def test_invalid_timestamp_cannot_produce_android_version_code(self):
        for timestamp in ("1970-01-01T00:00:00Z", "2040-01-01T00:00:00Z"):
            with self.subTest(timestamp=timestamp), self.assertRaises(RuntimeError):
                planner.workflow_version_code(timestamp)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.plan = {"tag": "0.3.1", "version_code": 1000012,
                     "lock": {"base": "a" * 40, "upstream_release": {"tag_name": "0.3.1"}},
                     "upstream_changed": True, "original_lock": {"base": "old"}}
        self.manager = "c" * 40
        self.target = "d" * 40
        remote = patch.object(publisher, "api", return_value={"sha": self.target, "private": True})
        remote.start()
        self.addCleanup(remote.stop)
        for name in ("DroidDeck-Patch.apk", "DroidDeck-Patch-source.tar.gz", "SHA256SUMS"):
            (self.directory / name).write_bytes(b"fixture")
        (self.directory / "jp-build.json").write_text(json.dumps(
            {"upstream": "a" * 40, "manager": self.manager,
             "version_name": "0.3.1", "version_code": 1000012}))

    def result(self, code=0, out="", error=""):
        return subprocess.CompletedProcess([], code, out, error)

    def test_command_failure_exposes_stderr(self):
        with patch.object(publisher.subprocess, "run", return_value=self.result(1, error="HTTP 403: denied")), \
             patch.object(publisher, "print") as output:
            with self.assertRaises(subprocess.CalledProcessError):
                publisher.run("gh", "release", "create")
            self.assertEqual(output.call_args.args[0], "HTTP 403: denied")

    def test_assets_finish_uploading_before_release_is_published(self):
        with patch.object(publisher, "run", side_effect=[
                self.result(1, error="release not found"), self.result(),
                self.result(), self.result()]) as invoke:
            publisher.publish("owner/repo", self.manager, self.plan, self.directory)
        commands = [call.args for call in invoke.call_args_list]
        self.assertIn("--draft", commands[1])
        self.assertEqual(commands[2][2], "upload")
        self.assertEqual(commands[3][2], "edit")
        self.assertIn("--draft=false", commands[3])

    def test_failed_upload_never_publishes_release(self):
        with patch.object(publisher, "run", side_effect=[
                self.result(1, error="release not found"), self.result(),
                subprocess.CalledProcessError(1, ["gh"])]) as invoke:
            with self.assertRaises(subprocess.CalledProcessError):
                publisher.publish("owner/repo", self.manager, self.plan, self.directory)
            self.assertEqual(invoke.call_count, 3)

    def test_existing_release_is_updated_with_matching_assets_and_tag(self):
        with patch.object(publisher, "run", return_value=self.result(out='{"isDraft": false}')) as invoke:
            publisher.publish("owner/repo", self.manager, self.plan, self.directory)
        commands = [call.args for call in invoke.call_args_list]
        self.assertIn("--draft", commands[1])
        self.assertEqual(commands[2][2], "upload")
        self.assertIn("--clobber", commands[2])
        self.assertEqual(invoke.call_count, 4)
        self.assertNotIn(str(self.directory / "jp-build.json"), commands[2])
        self.assertNotIn(str(self.directory / "DroidDeck-Patch-source.tar.gz"), commands[2])
        self.assertIn(str(self.directory / "SHA256SUMS"), commands[2])
        self.assertNotIn("source.tar.gz", (self.directory / "SHA256SUMS").read_text())
        self.assertIn("--draft=false", commands[3])
        self.assertIn("--notes-file", commands[3])
        notes = (self.directory / "release-notes.md").read_text()
        self.assertIn("APK SHA-256", notes)
        for label in ("APK versionName:", "APK versionCode:", "ビルド用ソース:", "License:"):
            self.assertNotIn(label, notes)

    def test_apk_update_preserves_only_hidden_proton_identity_and_assets(self):
        marker = "<!-- proton-source: " + "a" * 64 + " -->"
        block = "<!-- proton:start -->\nProton notes\n" + marker + "\n<!-- proton:end -->"
        info = {"isDraft": False, "body": "Old APK\n" + block,
                "assets": [{"name": "GE-Proton11-7-droiddeck-aarch64.tar.gz"},
                           {"name": "Proton-SHA256SUMS"}, {"name": "proton-build.json"}]}
        with patch.object(publisher, "run", return_value=self.result(out=json.dumps(info))) as invoke:
            publisher.publish("owner/repo", self.manager, self.plan, self.directory)
        notes = (self.directory / "release-notes.md").read_text()
        self.assertIn(marker, notes)
        self.assertNotIn("Proton notes", notes)
        self.assertNotIn("proton:start", notes)
        self.assertFalse(any("delete-asset" in call.args for call in invoke.call_args_list))

    def test_failed_update_leaves_release_draft(self):
        with patch.object(publisher, "run", side_effect=[
                self.result(out='{"isDraft": false}'), self.result(),
                subprocess.CalledProcessError(1, ["gh"])]) as invoke:
            with self.assertRaises(subprocess.CalledProcessError):
                publisher.publish("owner/repo", self.manager, self.plan, self.directory)
        self.assertEqual(invoke.call_count, 3)

    def test_new_release_uses_public_repository_commit_not_private_sha(self):
        with patch.object(publisher, "run", side_effect=[
                self.result(1, error="HTTP 404"), self.result(), self.result(), self.result()]) as invoke:
            publisher.publish("owner/public-downloads", self.manager, self.plan, self.directory)
        commands = [call.args for call in invoke.call_args_list]
        self.assertIn(self.target, commands[1])
        self.assertNotIn(self.manager, commands[1])
        for command in commands:
            self.assertIn("owner/public-downloads", command)
        self.assertEqual(commands[2][6:], (str(self.directory / "DroidDeck-Patch.apk"), str(self.directory / "SHA256SUMS"), "--clobber"))

    def test_public_release_does_not_require_source_archive(self):
        (self.directory / "DroidDeck-Patch-source.tar.gz").unlink()
        (self.directory / "SHA256SUMS").unlink()
        with patch.object(publisher, "run", return_value=self.result(out='{"isDraft": true}')) as invoke:
            publisher.publish("owner/public-downloads", self.manager, self.plan, self.directory)
        commands = [call.args for call in invoke.call_args_list]
        self.assertIn(str(self.directory / "DroidDeck-Patch.apk"), commands[1])

    def test_main_publishes_only_to_distribution_repository(self):
        plan_path = self.directory / "plan.json"
        plan_path.write_text(json.dumps(self.plan))
        with patch.object(publisher.sys, "argv", ["publish_release.py", "--repository", "owner/private",
                "--manager", self.manager, "--plan", str(plan_path), "--publish-only",
                "--release-repository", "owner/public"]), patch.object(publisher, "publish") as publish, \
                patch.object(publisher, "record_success") as record:
            publisher.main()
        self.assertEqual(publish.call_args.args[0], "owner/public")
        record.assert_not_called()

    def test_main_record_step_cannot_publish(self):
        plan_path = self.directory / "plan.json"
        plan_path.write_text(json.dumps(self.plan))
        with patch.object(publisher.sys, "argv", ["publish_release.py", "--repository", "owner/private",
                "--manager", self.manager, "--plan", str(plan_path), "--record-only"]), \
                patch.object(publisher, "publish") as publish, patch.object(publisher, "record_success") as record:
            publisher.main()
        publish.assert_not_called()
        self.assertEqual(record.call_args.args[0], "owner/private")

    def test_main_can_publish_to_its_own_private_source_repository(self):
        plan_path = self.directory / "plan.json"
        plan_path.write_text(json.dumps(self.plan))
        with patch.object(publisher.sys, "argv", ["publish_release.py", "--repository", "owner/private",
                "--manager", self.manager, "--plan", str(plan_path), "--publish-only",
                "--release-repository", "owner/private"]), patch.object(publisher, "publish") as publish:
            publisher.main()
        self.assertEqual(publish.call_args.args[0], "owner/private")

    def test_public_repository_accepts_release_publication(self):
        with patch.object(publisher, "api", return_value={"sha": self.target, "private": False}), \
                patch.object(publisher, "run", return_value=self.result(out='{"isDraft": true}')) as invoke:
            publisher.publish("owner/public", self.manager, self.plan, self.directory)
        self.assertIn("--draft=false", invoke.call_args.args)

    def test_obsolete_source_and_manifest_are_removed_after_apk_upload(self):
        with patch.object(publisher, "api", side_effect=[
                {"sha": self.target}]), \
                patch.object(publisher, "run", return_value=self.result(out=json.dumps({"isDraft": False,
                    "assets": [{"name": "DroidDeck-Patch-source.tar.gz"}, {"name": "jp-build.json"},
                               {"name": "DroidDeck-Patch.apk"}, {"name": "SHA256SUMS"}]}))) as invoke:
            publisher.publish("owner/private", self.manager, self.plan, self.directory)
        commands = [call.args for call in invoke.call_args_list]
        self.assertEqual(commands[2][2], "upload")
        self.assertEqual([command[4] for command in commands[3:5]],
                         ["DroidDeck-Patch-source.tar.gz", "jp-build.json"])
        self.assertTrue(all(command[2] == "delete-asset" for command in commands[3:5]))
        self.assertIn("--draft=false", commands[-1])

    def test_draft_recovery_uses_no_by_tag_api_lookup(self):
        with patch.object(publisher, "api", side_effect=[{"sha": self.target}]) as api, \
                patch.object(publisher, "run", return_value=self.result(out=json.dumps({"isDraft": True,
                    "assets": [{"name": "jp-build.json"}]}))) as invoke:
            publisher.publish("owner/private", self.manager, self.plan, self.directory)
        self.assertEqual(api.call_count, 1)
        commands = [call.args for call in invoke.call_args_list]
        self.assertEqual(commands[1][2], "upload")
        self.assertEqual(commands[2][2], "delete-asset")
        self.assertIn("--draft=false", commands[-1])

    def test_main_cannot_publish_without_an_explicit_distribution_repository(self):
        with patch.object(publisher.sys, "argv", ["publish_release.py", "--repository", "owner/private",
                "--manager", self.manager, "--publish-only"]), \
                patch.object(publisher, "publish") as publish, patch.object(publisher.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                publisher.main()
        self.assertEqual(error.exception.code, 2)
        publish.assert_not_called()

    def test_wrong_build_provenance_is_rejected_before_publish(self):
        with patch.object(publisher, "run") as invoke:
            with self.assertRaises(RuntimeError):
                publisher.publish("owner/repo", "wrong commit", self.plan, self.directory)
            invoke.assert_not_called()

    def test_concurrent_main_update_does_not_overwrite_lock(self):
        with patch.object(publisher, "api", return_value={"object": {"sha": "new commit"}}), \
             patch.object(publisher, "run") as invoke:
            self.assertFalse(publisher.record_success("owner/repo", self.manager, self.plan))
            invoke.assert_not_called()

    def test_success_records_exact_candidate_with_blob_lease(self):
        responses = [{"object": {"sha": self.manager}},
                     {"sha": "blob", "content": base64.b64encode(json.dumps(
                         self.plan["original_lock"]).encode()).decode()}]
        recorded = []

        def invoke(*args, **kwargs):
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            recorded.append(payload)
            return self.result()

        with patch.object(publisher, "api", side_effect=responses), \
             patch.object(publisher, "run", side_effect=invoke):
            self.assertTrue(publisher.record_success("owner/repo", self.manager, self.plan))
        self.assertEqual(recorded[0]["sha"], "blob")
        self.assertEqual(json.loads(base64.b64decode(recorded[0]["content"])), self.plan["lock"])


if __name__ == "__main__":
    unittest.main()

