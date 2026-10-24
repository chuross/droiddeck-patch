#!/usr/bin/env python3
"""Resolve upstream main and replace a version's nightly prerelease."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from release_plan import workflow_version_code

UPSTREAM = "Droid-Deck/DroidDeck"


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def plan():
    base = json.loads(gh("api", f"repos/{UPSTREAM}/commits/main"))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise RuntimeError("Invalid upstream commit")
    # A shared timestamp-based range keeps nightly upgrades monotonic across runs
    # and workflow recreation, without depending on the stable workflow counter.
    run = json.loads(gh("api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"))
    code = workflow_version_code(run["created_at"])
    Path("nightly-plan.json").write_text(json.dumps({"base": base, "version_code": code}))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"base={base}\nversion_code={code}\n")


def publish():
    repository = os.environ["GITHUB_REPOSITORY"]
    manager = os.environ["GITHUB_SHA"]
    planned = json.loads(Path("nightly-plan.json").read_text())
    manifest = json.loads(Path("dist/jp-build.json").read_text())
    if (manifest["upstream"] != planned["base"] or
            manifest["version_code"] != planned["version_code"] or
            manifest["manager"] != manager):
        raise RuntimeError("Build provenance differs from the nightly plan")
    version = manifest["version_name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", version):
        raise RuntimeError("Invalid upstream versionName")
    tag = version + "-nightly"
    apk = Path("dist/DroidDeck-Patch.apk")
    if not apk.is_file() or not apk.stat().st_size:
        raise RuntimeError("Release APK is missing")
    with apk.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    checksum = Path("dist/SHA256SUMS")
    checksum.write_text(digest + "  DroidDeck-Patch.apk\n")
    notes = Path("dist/release-notes.md")
    notes.write_text(f"- 本家リリース: {version} (main)\n"
                     f"- 本家コミット: {manifest['upstream']}\n"
                     f"APK SHA-256: `{digest}`\n")
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repository,
         "--json", "isPrerelease"], capture_output=True, text=True)
    if existing.returncode:
        if "not found" not in existing.stderr.lower() and "404" not in existing.stderr:
            raise RuntimeError(existing.stderr)
        gh("release", "create", tag, "--repo", repository, "--target", manager,
           "--title", tag, "--notes-file", str(notes), "--draft", "--prerelease")
    elif not json.loads(existing.stdout)["isPrerelease"]:
        raise RuntimeError("Refusing to replace a non-prerelease")
    gh("release", "upload", tag, str(apk), str(checksum),
       "--repo", repository, "--clobber")
    gh("release", "edit", tag, "--repo", repository, "--title", tag,
       "--notes-file", str(notes), "--prerelease", "--draft=false", "--latest=false")


if __name__ == "__main__":
    {"plan": plan, "publish": publish}[sys.argv[1]]()
