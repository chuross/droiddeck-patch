#!/usr/bin/env python3
"""Reuse published native packs only when their tracked build inputs match."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

REPOSITORY = "Droid-Deck/DroidDeck-Components"
COMPONENTS = {"xbox": ("xodus-cli", "xodus-service", "version"),
              "comet": ("comet", "version")}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_matches(source, revision, component):
    paths = ["tools/" + component]
    if component == "comet":
        paths.append("tools/local-cross.Dockerfile")
    run("git", "-C", str(source), "cat-file", "-e", revision + "^{commit}")
    if run("git", "-C", str(source), "ls-files", "--others", "--", *paths):
        return False
    result = subprocess.run(["git", "-C", str(source), "diff", "--quiet", revision,
                             "--", *paths])
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def cache_key(source, component, seed):
    directory = source / "tools" / component
    files = sorted((p for p in directory.rglob("*")
                    if p.is_file() and not p.is_symlink() and p.suffix != ".pyc"),
                   key=lambda p: os.fsencode(p.relative_to(source).as_posix()))
    if any(re.search(r"[\s\\\"']", p.relative_to(source).as_posix()) for p in files):
        raise RuntimeError("Upstream cache cannot represent these input filenames")
    text = seed + "\n" + "".join(
        digest(p) + "  " + p.relative_to(source).as_posix() + "\n" for p in files)
    return hashlib.sha256(text.encode()).hexdigest()


def unpack(archive, destination, component):
    total = 0
    names = set()
    with tempfile.TemporaryFile() as raw:
        subprocess.run(["zstd", "-dc", str(archive)], stdout=raw, check=True)
        if raw.tell() > 512 * 1024 * 1024:
            raise RuntimeError("Native pack is unexpectedly large")
        raw.seek(0)
        with tarfile.open(fileobj=raw) as packed:
            for member in packed:
                path = PurePosixPath(member.name)
                total += member.size
                if (not member.isfile() or path.is_absolute() or ".." in path.parts
                        or member.name in names or member.name == ".complete"
                        or total > 512 * 1024 * 1024):
                    raise RuntimeError("Invalid native pack member: " + member.name)
                names.add(member.name)
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with packed.extractfile(member) as stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for name in COMPONENTS[component]:
        file = destination / name
        if not file.is_file():
            raise RuntimeError("Native pack is missing " + name)
        if name != "version":
            with file.open("rb") as stream:
                header = stream.read(20)
            if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
                raise RuntimeError("Native pack does not contain ARM64 " + name)


def prepare(source, cache, image):
    script = (source / "tools/build_local.sh").read_text()
    hooks = {'xbox': 'xbox_key=$(inputs_hash "native-arm64-ubuntu24.04" tools/xbox)',
             'comet': 'comet_key=$(inputs_hash "${image_id}" tools/comet)'}
    for hook in hooks.values():
        if script.count(hook) != 1:
            raise RuntimeError("Upstream native cache inputs changed; review pack reuse")
    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image)
    releases = json.loads(run("gh", "api", "repos/" + REPOSITORY + "/releases?per_page=100"))
    records = []
    for component in COMPONENTS:
        for release in releases:
            if release["draft"] or release["prerelease"]:
                continue
            match = re.fullmatch("pack-" + component + r"-([0-9a-f]{16})", release["tag_name"])
            revision = re.search(r"from `([0-9a-f]{40})`", release.get("body") or "")
            if not match or not revision or not source_matches(source, revision[1], component):
                continue
            assets = [a for a in release["assets"] if a["name"] == component + ".tar.zst"]
            if len(assets) != 1:
                raise RuntimeError("Published native pack asset is missing or ambiguous")
            asset = assets[0]
            sha = (asset.get("digest") or "").removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", sha) or not sha.startswith(match[1]):
                raise RuntimeError("Published native pack lacks a matching SHA-256")
            if not 0 < asset["size"] <= 64 * 1024 * 1024:
                raise RuntimeError("Published native pack has an unexpected size")
            seed = "native-arm64-ubuntu24.04" if component == "xbox" else image_id
            target = cache / "steps" / component / cache_key(source, component, seed)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
                temporary = Path(temporary)
                archive = temporary / "pack.tar.zst"
                run("gh", "release", "download", release["tag_name"], "--repo", REPOSITORY,
                    "--pattern", asset["name"], "--output", str(archive))
                if archive.stat().st_size != asset["size"] or digest(archive) != sha:
                    raise RuntimeError("Published native pack checksum mismatch")
                output = temporary / "output"
                output.mkdir()
                unpack(archive, output, component)
                (output / ".complete").touch()
                # Replace only this disposable input-keyed cache, including stale local builds.
                if target.exists():
                    shutil.rmtree(target)
                output.rename(target)
            records.append({"component": component, "source": revision[1],
                            "tag": release["tag_name"], "sha256": sha})
            print("Verified upstream native pack:", component, release["tag_name"])
            break
        else:
            print("No matching published pack; build from source:", component)
    (source / "upstream-native-packs.json").write_text(json.dumps(records, indent=2) + "\n")


def verify(source):
    directory = source / "app/build/packs"
    for component in ("proot", "directaudio", "tracking", "xbox", "comet"):
        archive = directory / (component + ".tar.zst")
        sha = digest(archive)
        tag = "pack-" + component + "-" + sha[:16]
        release = json.loads(run("gh", "api", "repos/" + REPOSITORY + "/releases/tags/" + tag))
        if release["draft"] or release["prerelease"]:
            raise RuntimeError("Required add-on pack is not publicly released: " + tag)
        assets = [a for a in release["assets"] if a["name"] == archive.name]
        if (len(assets) != 1 or assets[0].get("digest") != "sha256:" + sha
                or assets[0]["size"] != archive.stat().st_size):
            raise RuntimeError("APK add-on pack is not available with its pinned checksum: " + tag)
        print("Verified published APK add-on pack:", tag)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/droiddeck-build")
    parser.add_argument("--image", default="droiddeck-local-cross:24.04-v3")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.source.resolve(), args.cache.resolve(), args.image)
    else:
        verify(args.source.resolve())
