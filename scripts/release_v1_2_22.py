#!/usr/bin/env python3
"""Publish v1.2.22 and retarget the existing marketplace verification issue.

Run from any directory: python3 /path/to/repo/scripts/release_v1_2_22.py
Use --dry-run to validate and preview without committing or publishing.
Safe to rerun after a partial failure; existing matching tags/releases are reused.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REPO = "gladimdim/omarchy-config-sync-plugin"
MARKETPLACE = "omacom/omarchy-plugin-marketplace"
PLUGIN_ID = "gladimdim.config-sync"
VERSION = "1.2.22"
TAG = f"v{VERSION}"
RELEASE_TITLE = f"Omarchy Config Sync {TAG}"
VERIFY_TITLE = f"[Verify]: Omarchy Config Sync {TAG}"
RELEASE_NOTES = """Shortcuts defined with `o.rebind(...)` now appear in outgoing changes and can be selected for sync. This fixes missing shortcuts such as Super+Down and Super+Up when overriding Omarchy defaults. Publishing preserves the original `o.rebind(...)` call.

Validation: the Python test suite and Omarchy plugin validation passed.
"""
RELEASE_FILES = {
    "manifest.json",
    "scripts/config_sync.py",
    "tests/test_config_sync.py",
    "scripts/release_v1_2_22.py",
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def run(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"},
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        fail(f"Command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result.stdout or ""


def git(*args: str) -> str:
    return run("git", *args).strip()


def api_pages(endpoint: str) -> list[dict]:
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", endpoint))
    return [item for page in pages for item in page]


def section(body: str, heading: str) -> str:
    marker = f"### {heading}\n"
    normalized = body.replace("\r\n", "\n")
    if marker not in normalized:
        return ""
    return normalized.split(marker, 1)[1].split("\n### ", 1)[0].strip()


def find_issue() -> dict:
    candidates = [
        issue
        for issue in api_pages(f"repos/{MARKETPLACE}/issues?state=open&per_page=100")
        if "pull_request" not in issue
        and issue["title"].startswith("[Verify]")
        and section(issue.get("body") or "", "Plugin ID") == PLUGIN_ID
    ]
    if len(candidates) != 1:
        urls = "\n".join(issue["html_url"] for issue in candidates)
        fail(f"Expected exactly one open [Verify] issue for {PLUGIN_ID}; found {len(candidates)}.\n{urls}")
    issue = candidates[0]
    if issue["user"]["login"] != "gladimdim":
        fail(f"The verification issue belongs to another author: {issue['html_url']}")
    return issue


def listing_commit() -> str:
    catalog = json.loads(run(
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--connect-timeout", "15", "--max-time", "60",
        "https://plugins.omarchy.org/catalog.json",
    ))
    for plugin in catalog["plugins"]:
        if plugin.get("id") == PLUGIN_ID:
            return plugin.get("listingValidatedCommit") or ""
    fail(f"Plugin {PLUGIN_ID} is missing from the live catalog.")


def verification_body(sha: str) -> str:
    return f"""### Verification action

Verify and publish a newer upstream commit

### Plugin ID

{PLUGIN_ID}

### Repository URL

https://github.com/{REPO}

### Target commit

{sha}

### Verification acknowledgment

- [x] I understand that only the exact target commit can become a verified marketplace snapshot and that verification is not a security audit.

### Standard installation acknowledgment

