#!/usr/bin/env python3
"""Delete build artifacts and sealed requests older than CLEAN_DAYS days.

Runs from clean.yml on a cron and on demand. Lists the assets of the
`requests` release and every `results-*` release and deletes those whose
creation time is older than CLEAN_DAYS (180 days, aligned with the documented
retention; bump the constant to keep builds longer).

Environment:
  GH_TOKEN / GITHUB_TOKEN   token allowed to edit releases (contents: write)
  GITHUB_REPOSITORY         owner/repo
  DRY_RUN                   if "1", only print what would be deleted
"""

import json
import os
import subprocess
import sys
import datetime

CLEAN_DAYS = 180  # keep builds for 180 days; lower for a shorter retention


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY must be set", file=sys.stderr)
        return 2
    dry_run = os.environ.get("DRY_RUN") == "1"
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=CLEAN_DAYS)

    tags = ["requests", "results"] + [f"results-{c}" for c in "0123456789abcdef"]
    deleted = 0
    kept = 0
    for tag in tags:
        try:
            release = gh_json("api", f"repos/{repo}/releases/tags/{tag}")
        except subprocess.CalledProcessError:
            print(f"clean: release {tag} not found, skipping")
            continue
        if not release:
            continue
        page = 1
        while True:
            assets = gh_json(
                "api",
                f"repos/{repo}/releases/{release['id']}/assets?per_page=100&page={page}",
            )
            if not assets:
                break
            for asset in assets:
                created = datetime.datetime.strptime(asset["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc
                )
                if created < cutoff:
                    name = asset["name"]
                    if dry_run:
                        print(f"clean: would delete {tag}/{name} (created {asset['created_at']})")
                    else:
                        subprocess.run(
                            ["gh", "release", "delete-asset", tag, name, "--repo", repo, "--yes"],
                            check=True,
                        )
                        print(f"clean: deleted {tag}/{name}")
                    deleted += 1
                else:
                    kept += 1
            page += 1

    print(f"clean: {'would delete' if dry_run else 'deleted'} {deleted} asset(s), kept {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
