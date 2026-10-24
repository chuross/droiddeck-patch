#!/usr/bin/env python3
"""Discover stable upstream APK releases and plan a reproducible JP build."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

UPSTREAM = "Droid-Deck/DroidDeck"
ROOT = Path(__file__).resolve().parents[1]


def workflow_version_code(created_at):
    # Both APK channels share one package, so their counters must share a range.
    code = int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp())
    if not 1 <= code <= 2100000000:
        raise RuntimeError("Invalid Android version code")
    return code


def gh_json(endpoint, *arguments):
    result = subprocess.run(["gh", "api", endpoint, *arguments], check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout)


def latest_apk_release(releases):
    stable = [release for release in releases
              if not release.get("draft") and not release.get("prerelease")
              and release.get("published_at")
              and any(asset["name"].endswith(".apk") for asset in release.get("assets", []))]
    if not stable:
        raise RuntimeError("No stable upstream release containing an APK")
    latest = max(stable, key=lambda release: release["published_at"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", latest["tag_name"]):
        raise RuntimeError("Unexpected upstream tag format")
    return latest


def release_identity(release):
    return {key: release[key] for key in ("id", "tag_name", "published_at")}


def resolve_tag(repository, tag):
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", repository,
         "refs/tags/" + tag, "refs/tags/" + tag + "^{}"],
        check=True, capture_output=True, text=True)
    references = dict((ref, sha) for sha, ref in
                      (line.split() for line in result.stdout.splitlines()))
    sha = references.get("refs/tags/" + tag + "^{}", references.get("refs/tags/" + tag))
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Cannot resolve upstream release tag")
    return sha


def make_plan(lock, release, event, run_number, resolve=resolve_tag):
    candidate = json.loads(json.dumps(lock))
    previous = lock.get("upstream_release")
    identity = release_identity(release)
    if previous is None:
        raise RuntimeError("Seed upstream_release in the lock before enabling discovery")
    newer = (identity != previous
             and identity["published_at"] >= previous["published_at"])
    if newer:
        candidate["base"] = resolve(lock["repository"], identity["tag_name"])
        candidate["upstream_release"] = identity
    else:
        identity = previous
    if run_number < 1 or run_number > 1000000000:
        raise RuntimeError("Invalid workflow run number")
    build = event in ("push", "workflow_dispatch") or (event == "schedule" and newer)
    return {
        "build": build, "upstream_changed": newer,
        "lock": candidate, "original_lock": lock,
        "tag": identity["tag_name"],
        "version_code": 1000000 + run_number,
    }


def public_rebuild(plan, manager, body, run_number):
    """Detect source changes and preserve upgrade ordering across release migrations."""
    codes = re.findall(r"APK versionCode: (\d+)", body)
    previous_code = max(map(int, codes), default=0)
    # The deleted distribution repo already shipped versionCode 2000006.
    plan["version_code"] = max(2000007, 2000000 + run_number, previous_code + 1)
    if "<!-- jp-source: " + hashlib.sha256(manager.encode()).hexdigest() + " -->" not in body:
        plan["build"] = True
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--run-number", required=True, type=int)
    parser.add_argument("--output", type=Path, default=Path("release-plan.json"))
    parser.add_argument("--release-repository", "--public-repository", dest="release_repository")
    parser.add_argument("--manager")
    args = parser.parse_args()
    pages = gh_json("repos/" + UPSTREAM + "/releases?per_page=100", "--paginate", "--slurp")
    release = latest_apk_release([release for page in pages for release in page])
    lock = json.loads((ROOT / "upstream.lock.json").read_text())
    plan = make_plan(lock, release, args.event, args.run_number)
    if args.release_repository:
        if not args.manager or not re.fullmatch(r"[0-9a-f]{40}", args.manager):
            parser.error("--release-repository requires an exact --manager SHA")
        existing = subprocess.run(
            ["gh", "release", "view", plan["tag"], "--repo", args.release_repository,
             "--json", "body", "--jq", ".body"], capture_output=True, text=True)
        if existing.returncode and "not found" not in existing.stderr.lower() and "404" not in existing.stderr:
            raise RuntimeError("Cannot inspect existing release")
        public_rebuild(plan, args.manager, existing.stdout if not existing.returncode else "", args.run_number)
        run = gh_json(f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        plan["version_code"] = max(plan["version_code"], workflow_version_code(run["created_at"]))
    args.output.write_text(json.dumps(plan, indent=2) + "\n")
    Path("candidate.lock.json").write_text(json.dumps(plan["lock"], indent=2) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key, value in (("build", str(plan["build"]).lower()),
                               ("tag", plan["tag"]), ("base", plan["lock"]["base"]),
                               ("version_code", str(plan["version_code"]))):
                output.write(key + "=" + value + "\n")
    print("Upstream:", plan["lock"]["upstream_release"]["tag_name"],
          "| base:", plan["lock"]["base"], "| build:", plan["build"])


if __name__ == "__main__":
    main()

