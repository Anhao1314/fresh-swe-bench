"""Harvest fresh-swe-bench instances from recent merged PRs.

Three stages:
    scan    list merged PRs since --since that touch both tests and source,
            emit candidate metadata (JSONL)
    build   split each candidate's diff into test/golden patches, attach task
            text (linked issue when available), emit instances (JSONL)
    verify  in docker, confirm each instance's tests fail at base and pass
            after the golden patch; only verified instances are real tasks

Usage:
    python scripts/harvest.py scan   --repo pallets/click --since 2026-08-01 --out harvest/click.jsonl
    python scripts/harvest.py build  --harvest harvest/click.jsonl --out instances/click.jsonl
    python scripts/harvest.py verify --instances instances/click.jsonl [--limit 3]

Requires: gh CLI authenticated; docker for verify; network for pip/git in containers.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

GH = os.getenv("GH", "gh")
DOCKER = os.getenv("DOCKER", "docker")
BASE_IMAGE = "fsb-base:latest"
CLOSE_RE = re.compile(r"(?im)^\s*(?:close[sd]?|fix(?:e[sd]?)?|resolve[sd]?)\s*:?\s+#(\d+)")


def gh_api(path: str, accept: str | None = None, timeout: int = 60) -> str:
    cmd = [GH, "api", path]
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    last_err = ""
    for attempt in range(4):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode == 0:
            return p.stdout
        last_err = p.stderr[:400]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gh api {path}: {last_err}")


def gh_json(path: str) -> dict | list:
    return json.loads(gh_api(path))


def write_jsonl(path: str, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows -> {path}")


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- scan stage

def scan(repo: str, since: str, out: str) -> None:
    """Collect merged PRs (created after `since`) touching tests + source."""
    candidates, page = [], 1
    while page <= 30:
        prs = gh_json(f"repos/{repo}/pulls?state=closed&sort=created"
                      f"&direction=desc&per_page=100&page={page}")
        if not prs:
            break
        stop = False
        for pr in prs:
            if pr["created_at"][:10] < since:
                stop = True
                break
            if not pr.get("merged_at"):
                continue
            if pr["title"].lower().startswith(
                    ("merge ", "release ", "bump ", "chore")):
                continue  # merges / releases are not tasks
            files = gh_json(f"repos/{repo}/pulls/{pr['number']}/files?per_page=100")
            paths = [f["filename"] for f in files]
            changes = sum(f["changes"] for f in files)
            test_files = [p for p in paths if p.startswith("tests/")]
            src_files = [p for p in paths if p.endswith(".py")
                         and not p.startswith(("tests/", "docs/", "examples/"))]
            if not test_files or not src_files:
                continue
            if len(paths) > 15 or changes > 800:
                continue  # too broad to be a focused task
            candidates.append({
                "repo": repo,
                "number": pr["number"],
                "instance_id": f"{repo.split('/')[0]}__{repo.split('/')[1]}"
                               f"-pr{pr['number']}",
                "title": pr["title"],
                "body": (pr.get("body") or "")[:4000],
                "base_commit": pr["base"]["sha"],
                "merged_at": pr["merged_at"],
                "test_files": test_files,
                "src_files": src_files,
                "changes": changes,
            })
            print(f"  candidate #{pr['number']}: {pr['title'][:60]} "
                  f"(+{changes} lines, {len(test_files)} test files)")
        if stop:
            break
        page += 1
    write_jsonl(out, candidates)


# --------------------------------------------------------------- build stage

def split_diff(diff: str) -> tuple[str, str]:
    """Split a PR diff into (test_patch, golden_patch); drop docs/metadata."""
    sections, current = [], None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            current = [line]
            sections.append(current)
        elif current is not None:
            current.append(line)

    def path_of(section: list[str]) -> str:
        m = re.match(r"diff --git a/(.+?) b/(.+)", section[0])
        return (m.group(2) or m.group(1)) if m else ""

    test, golden = [], []
    for sec in sections:
        p = path_of(sec)
        body = "".join(sec)
        if p.startswith("tests/"):
            test.append(body)
        elif p.endswith(".py") and not p.startswith(("docs/", "examples/")):
            golden.append(body)
        # everything else (docs, CHANGES.rst, CI) is dropped
    return "".join(test), "".join(golden)


def task_text(cand: dict) -> str:
    """Prefer the linked issue text (a problem statement); fall back to PR text."""
    m = CLOSE_RE.search(cand.get("body") or "")
    if m:
        issue_no = m.group(1)
        try:
            issue = gh_json(f"repos/{cand['repo']}/issues/{issue_no}")
            return (f"{issue['title']}\n\n{issue.get('body') or ''}")[:3500]
        except Exception:
            pass
    return (f"{cand['title']}\n\n{cand.get('body') or ''}")[:3500]


def build(harvest_path: str, out: str) -> None:
    instances, skipped = [], 0
    for cand in read_jsonl(harvest_path):
        try:
            diff = gh_api(f"repos/{cand['repo']}/pulls/{cand['number']}",
                          accept="application/vnd.github.v3.diff")
        except Exception as e:
            print(f"  skip #{cand['number']}: {e}")
            skipped += 1
            continue
        test_patch, golden = split_diff(diff)
        if not test_patch.strip() or not golden.strip():
            skipped += 1
            continue
        instances.append({**cand, "task_text": task_text(cand),
                          "test_patch": test_patch, "golden_patch": golden})
        print(f"  built {cand['instance_id']} "
              f"(test {len(test_patch)}B / golden {len(golden)}B)")
    write_jsonl(out, instances)
    print(f"skipped {skipped} candidates")


# ------------------------------------------------------------- verify stage

def ensure_base_image() -> None:
    p = subprocess.run([DOCKER, "images", "-q", BASE_IMAGE],
                       capture_output=True, text=True)
    if p.stdout.strip():
        return
    print(f"building {BASE_IMAGE} ...")
    dockerfile = ("FROM python:3.12-slim\n"
                  "RUN apt-get update && apt-get install -y -q git build-essential\n"
                  "RUN rm -rf /var/lib/apt/lists/*\n")
    subprocess.run([DOCKER, "build", "-t", BASE_IMAGE, "-"],
                   input=dockerfile.encode(), check=True)


def run_script(name: str, script: str, timeout: int = 1200) -> tuple[int, str]:
    p = subprocess.run([DOCKER, "exec", name, "bash", "-lc", script],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


FAILED_ID = re.compile(r"^(?:FAILED|ERROR) (tests/\S+)", re.M)


def verify(instances_path: str, limit: int) -> None:
    ensure_base_image()
    instances = read_jsonl(instances_path)
    for inst in instances[:limit]:
        iid, repo = inst["instance_id"], inst["repo"]
        name = f"fsb-verify-{uuid.uuid4().hex[:8]}"
        print(f"\n=== {iid} ===")
        subprocess.run([DOCKER, "run", "-d", "--name", name, "-w", "/work",
                        BASE_IMAGE, "sleep", "2400"],
                       capture_output=True, text=True, check=True)
        try:
            for fname, key in (("test.patch", "test_patch"),
                               ("golden.patch", "golden_patch")):
                Path(fname).write_text(inst[key], encoding="utf-8", newline="\n")
                subprocess.run([DOCKER, "cp", fname, f"{name}:/tmp/{fname}"],
                               capture_output=True, check=True)
                os.remove(fname)
            setup = (f"git clone -q https://github.com/{repo} /work/repo && "
                     f"cd /work/repo && git checkout -q {inst['base_commit']} && "
                     f"pip install -q -e . pytest 2>&1 | tail -1 && "
                     f"git apply /tmp/test.patch && echo SETUP_OK")
            rc, out = run_script(name, setup, timeout=900)
            if "SETUP_OK" not in out:
                print(f"  setup failed:\n{out[-800:]}")
                continue
            targets = " ".join(inst["test_files"])
            base_run = run_script(name, f"cd /work/repo && python -m pytest -v "
                                        f"{targets} 2>&1; true")[1]
            f2p = sorted(set(FAILED_ID.findall(base_run)))
            if not f2p:
                print("  REJECT: tests already pass at base (no signal)")
                continue
            rc, out = run_script(name, f"cd /work/repo && git apply "
                                       f"/tmp/golden.patch && echo APPLY_OK")
            if "APPLY_OK" not in out:
                print(f"  REJECT: golden patch does not apply:\n{out[-500:]}")
                continue
            fixed_run = run_script(name, f"cd /work/repo && python -m pytest -v "
                                         f"{targets} 2>&1; true")[1]
            still_failing = sorted(set(FAILED_ID.findall(fixed_run)))
            if still_failing:
                print(f"  REJECT: {len(still_failing)} tests still fail after "
                      f"golden patch: {still_failing[:3]}")
                continue
            inst["fail_to_pass"] = f2p
            print(f"  VERIFIED: {len(f2p)} fail-to-pass tests "
                  f"(e.g. {f2p[0][:70]})")
        finally:
            subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
    write_jsonl(instances_path, instances)
    print("\nverify pass done (verified instances carry fail_to_pass field)")


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="stage", required=True)
    s = sub.add_parser("scan");  s.add_argument("--repo", required=True)
    s.add_argument("--since", default="2026-08-01"); s.add_argument("--out", required=True)
    b = sub.add_parser("build"); b.add_argument("--harvest", required=True)
    b.add_argument("--out", required=True)
    v = sub.add_parser("verify"); v.add_argument("--instances", required=True)
    v.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()
    if args.stage == "scan":
        scan(args.repo, args.since, args.out)
    elif args.stage == "build":
        build(args.harvest, args.out)
    else:
        verify(args.instances, args.limit)


if __name__ == "__main__":
    main()