- [x] I confirm that this listed root plugin supports the standard Omarchy installation path and does not require manual setup.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Run preflight/tests and fetch refs; do not commit, tag, push, or edit GitHub.")
    args = parser.parse_args()
    for command in ("git", "gh", "curl", "omarchy"):
        if not shutil.which(command):
            fail(f"Required command is missing: {command}")
    if json.loads((ROOT / "manifest.json").read_text())["version"] != VERSION:
        fail(f"manifest.json must already contain version {VERSION}.")
    if git("branch", "--show-current") != "main":
        fail("Switch to main before running this release script.")
    for direction in ((), ("--push",)):
        remote = git("remote", "get-url", *direction, "origin")
        if remote not in {f"https://github.com/{REPO}.git", f"https://github.com/{REPO}",
                          f"git@github.com:{REPO}.git", f"ssh://git@github.com/{REPO}.git"}:
            fail(f"Unexpected origin URL: {remote}")

    print("Checking GitHub authentication and fetching origin...", flush=True)
    login = run("gh", "api", "user", "--jq", ".login").strip()
    if login != "gladimdim":
        fail(f"gh is authenticated as {login}; expected gladimdim.")
    git("fetch", "origin", "--tags")
    if git("merge-base", "origin/main", "HEAD") != git("rev-parse", "origin/main"):
        fail("origin/main has changes missing locally. Integrate them and rerun; this script never force-pushes.")
    if git("ls-files", "--unmerged"):
        fail("Resolve the current merge conflicts first.")
    dirty = set(run("git", "diff", "HEAD", "--name-only", "-z").split("\0"))
    dirty.update(run("git", "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    dirty.discard("")
    ahead = set(run("git", "diff", "origin/main...HEAD", "--name-only", "-z").split("\0")) - {""}
    unrelated = (dirty | ahead) - RELEASE_FILES
    if unrelated:
        fail("Unrelated changes would enter this release; handle them first:\n" + "\n".join(sorted(unrelated)))

    # Check the marketplace before publishing, then re-check just before editing.
    verified = listing_commit()
    issue = None if verified == git("rev-parse", "HEAD") and not dirty else find_issue()
    if issue:
        print(f"Verification issue to update: {issue['html_url']}", flush=True)
    releases = api_pages(f"repos/{REPO}/releases?per_page=100")
    release = next((item for item in releases if item["tag_name"] == TAG), None)
    tags = git("tag", "--list", TAG).splitlines()
    if tags and (dirty or git("rev-parse", f"{TAG}^{{commit}}") != git("rev-parse", "HEAD")):
        fail(f"{TAG} already exists and does not match the proposed release. It will not be moved.")

    print("Running the Python test suite and Omarchy plugin validation...", flush=True)
    run(sys.executable, "-B", "-m", "unittest", "tests.test_config_sync", "-q", capture=False)
    run("omarchy", "plugin", "validate", ".", capture=False)
    git("diff", "--check")
    git("diff", "--cached", "--check")
    if args.dry_run:
        print(f"Preflight passed. Would commit the release files, push main and {TAG}, publish the GitHub release, and update the existing verification issue if needed.")
        return

    if dirty:
        git("add", "--", *sorted(RELEASE_FILES))
        run("git", "commit", "-m", f"Release {TAG}: detect o.rebind shortcuts", capture=False)
    sha = git("rev-parse", "HEAD")
    if not tags:
        git("tag", "-a", TAG, "-m", RELEASE_TITLE)
    print(f"Publishing {TAG} at {sha}...", flush=True)
    run("git", "push", "--atomic", "origin", "HEAD:refs/heads/main", f"refs/tags/{TAG}", capture=False)
    git("fetch", "origin")
    upstream_sha = git("rev-parse", "origin/main")
    if upstream_sha != sha:
        fail(f"origin/main moved unexpectedly to {upstream_sha}; refusing to verify a different release.")

    with tempfile.TemporaryDirectory(prefix="omarchy-config-sync-release-") as tmp:
        notes = Path(tmp) / "release.md"
        notes.write_text(RELEASE_NOTES)
        if release is None:
            run("gh", "release", "create", TAG, "--repo", REPO,
                "--verify-tag", "--title", RELEASE_TITLE, "--notes-file", str(notes), capture=False)
        elif release["draft"]:
            run("gh", "release", "edit", TAG, "--repo", REPO,
                "--draft=false", "--title", RELEASE_TITLE, "--notes-file", str(notes), capture=False)
        print(f"Release: https://github.com/{REPO}/releases/tag/{TAG}", flush=True)

        if listing_commit() == upstream_sha:
            print("Marketplace listing already verifies this exact commit; no issue update needed.")
            return
        current_issue = find_issue()
        if issue and current_issue["number"] != issue["number"]:
            fail("The open verification issue changed during publication. Rerun to review the new issue.")
        body = verification_body(upstream_sha)
        if current_issue["title"] != VERIFY_TITLE or (current_issue.get("body") or "").strip() != body.strip():
            body_path = Path(tmp) / "verification.md"
            body_path.write_text(body)
            run("gh", "issue", "edit", str(current_issue["number"]), "--repo", MARKETPLACE,
                "--title", VERIFY_TITLE, "--body-file", str(body_path), capture=False)
        updated = json.loads(run("gh", "api", f"repos/{MARKETPLACE}/issues/{current_issue['number']}"))
        if updated["title"] != VERIFY_TITLE or section(updated.get("body") or "", "Target commit") != upstream_sha:
            fail("Could not confirm that the verification issue contains the new version and SHA.")
        print(f"Verification: {updated['html_url']}")
        print(f"Target commit: {upstream_sha}")
        print("Release published and verification issue updated. Marketplace approval is pending.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f"\nStopped: {error}\nFix the reported problem and rerun this script to resume.", file=sys.stderr)
        sys.exit(1)
