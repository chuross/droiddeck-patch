import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "overlay/proton-import"
HELPER = runpy.run_path(str(OVERLAY / "droiddeck-proton-import"))
spec = importlib.util.spec_from_file_location("proton_import_overlay", OVERLAY / "apply.py")
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class ProtonImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.steam = self.root / "Steam"
        self.tools = self.steam / "compatibilitytools.d"
        self.tools.mkdir(parents=True)
        self.globals = HELPER["install"].__globals__
        self.compat = {
            "manifest_entry_point": lambda path: "proton",
            "first_key": lambda path, section: "original" if Path(path).is_file() else None,
            "EXTRA_COMPAT_TOOL_VDF": '"compatibilitytools" { "compat_tools" { "%s" { "display_name" "%s" } } }',
            "adopt_extras": lambda path: {"ddjp-import-Test-Proton": "DDJP-Test-Proton"},
        }

    def elf(self, machine=183):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = machine.to_bytes(2, "little")
        return bytes(header)

    def archive(self, extras=(), *, machine=183, mode="w:gz", missing=None):
        archive = self.root / ("test.tar.xz" if mode == "w:xz" else "test.tar.gz")
        files = {
            "proton": b"#!/usr/bin/python3\n", "toolmanifest.vdf": b'"manifest" { "commandline" "/proton %verb%" }',
            "compatibilitytool.vdf": b'"compatibilitytools" { "compat_tools" { "test" { "install_path" "." } } }',
            "files/bin/wine64": self.elf(machine), "files/bin/wineserver": self.elf(machine),
        }
        files.pop(missing, None)
        with tarfile.open(archive, mode) as packed:
            for name, data in files.items():
                member = tarfile.TarInfo("Test-Proton/" + name)
                member.size = len(data)
                member.mode = 0o755 if name == "proton" or name.startswith("files/bin/") else 0o644
                packed.addfile(member, io.BytesIO(data))
            for name, kind, content in extras:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = content
                    packed.addfile(member)
                elif kind == tarfile.REGTYPE:
                    member.size = len(content)
                    packed.addfile(member, io.BytesIO(content))
                else:
                    packed.addfile(member)
        return archive

    def install(self, archive):
        return HELPER["install"](self.steam, archive, self.compat)

    def assert_rejected(self, archive, code):
        with self.assertRaises(HELPER["ImportProblem"]) as error:
            self.install(archive)
        self.assertEqual(error.exception.code, code)
        self.assertEqual(list(self.tools.iterdir()), [])

    def test_gzip_and_xz_install_separately_and_preserve_executable_modes(self):
        for mode in ("w:gz", "w:xz"):
            with self.subTest(mode=mode):
                archive = self.archive(mode=mode, extras=[("Test-Proton/files/bin/wine", tarfile.SYMTYPE, "wine64")])
                directory = self.install(archive)
                self.assertEqual(directory, "DDJP-Test-Proton")
                tree = self.tools / directory
                self.assertTrue((tree / "files/bin/wine").is_symlink())
                self.assertTrue((tree / "proton").stat().st_mode & 0o111)
                self.assertIn("ddjp-import-Test-Proton", (tree / "compatibilitytool.vdf").read_text())
                self.assertEqual(json.loads((tree / HELPER["MARKER"]).read_text())["directory"], directory)
                HELPER["remove"](self.steam, directory)
                self.assertTrue(archive.exists())

    def test_reimport_updates_managed_build_and_preserves_tool_identity(self):
        archive = self.archive()
        directory = self.install(archive)
        sentinel = self.tools / directory / "keep"
        sentinel.write_text("old build")
        self.assertEqual(self.install(archive), directory)
        self.assertFalse(sentinel.exists())
        self.assertEqual(HELPER["import_metadata"](self.tools / directory)["directory"], directory)
        self.assertEqual([p.name for p in self.tools.iterdir()], [directory])

    def test_legacy_marker_import_is_recognized_removed_and_reimported(self):
        archive = self.archive()
        directory = self.install(archive)
        tree = self.tools / directory
        (tree / HELPER["MARKER"]).rename(tree / HELPER["LEGACY_MARKER"])
        self.assertEqual(HELPER["import_metadata"](tree)["directory"], directory)
        self.assertEqual(self.install(archive), directory)
        self.assertTrue((tree / HELPER["MARKER"]).is_file())
        (tree / HELPER["MARKER"]).rename(tree / HELPER["LEGACY_MARKER"])
        HELPER["remove"](self.steam, directory)
        self.assertEqual(list(self.tools.iterdir()), [])

    def test_invalid_replacement_and_registration_failure_preserve_installed_build(self):
        directory = self.install(self.archive())
        tree = self.tools / directory
        sentinel = tree / "keep"
        sentinel.write_text("old build")
        for broken in ("invalid", "registration"):
            with self.subTest(broken=broken):
                archive = self.archive(machine=62 if broken == "invalid" else 183)
                with patch.dict(self.compat, adopt_extras=lambda path: {}):
                    with self.assertRaises(HELPER["ImportProblem"]):
                        self.install(archive)
                self.assertEqual(sentinel.read_text(), "old build")
                self.assertEqual(HELPER["import_metadata"](tree)["directory"], directory)
                self.assertEqual([p.name for p in self.tools.iterdir()], [directory])

    def test_failed_replacement_rename_rolls_back_original_build(self):
        archive = self.archive()
        directory = self.install(archive)
        sentinel = self.tools / directory / "keep"
        sentinel.write_text("old build")
        original = Path.rename

        def rename(path, target):
            if path.parent.name.startswith(".ddjp-import-") and Path(target) == self.tools / directory:
                raise OSError("simulated publish failure")
            return original(path, target)

        with patch.object(Path, "rename", rename):
            with self.assertRaises(OSError):
                self.install(archive)
        self.assertEqual(sentinel.read_text(), "old build")
        self.assertEqual([p.name for p in self.tools.iterdir()], [directory])

    def test_reimport_does_not_replace_unmanaged_collision(self):
        tree = self.tools / "DDJP-Test-Proton"
        tree.mkdir()
        (tree / "keep").write_text("unmanaged")
        with self.assertRaises(HELPER["ImportProblem"]) as error:
            self.install(self.archive())
        self.assertEqual(error.exception.code, "already_installed")
        self.assertEqual((tree / "keep").read_text(), "unmanaged")

    def test_legacy_marker_removal_rejects_symlink_and_directory_mismatch(self):
        directory = self.install(self.archive())
        tree = self.tools / directory
        marker = tree / HELPER["MARKER"]
        legacy = tree / HELPER["LEGACY_MARKER"]
        metadata = marker.read_text()
        marker.unlink()
        outside = self.root / "outside-marker"
        outside.write_text(metadata)
        legacy.symlink_to(outside)
        with self.assertRaises(HELPER["ImportProblem"]):
            HELPER["remove"](self.steam, directory)
        legacy.unlink()
        legacy.write_text(json.dumps({"directory": "DDJP-other"}))
        with self.assertRaises(HELPER["ImportProblem"]):
            HELPER["remove"](self.steam, directory)
        self.assertTrue(tree.is_dir())

    def test_x86_64_and_incomplete_protons_are_rejected(self):
        self.assert_rejected(self.archive(machine=62), "not_arm64")
        self.assert_rejected(self.archive(missing="proton"), "invalid_proton")
        self.assert_rejected(self.archive(missing="files/bin/wineserver"), "not_arm64")

    def test_traversal_absolute_paths_special_files_and_multiple_roots_are_rejected(self):
        cases = [
            ("../outside", tarfile.REGTYPE, b"escape", "unsafe_archive"),
            ("/tmp/outside", tarfile.REGTYPE, b"escape", "unsafe_archive"),
            ("Test-Proton/fifo", tarfile.FIFOTYPE, "", "unsafe_archive"),
            ("Other-Proton/file", tarfile.REGTYPE, b"other", "invalid_proton"),
            ("Test-Proton/proton", tarfile.REGTYPE, b"duplicate", "unsafe_archive"),
        ]
        for name, kind, content, code in cases:
            with self.subTest(name=name):
                self.assert_rejected(self.archive(extras=[(name, kind, content)]), code)

    def test_symlink_escape_and_link_ancestors_are_rejected(self):
        cases = [
            [("Test-Proton/link", tarfile.SYMTYPE, "../../outside")],
            [("Test-Proton/link", tarfile.SYMTYPE, "/tmp/outside")],
            [("Test-Proton/link", tarfile.SYMTYPE, "files"), ("Test-Proton/link/file", tarfile.REGTYPE, b"x")],
            [("Test-Proton/a", tarfile.SYMTYPE, "."), ("Test-Proton/b", tarfile.SYMTYPE, "a/..")],
            [("Test-Proton/a", tarfile.SYMTYPE, "b"), ("Test-Proton/b", tarfile.SYMTYPE, "a")],
        ]
        for extras in cases:
            with self.subTest(extras=extras):
                self.assert_rejected(self.archive(extras=extras), "unsafe_archive")
        self.assertFalse((self.root / "outside").exists())

    def test_hardlinks_to_regular_members_work_but_links_outside_are_rejected(self):
        directory = self.install(self.archive(extras=[("Test-Proton/files/bin/wine", tarfile.LNKTYPE, "Test-Proton/files/bin/wine64")]))
        tree = self.tools / directory
        self.assertEqual((tree / "files/bin/wine").stat().st_ino, (tree / "files/bin/wine64").stat().st_ino)
        HELPER["remove"](self.steam, directory)
        self.assert_rejected(self.archive(extras=[("Test-Proton/link", tarfile.LNKTYPE, "../outside")]), "unsafe_archive")

    def test_prewrapped_manifest_and_import_markers_are_not_trusted(self):
        for name in (HELPER["MARKER"], HELPER["LEGACY_MARKER"], "toolmanifest.vdf.droiddeck-orig", "toolmanifest.vdf.bannerlator-orig"):
            with self.subTest(name=name):
                self.assert_rejected(self.archive(extras=[("Test-Proton/" + name, tarfile.REGTYPE, b"untrusted")]), "invalid_proton")

    def test_registration_failure_leaves_no_partial_install(self):
        self.compat["adopt_extras"] = lambda path: {}
        self.assert_rejected(self.archive(), "registration_failed")

    def test_duplicate_internal_tool_name_is_rejected(self):
        archive = self.archive()
        first_key = self.compat["first_key"]
        self.compat["first_key"] = lambda path, section: (
            "ddjp-import-Test-Proton" if Path(path).parent.name == "existing" else first_key(path, section))
        existing = self.tools / "existing"
        existing.mkdir()
        (existing / "keep").write_text("existing build")
        with self.assertRaises(HELPER["ImportProblem"]) as error:
            self.install(archive)
        self.assertEqual(error.exception.code, "already_installed")
        self.assertEqual([p.name for p in self.tools.iterdir()], ["existing"])
        self.assertEqual((existing / "keep").read_text(), "existing build")

    def test_repeated_directories_still_count_toward_the_entry_limit(self):
        archive = self.archive(extras=[("Test-Proton/empty", tarfile.DIRTYPE, "")] * 10)
        with patch.dict(self.globals, MAX_ENTRIES=7):
            self.assert_rejected(archive, "archive_too_large")

    def test_low_space_and_expansion_limit_leave_no_partial_install(self):
        archive = self.archive()
        disk = type("Disk", (), {"free": HELPER["RESERVE"] + 1})()
        with patch.object(self.globals["shutil"], "disk_usage", return_value=disk):
            self.assert_rejected(archive, "no_space")
        with patch.dict(self.globals, MAX_BYTES=1):
            self.assert_rejected(archive, "archive_too_large")

    def test_remove_rejects_unmanaged_trees_and_does_not_follow_external_links(self):
        external = self.root / "external"
        external.mkdir()
        (external / "keep").write_text("keep")
        for directory in ("GE-Proton11-7", "../external", "DDJP-../external"):
            with self.assertRaises(HELPER["ImportProblem"]):
                HELPER["remove"](self.steam, directory)
        (self.tools / "DDJP-linked").symlink_to(external)
        with self.assertRaises(HELPER["ImportProblem"]):
            HELPER["remove"](self.steam, "DDJP-linked")
        directory = self.install(self.archive())
        (self.tools / directory / "outside").symlink_to(external)
        HELPER["remove"](self.steam, directory)
        self.assertEqual((external / "keep").read_text(), "keep")

    def test_truncated_archive_returns_structured_failure_without_publishing(self):
        archive = self.archive()
        archive.write_bytes(archive.read_bytes()[:30])
        result = subprocess.run([os.sys.executable, str(OVERLAY / "droiddeck-proton-import"), "install", str(self.steam), str(archive)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout.split("DDJP_IMPORT ")[1])["code"], "invalid_archive")
        self.assertEqual(list(self.tools.iterdir()), [])


