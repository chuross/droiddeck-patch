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
COMPONENTS = {"proot": ("libproot.so", "libproot-loader.so"),
              "xbox": ("xodus-cli", "xodus-service", "version"),
              "comet": ("comet", "version")}
XBOX_HOST_CONFIGURATIONS = {
    ("87ccb068de3064309094d23223ed4847972b98e81799c8c1e7b6c11759232835", None),
    ("bb15ae6949b7ee51b7ce7be18ea8d1785ea4382e6b8a76fc0a869c2fb0309b67",
     "39bcfd326e2f300c3aeafc8c950b51e1e43b3c8e19a0f35f19b0e328dae3eb13"),
}
COMET_HOST_FILES = ("tools/comet/build-local.sh", "tools/comet/build.sh", "tools/local-cross.Dockerfile")
COMET_HOST_CONFIGURATIONS = {
    ("1192b5fb553ec5c064eefc654ff0bb736a14ac3cb4d6fbe7baff6699c8c76c51",
     "eefb97ecf1571a4d7c224b278c52ab4094203bcd49d2e3dbf2c33cad42486eba",
     "b67868228b59667704e9c3335d41def2e1812e4eed167cc24ae953e9d6fa2cc4"),
    ("982d64752050cb693a6454909323937c2693634318745821a5219629b84513ff",
     "a11522b6fff2ace9ee687cc57fb8719e2919ed46447a6fd53e960919374f386a",
     "b615201a4ce181b004048849861775f9f675d64cbdfc5c21af91cc9305c81dfe"),
}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tracked_inputs_equal(source, revision, paths):
    result = subprocess.run(["git", "-C", str(source), "diff", "--quiet", revision, "--", *paths])
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def xbox_runtime_inputs_match(source, revision):
    # Upstream #1440 changes only the host compiler and when ARM64 tests execute.
    # Permit those exact wrappers, while comparing every production build command and input.
    wrappers = ("tools/xbox/build-local.sh", "tools/xbox/cross.Dockerfile")
    current = tuple(digest(source / name) if (source / name).is_file() else None for name in wrappers)
    previous = []
    for name in wrappers:
        result = subprocess.run(["git", "-C", str(source), "show", revision + ":" + name],
                                capture_output=True)
        previous.append(hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None)
    if current not in XBOX_HOST_CONFIGURATIONS or tuple(previous) not in XBOX_HOST_CONFIGURATIONS:
        return False

    def production_commands(text):
        native = 'if [[ $(uname -m) = aarch64 ]]; then\n'
        cross = 'if [[ $(uname -m) = aarch64 || -n ${CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUNNER:-} ]]; then\n'
        if text.count(native) + text.count(cross) != 1:
            return None
        return text.replace(cross, native).strip()

    build = "tools/xbox/build.sh"
    current_build = production_commands((source / build).read_text())
    previous_build = production_commands(run("git", "-C", str(source), "show", revision + ":" + build))
    if current_build is None or current_build != previous_build:
        return False
    excluded = [":(exclude)" + path for path in (*wrappers, build)]
    return tracked_inputs_equal(source, revision, ["tools/xbox", *excluded])


def git_digest(source, revision, name):
    result = subprocess.run(["git", "-C", str(source), "show", revision + ":" + name],
                            capture_output=True)
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None


def comet_host_inputs_match(source, revision):
    # Upstream #1719 and #1744 change only the ARM64 host wrapper, the image's package
    # selection on ARM64 hosts and the checksum-verified download's curl retries.
    current = tuple(digest(source / name) if (source / name).is_file() else None
                    for name in COMET_HOST_FILES)
    previous = tuple(git_digest(source, revision, name) for name in COMET_HOST_FILES)
    if current not in COMET_HOST_CONFIGURATIONS or previous not in COMET_HOST_CONFIGURATIONS:
        return False
    excluded = [":(exclude)" + path for path in COMET_HOST_FILES[:2]]
    return tracked_inputs_equal(source, revision, ["tools/comet", *excluded])


def source_matches(source, revision, component):
    paths = ["tools/" + component]
    if component == "proot":
        # The separately compiled fastpath preload is not an input to the PRoot executables.
        if "fastpath" in (source / "tools/proot/build.sh").read_text():
            raise RuntimeError("PRoot build inputs changed; review native pack reuse")
        paths.append(":(exclude)tools/proot/fastpath/**")
    if component == "comet":
        paths.append("tools/local-cross.Dockerfile")
    run("git", "-C", str(source), "cat-file", "-e", revision + "^{commit}")
    untracked = run("git", "-C", str(source), "ls-files", "--others", "--", *paths).splitlines()
    if any(not name.endswith(".pyc") for name in untracked):
        return False
    if tracked_inputs_equal(source, revision, paths):
        return True
    if component == "comet":
        return comet_host_inputs_match(source, revision)
    return component == "xbox" and xbox_runtime_inputs_match(source, revision)


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


def install_proot(source, output, ndk_version):
    script = (source / "tools/build_local.sh").read_text()
    hooks = (
        'proot_sources=$(cd "${repo_root}/tools/proot" && find . -type f ! -name \'*.pyc\' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -d\' \' -f1)',
        'proot_inputs=$(printf \'%s\\n\' "${proot_sources}" "${ndk_version}" | shasum -a 256 | cut -d\' \' -f1)',
    )
    if any(script.count(hook) != 1 for hook in hooks) or not re.fullmatch(r"\d+\.\d+\.\d+", ndk_version or ""):
        raise RuntimeError("PRoot cache inputs changed or NDK version is missing")
    directory = source / "tools/proot"
    files = sorted((p for p in directory.rglob("*")
                    if p.is_file() and not p.is_symlink() and p.suffix != ".pyc"),
                   key=lambda p: os.fsencode(p.relative_to(directory).as_posix()))
    hashes = "".join(digest(p) + "  ./" + p.relative_to(directory).as_posix() + "\n" for p in files)
    source_hash = hashlib.sha256(hashes.encode()).hexdigest()
    inputs = hashlib.sha256((source_hash + "\n" + ndk_version + "\n").encode()).hexdigest()
    destination = source / "app/src/main/jniLibs/arm64-v8a"
    destination.mkdir(parents=True, exist_ok=True)
    for name in COMPONENTS["proot"]:
        shutil.copy2(output / name, destination / name)
    outputs = "".join(digest(destination / name) + "  " + name + "\n" for name in COMPONENTS["proot"])
    (destination / ".proot-outputs").write_text(outputs)
    (destination / ".proot-inputs").write_text(inputs + "\n")


def prepare(source, cache, image, ndk_version=None):
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
                if component == "proot":
                    install_proot(source, output, ndk_version)
                else:
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
    parser.add_argument("--ndk-version", default=os.environ.get("DROIDDECK_NDK_VERSION"))
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.source.resolve(), args.cache.resolve(), args.image, args.ndk_version)
    else:
        verify(args.source.resolve())
