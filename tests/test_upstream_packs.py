import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upstream_packs", ROOT / "tools/upstream_packs.py")
packs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packs)


class UpstreamPacksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for name in ("xbox", "comet", "proot"):
            directory = self.root / "tools" / name
            directory.mkdir(parents=True)
            (directory / "build.sh").write_text("source fixture\n")
        (self.root / "tools/local-cross.Dockerfile").write_text("compiler fixture\n")
        packs.run("git", "-C", str(self.root), "add", ".")
        packs.run("git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                  "user.email=test@example.invalid", "commit", "-qm", "source")
        self.revision = packs.run("git", "-C", str(self.root), "rev-parse", "HEAD")

    def test_source_changes_untracked_inputs_and_compiler_changes_prevent_reuse(self):
        self.assertTrue(packs.source_matches(self.root, self.revision, "comet"))
        (self.root / "tools/comet/build.sh").write_text("changed\n")
        self.assertFalse(packs.source_matches(self.root, self.revision, "comet"))
        packs.run("git", "-C", str(self.root), "restore", "tools/comet/build.sh")
        (self.root / "tools/comet/untracked").touch()
        self.assertFalse(packs.source_matches(self.root, self.revision, "comet"))
        (self.root / "tools/comet/untracked").unlink()
        (self.root / "tools/local-cross.Dockerfile").write_text("new compiler\n")
        self.assertFalse(packs.source_matches(self.root, self.revision, "comet"))
        self.assertTrue(packs.source_matches(self.root, self.revision, "xbox"))

    def test_proot_ignores_only_the_independent_fastpath_preload(self):
        directory = self.root / "tools/proot"
        (directory / "fastpath").mkdir()
        (directory / "fastpath/fastpath.c").write_text("independent preload")
        self.assertTrue(packs.source_matches(self.root, self.revision, "proot"))
        (directory / "new.patch").touch()
        self.assertFalse(packs.source_matches(self.root, self.revision, "proot"))
        (directory / "build.sh").write_text("use fastpath in PRoot too")
        with self.assertRaisesRegex(RuntimeError, "PRoot build inputs changed"):
            packs.source_matches(self.root, self.revision, "proot")

    def test_installed_proot_passes_upstream_input_and_output_checks(self):
        (self.root / "tools/build_local.sh").write_text(
            'proot_sources=$(cd "${repo_root}/tools/proot" && find . -type f ! -name \'*.pyc\' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d\' \' -f1)\n'
            'proot_inputs=$(printf \'%s\\n\' "${proot_sources}" "${ndk_version}" | shasum -a 256 | cut -d\' \' -f1)\n')
        output = self.root / "output"
        output.mkdir()
        for name in packs.COMPONENTS["proot"]:
            (output / name).write_bytes(name.encode())
        packs.install_proot(self.root, output, "27.3.13750724")
        destination = self.root / "app/src/main/jniLibs/arm64-v8a"
        subprocess.run(["shasum", "-a", "256", "-c", ".proot-outputs"],
                       cwd=destination, check=True, capture_output=True)
        expected = subprocess.check_output([
            "bash", "-c", 'proot_sources=$(find . -type f ! -name "*.pyc" | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d" " -f1); '
            'printf "%s\\n" "$proot_sources" 27.3.13750724 | shasum -a 256 | cut -d" " -f1'],
            cwd=self.root / "tools/proot", text=True).strip()
        self.assertEqual((destination / ".proot-inputs").read_text().strip(), expected)

    def test_cache_key_matches_upstream_shell_with_sorted_paths_and_ignored_bytecode(self):
        directory = self.root / "tools/xbox"
        (directory / "a-file").write_bytes(b"second input")
        (directory / "ignored.pyc").write_bytes(b"bytecode")
        expected = subprocess.check_output([
            "bash", "-c", "{ printf '%s\\n' native-arm64-ubuntu24.04; "
            "find tools/xbox -type f ! -name '*.pyc' | LC_ALL=C sort | "
            "xargs shasum -a 256; } | shasum -a 256 | cut -d' ' -f1"],
            cwd=self.root, text=True).strip()
        self.assertEqual(packs.cache_key(self.root, "xbox", "native-arm64-ubuntu24.04"), expected)
        (directory / "unsupported filename").touch()
        with self.assertRaisesRegex(RuntimeError, "input filenames"):
            packs.cache_key(self.root, "xbox", "native-arm64-ubuntu24.04")

    def archive(self, bad=None, machine=183):
        raw = self.root / "pack.tar"
        with tarfile.open(raw, "w") as packed:
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = machine.to_bytes(2, "little")
            for name in packs.COMPONENTS["xbox"]:
                content = b"version" if name == "version" else bytes(header)
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o755
                packed.addfile(member, io.BytesIO(content))
            if bad:
                packed.addfile(bad)
        archive = self.root / "pack.tar.zst"
        subprocess.run(["zstd", "-q", "-f", str(raw), "-o", str(archive)], check=True)
        return archive

    def test_pack_preserves_executable_binaries_and_requires_arm64(self):
        output = self.root / "output"
        output.mkdir()
        packs.unpack(self.archive(), output, "xbox")
        self.assertTrue((output / "xodus-cli").stat().st_mode & 0o111)
        self.assertEqual((output / "version").read_bytes(), b"version")
        other = self.root / "other-output"
        other.mkdir()
        with self.assertRaisesRegex(RuntimeError, "does not contain ARM64"):
            packs.unpack(self.archive(machine=62), other, "xbox")

    def test_changed_download_never_publishes_a_complete_cache(self):
        (self.root / "tools/build_local.sh").write_text(
            'xbox_key=$(inputs_hash "native-arm64-ubuntu24.04" tools/xbox)\n'
            'comet_key=$(inputs_hash "${image_id}" tools/comet)\n')
        release = {"draft": False, "prerelease": False,
                   "tag_name": "pack-xbox-" + "0" * 16,
                   "body": "from `" + self.revision + "`", "assets": [{
                       "name": "xbox.tar.zst", "digest": "sha256:" + "0" * 64, "size": 7}]}
        original = packs.run

        def command(*args):
            if args[0] == "docker":
                return "sha256:image"
            if args[:2] == ("gh", "api"):
                return json.dumps([release])
            if args[:3] == ("gh", "release", "download"):
                Path(args[-1]).write_bytes(b"changed")
                return ""
            return original(*args)

        cache = self.root / "cache"
        with patch.object(packs, "run", side_effect=command):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                packs.prepare(self.root, cache, "image")
        self.assertEqual(list(cache.rglob(".complete")), [])

    def test_archive_links_traversal_and_cache_marker_are_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE),
                           (".complete", tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                bad = tarfile.TarInfo(name)
                bad.type = kind
                bad.linkname = "version"
                with self.assertRaisesRegex(RuntimeError, "Invalid native pack member"):
                    packs.unpack(self.archive(bad), Path(temporary), "xbox")

    def test_publication_requires_every_pinned_pack_digest_and_size(self):
        directory = self.root / "app/build/packs"
        directory.mkdir(parents=True)
        for name in ("proot", "directaudio", "tracking", "xbox", "comet"):
            (directory / (name + ".tar.zst")).write_bytes(name.encode())

        def release(*args):
            tag = args[-1].rsplit("/", 1)[1]
            name = tag.split("-")[1]
            archive = directory / (name + ".tar.zst")
            return json.dumps({"draft": False, "prerelease": False, "assets": [{
                "name": archive.name, "size": archive.stat().st_size,
                "digest": "sha256:" + packs.digest(archive)}]})

        with patch.object(packs, "run", side_effect=release) as call:
            packs.verify(self.root)
            self.assertEqual(call.call_count, 5)
        with patch.object(packs, "run", return_value=json.dumps({
                "draft": False, "prerelease": False, "assets": []})):
            with self.assertRaisesRegex(RuntimeError, "not available"):
                packs.verify(self.root)