@unittest.skipUnless(os.environ.get("DROIDDECK_PATCH_SOURCE"), "set DROIDDECK_PATCH_SOURCE to a prepared checkout")
class ProtonImportIntegrationTest(ProtonImportTest):
    def test_installed_changes_refresh_components_after_import_and_removal(self):
        source = Path(os.environ["DROIDDECK_PATCH_SOURCE"])
        launcher = source / "app/src/main/java/com/droiddeck/launcher"
        menu = (launcher / "ProtonMenu.kt").read_text()
        activity_path = launcher / "FrontEndActivity.kt"
        if not activity_path.exists():
            activity_path = launcher / "MainActivity.kt"
        activity = activity_path.read_text()
        # Upstream refreshes components whenever a Proton operation releases the busy id.
        self.assertIn("LaunchedEffect(protons.protonBusyId) { if (protons.protonBusyId == null) components.refreshComponents() }",
                      activity)
        for operation in ("importProton", "removeImportedProton", "installProton", "removeProton"):
            body = menu.split("fun " + operation + "(", 1)[1].split("\n    fun ", 1)[0].split("\n    private fun ", 1)[0]
            self.assertRegex(body, r"protonBusyId = (id|ProtonImport\.BUSY_ID)\n")
            self.assertIn("protonBusyId = null", body)
            self.assertIn("refreshProtons()", body)

    def setUp(self):
        super().setUp()
        source = Path(os.environ["DROIDDECK_PATCH_SOURCE"])
        self.compat = runpy.run_path(str(source / "tools/linuxfs/overlay/usr/local/bin/steam-compatibility"))

    def test_import_uses_real_android_wrapper_and_keeps_original_entry_point(self):
        directory = self.install(self.archive())
        tree = self.tools / directory
        self.assertIn('droiddeck-proton-wrap', (tree / "toolmanifest.vdf").read_text())
        self.assertNotIn('require_tool_appid', (tree / "toolmanifest.vdf").read_text())
        self.assertIn('"$here/proton"', (tree / "droiddeck-proton-wrap").read_text())
        self.assertTrue((tree / "toolmanifest.vdf.droiddeck-orig").is_file())
        subprocess.run(["bash", "-n", str(tree / "droiddeck-proton-wrap")], check=True)
        self.assertIn("ddjp-import-Test-Proton", self.compat["adopt_extras"](str(self.tools)))

    def test_current_plop_layout_installs_and_registers_with_real_wrapper(self):
        packaging = runpy.run_path(str(ROOT / "overlay/proton-ntcontext/package.py"))
        lock = json.loads((ROOT / "overlay/proton-ntcontext/build.lock.json").read_text())
        name = lock["build_name"]
        tree = self.root / name
        binaries = tree / "files/bin-arm64"
        binaries.mkdir(parents=True)
        for binary in ("wine", "wineserver"):
            (binaries / binary).write_bytes(self.elf())
            (binaries / binary).chmod(0o755)
        (tree / "proton").write_text("#!/usr/bin/python3\n")
        (tree / "proton").chmod(0o755)
        (tree / "toolmanifest.vdf").write_text('"manifest" { "commandline" "/proton %verb%" }')
        (tree / "compatibilitytool.vdf").write_text(
            '"compatibilitytools" { "compat_tools" { "%s" { "install_path" "." } } }' % name)
        # Exercise the publication fix, including its bin -> bin-arm64 symlinks,
        # then the entire importer and upstream registration, rather than validate alone.
        packaging["import_layout"](tree)
        archive = self.root / (name + "-aarch64.tar.gz")
        with tarfile.open(archive, "w:gz") as packed:
            packed.add(tree, arcname=name)
        directory = self.install(archive)
        self.assertEqual(directory, "DDJP-" + name)
        installed = self.tools / directory
        self.assertTrue((installed / "files/bin/wine").is_symlink())
        self.assertEqual((installed / "files/bin/wineserver").read_bytes(), self.elf())
        self.assertIn("ddjp-import-" + name, self.compat["adopt_extras"](str(self.tools)))
        self.assertIn('"$here/proton"', (installed / "droiddeck-proton-wrap").read_text())
        HELPER["remove"](self.steam, directory)
        self.assertEqual(list(self.tools.iterdir()), [])


class ProtonImportOverlayTest(unittest.TestCase):
    def test_changed_upstream_is_rejected_before_creating_kotlin_or_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            with self.assertRaisesRegex(RuntimeError, "Proton import integration changed"):
                overlay.check(source)
            self.assertFalse((source / overlay.KOTLIN).exists())


if __name__ == "__main__":
    unittest.main()
