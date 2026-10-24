#!/usr/bin/env python3
"""Prepare a disposable upstream checkout with independent overlays."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATCH_NAMES = ("proton-import",)


def load_patch(name):
    path = ROOT / "overlay" / name / "apply.py"
    spec = importlib.util.spec_from_file_location("droiddeck_overlay_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PATCHES = {name: load_patch(name) for name in PATCH_NAMES}


def git(source, *args, check=True):
    return subprocess.run(["git", "-C", str(source), *args], check=check,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def stage_overlay_assets(source):
    script = source / "app/src/main/java/com/droiddeck/launcher/session/SessionFiles.kt"
    text = script.read_text()
    hook = '        val files = arrayOf(\n'
    if text.count(hook) != 1:
        raise RuntimeError("SessionFiles staging changed; review JP overlay update integration")
    assets = [asset for module in PATCHES.values() for asset in getattr(module, "ASSETS", ())]
    assets.sort(key=lambda asset: not asset.startswith("usr/local/bin/"))
    entries = ''.join('            "%s" to "%s",\n' % (asset, asset) for asset in assets)
    script.write_text(text.replace(hook, hook + entries))

def overlay(source):
    # Validate source patches before downloading or installing assets.
    for module in PATCHES.values():
        if hasattr(module, "check"):
            module.check(source)
    for name, module in PATCHES.items():
        print("Applying overlay:", name)
        module.apply(source)
        target = source / "tools/linuxfs/overlay/usr/local/bin"
        for helper in getattr(module, "HELPERS", ()):
            target.mkdir(parents=True, exist_ok=True)
            installed = target / helper
            shutil.copy2(ROOT / "overlay" / name / helper, installed)
            installed.chmod(0o755)
    stage_overlay_assets(source)
    patch_app_identity(source)


def patch_app_identity(source):
    # Namespace and component class names remain upstream's.
    gradle = source / "app/build.gradle"
    text = gradle.read_text()
    old = 'applicationId "com.droiddeck.launcher"'
    if text.count(old) != 1:
        raise RuntimeError("applicationId changed; review the JP package")
    gradle.write_text(text.replace(old, 'applicationId "com.droiddeck.launcher.japanese"'))
    # All translations use a stable launcher label.
    for strings in (source / "app/src/main/res").glob("values*/strings.xml"):
        text = strings.read_text()
        text = re.sub(r'(<string name="app_name">).*?(</string>)',
                      r'\1DroidDeck Patch\2', text)
        strings.write_text(text)

def prepare(destination, base=None, lock_path=None, version_code=None):
    lock = json.loads((lock_path or ROOT / "upstream.lock.json").read_text())
    destination = destination.resolve()
    if destination.exists():
        raise RuntimeError("Destination already exists; choose a fresh directory: %s" % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--no-checkout", lock["repository"], str(destination)], check=True)
    git(destination, "config", "user.name", "DroidDeck Patch builder")
    git(destination, "config", "user.email", "droiddeck-patch@users.noreply.github.com")
    revision = base or lock["base"]
    git(destination, "fetch", "--no-tags", "origin", revision)
    git(destination, "checkout", "--detach", "FETCH_HEAD")
    upstream_sha = git(destination, "rev-parse", "HEAD").stdout.strip()
    overlay(destination)
    gradle = destination / "app/build.gradle"
    if version_code is not None:
        if not 1 <= version_code <= 2100000000:
            raise RuntimeError("Invalid Android version code")
        text, count = re.subn(r"(?m)^(\s*versionCode\s+)\d+",
                             lambda match: match[1] + str(version_code), gradle.read_text())
        if count != 1:
            raise RuntimeError("versionCode changed; review the JP build")
        gradle.write_text(text)
    version = re.search(r'versionName\s+"([^"]+)"', gradle.read_text())
    if version is None:
        raise RuntimeError("Cannot read upstream versionName")
    manifest = {"upstream": upstream_sha, "commits": lock["commits"],
                "upstream_release": lock.get("upstream_release"),
                "version_name": version[1], "version_code": version_code,
                "manager": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                         capture_output=True, text=True).stdout.strip()}
    (destination / "jp-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Prepared:", destination)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path, default=ROOT / "build/DroidDeck")
    parser.add_argument("--base", help="Override the pinned base for upstream compatibility checks")
    parser.add_argument("--lock", type=Path, help="Candidate lock from release discovery")
    parser.add_argument("--version-code", type=int, help="Monotonic JP Android version code")
    args = parser.parse_args()
    try:
        prepare(args.destination, args.base, args.lock, args.version_code)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr or "", file=sys.stderr)
        sys.exit(1)






