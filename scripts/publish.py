#!/usr/bin/env python3
"""Publish the outcome of a build job.

Runs in its own job, AFTER the sandboxed builder, and never executes mod code.
Verifies the produced library against the status document (size + SHA-256) and
uploads it as `<job>.so` + `<job>.json` to `results-<first hex of job>`, or, on
failure, `<job>.failed.json`. Also deletes the sealed request asset from the
`requests` release, and writes a digest summary to the step summary.

Environment:
  GH_TOKEN / GITHUB_TOKEN        token allowed to write release assets (contents: write)
  GITHUB_REPOSITORY              owner/repo
  GITHUB_EVENT_PATH              workflow_dispatch payload (job/ask)
  MODBUILD                       work root (default: ~/modbuild)
  GITHUB_STEP_SUMMARY            optional; gets the digest summary
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys


def gh(*args: str, check=True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["gh", *args], check=False, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        print(f"gh {' '.join(args)} failed ({proc.returncode}): {detail}", file=sys.stderr)
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc


def main() -> int:
    modbuild = pathlib.Path(os.environ.get("MODBUILD", str(pathlib.Path.home() / "modbuild")))
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY must be set", file=sys.stderr)
        return 2

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    job = ""
    ask = ""
    if event_path:
        try:
            inputs = json.load(open(event_path)).get("inputs", {})
            job = inputs.get("job", "")
            ask = inputs.get("ask", "")
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not read event inputs: {e}", file=sys.stderr)
    job = os.environ.get("JOB", job)
    ask = os.environ.get("ASK", ask)
    if len(job) != 24 or any(c not in "0123456789abcdef" for c in job):
        print(f"refusing to publish: job {job!r} is not 24 lowercase hex", file=sys.stderr)
        return 2

    result_dir = modbuild / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    status_path = result_dir / "status.json"
    lib_path = result_dir / "libhorizon_android.so"
    tag = f"results-{job[0]}"

    # Job ids are content-addressed, so a job can start with any hex digit.
    # Only 0-9/a/c/d/f had release shards, which made publish.py crash and
    # left the worker polling forever for a failure status.
    if gh("release", "view", tag, "--repo", repo, check=False).returncode != 0:
        made = gh(
            "release", "create", tag, "--repo", repo, "--prerelease",
            "--title", tag, "--notes", "Build result shard", check=False,
        )
        if made.returncode != 0:
            print(f"could not create release {tag}", file=sys.stderr)
            return 2

    status = None
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text())
        except json.JSONDecodeError as e:
            status = {"state": "failed", "error": f"status.json is not valid JSON: {e}"}
    if status is None:
        status = {"state": "failed", "error": "build job produced no status.json (the job failed before reporting)"}

    ok = (
        status.get("state") == "done"
        and lib_path.is_file()
        and status.get("size") == lib_path.stat().st_size
        and status.get("sha256") == hashlib.sha256(lib_path.read_bytes()).hexdigest()
    )
    if status.get("state") == "done" and not ok:
        status = {
            "state": "failed",
            "error": "status.json does not match the produced library (size/sha256 mismatch)",
            "build": status.get("build"),
        }

    summary_lines = []
    try:
        if ok:
            job_json = dict(status)
            job_json["job"] = job
            payload = result_dir / f"{job}.json"
            payload.write_text(json.dumps(job_json, indent=2, sort_keys=True) + "\n")
            lib_out = result_dir / f"{job}.so"
            lib_out.write_bytes(lib_path.read_bytes())
            gh("release", "upload", tag, str(lib_out), "--repo", repo, "--clobber")
            gh("release", "upload", tag, str(payload), "--repo", repo, "--clobber")
            # A previous failed attempt for the same content address may exist.
            gh("release", "delete-asset", tag, f"{job}.failed.json", "--repo", repo, "--yes", check=False)
            summary_lines = [
                f"job: {job}",
                f"state: done",
                f"sha256: {status['sha256']}",
                f"size: {status['size']}",
                f"release: {tag}",
            ]
        else:
            failed = {
                "state": "failed",
                "job": job,
                "error": status.get("error", "unknown failure"),
                "build": status.get("build"),
            }
            payload = result_dir / f"{job}.failed.json"
            payload.write_text(json.dumps(failed, indent=2, sort_keys=True) + "\n")
            gh("release", "upload", tag, str(payload), "--repo", repo, "--clobber")
            summary_lines = [f"job: {job}", "state: failed", f"error: {failed['error']}", f"release: {tag}"]
    finally:
        # The sealed mods are single-use: delete them whether or not publishing
        # succeeded (clean.yml catches whatever this misses).
        if ask and len(ask) == 32 and all(c in "0123456789abcdef" for c in ask):
            gh("release", "delete-asset", "requests", f"{ask}.bin", "--repo", repo, "--yes", check=False)

    summary = "\n".join(summary_lines) + "\n"
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write("### mod build\n\n```\n" + summary + "```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
