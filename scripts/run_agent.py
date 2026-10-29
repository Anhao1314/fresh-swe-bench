"""Batch runner for fresh-swe-bench: solve instances with an agent, then evaluate.

Per verified instance (must carry fail_to_pass):
  1. start a fsb-base container, clone repo at base_commit, install, apply test patch
  2. run the agent (mini-swe-agent DefaultAgent + LitellmModel) on the task text
  3. extract `git diff` as the model patch
  4. evaluate in a fresh container: apply model patch + test patch, run fail_to_pass tests

Usage:
  python scripts/run_agent.py --instances instances/click.jsonl \
      --model deepseek/deepseek-flash --out runs/deepseek-flash [--limit 2]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import yaml

import minisweagent
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models.litellm_model import LitellmModel

from harvest import split_diff

DOCKER = os.getenv("DOCKER", "docker")
BASE_IMAGE = "fsb-base:latest"
BUNDLED_CFG = Path(minisweagent.__file__).parent / "config" / "benchmarks" / "swebench.yaml"
FAILED_ID = re.compile(r"^(?:FAILED|ERROR) (tests/\S+)", re.M)


def _nl(s: str) -> str:
    """git apply requires the final line of a patch to end with a newline."""
    return s if s.endswith("\n") else s + "\n"


def sh(args: list, timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def load_templates() -> tuple[str, str]:
    cfg = yaml.safe_load(BUNDLED_CFG.read_text(encoding="utf-8"))
    return cfg["agent"]["system_template"], cfg["agent"]["instance_template"]


def cp_in(container: str, local: Path, remote: str) -> None:
    subprocess.run([DOCKER, "cp", str(local), f"{container}:{remote}"],
                   capture_output=True, check=True)


def _fetch_tarball(repo: str, base: str, dest: Path) -> str:
    """Fetch the repo tarball at `base` via api.github.com, fall back to
    codeload.github.com. Both survive when git-over-https to github.com is
    blocked. Returns "" or error."""
    for attempt in range(2):
        p = subprocess.run(["gh", "api", f"repos/{repo}/tarball/{base}"],
                           capture_output=True, timeout=300)
        if p.returncode == 0 and len(p.stdout) > 1000:
            dest.write_bytes(p.stdout)
            return ""
        time.sleep(3)
    rc, out = sh(["curl", "-sL", "--max-time", "240",
                  f"https://codeload.github.com/{repo}/tar.gz/{base}"], timeout=300)
    if rc == 0 and len(out) > 1000:
        dest.write_bytes(out.encode("latin-1", errors="surrogateescape"))
        return ""
    return f"tarball fetch failed: gh={p.stderr.decode(errors='ignore')[-150:]} curl={out[-150:]}"


def host_clone(repo: str, base: str, container: str) -> str:
    """Materialize the repo at `base` inside the container. GitHub tarball via
    host + docker cp (git-over-https to github.com is unreliable here); the
    container then gets a synthetic git repo whose state equals the base
    commit, so git apply/diff behave normally."""
    tmp = Path(tempfile.mkdtemp(prefix="fsbclone-"))
    try:
        err = _fetch_tarball(repo, base, tmp / "repo.tgz")
        if err:
            return err
        with tarfile.open(tmp / "repo.tgz") as tf:
            tf.extractall(tmp / "x", filter="data")
        roots = list((tmp / "x").iterdir())
        if len(roots) != 1:
            return f"unexpected tarball layout: {[r.name for r in roots]}"
        rc, out = sh([DOCKER, "cp", f"{roots[0]}/.", f"{container}:/testbed"], timeout=600)
        if rc != 0:
            return f"docker cp failed: {out[-200:]}"
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def setup_env(container: str, inst: dict) -> str:
    """Populate /testbed at base commit, install, apply test patch.
    Returns "" or error tail."""
    err = host_clone(inst["repo"], inst["base_commit"], container)
    if err:
        return err
    script = ("cd /testbed && git init -q && git add -A && "
              "git -c user.email=agent@local -c user.name=agent commit -q -m base && "
              "pip install -q -e . pytest && git apply /tmp/test.patch")
    rc, out = sh([DOCKER, "exec", "-w", "/root", container, "bash", "-lc",
                  script + " && echo SETUP_OK"], timeout=900)
    return "" if "SETUP_OK" in out else out[-800:]


def run_agent_on(inst: dict, out_dir: Path, model_name: str, sys_t: str, ins_t: str) -> dict:
    traj_path = out_dir / f"{inst['instance_id']}.traj.json"
    patch_path = out_dir / f"{inst['instance_id']}.patch"
    env = DockerEnvironment(image=BASE_IMAGE, cwd="/testbed", timeout=600,
                            container_timeout="2h")
    t0 = time.time()
    try:
        Path("test.patch").write_text(_nl(inst["test_patch"]), encoding="utf-8", newline="\n")
        cp_in(env.container_id, Path("test.patch"), "/tmp/test.patch")
        os.remove("test.patch")
        err = setup_env(env.container_id, inst)
        if err:
            return {"instance_id": inst["instance_id"], "resolved": False,
                    "error": f"setup: {err[-200:]}"}
        model = LitellmModel(model_name=model_name)
        agent = DefaultAgent(model, env, system_template=sys_t,
                             instance_template=ins_t, step_limit=100,
                             cost_limit=2.0, output_path=traj_path)
        try:
            agent.run(inst["task_text"])
        except Exception as e:
            print(f"  agent ended with {type(e).__name__}: {str(e)[:120]}")
        # extract via raw docker exec: the agent env truncates long output,
        # which would corrupt the patch; keep source files only (eval applies
        # the official test patch itself)
        _, raw = sh([DOCKER, "exec", env.container_id, "bash", "-lc",
                     "cd /testbed && git diff && git diff --cached"], timeout=180)
        _, patch = split_diff(raw.strip())
        patch_path.write_text(_nl(patch), encoding="utf-8", newline="\n")
        cost = n_calls = 0
        if traj_path.exists():
            stats = json.loads(traj_path.read_text(encoding="utf-8"))["info"]["model_stats"]
            cost, n_calls = stats.get("instance_cost", 0.0), stats.get("api_calls", 0)
        return {"instance_id": inst["instance_id"], "resolved": None,
                "patch_bytes": len(patch), "cost": cost, "n_calls": n_calls,
                "duration_s": round(time.time() - t0)}
    finally:
        env.cleanup()


def evaluate(inst: dict, patch_path: Path) -> dict:
        # strip only newlines: a plain .strip() would eat whitespace-only
        # context lines at the patch tail and corrupt the hunk line counts
        patch = patch_path.read_text(encoding="utf-8").strip("\n") if patch_path.exists() else ""
        if not patch:
            return {"resolved": False, "detail": "empty patch"}
        name = f"fsb-eval-{uuid.uuid4().hex[:8]}"
        subprocess.run([DOCKER, "run", "-d", "--name", name, BASE_IMAGE, "sleep", "1500"],
                       capture_output=True, check=True)
        try:
            Path("test.patch").write_text(_nl(inst["test_patch"]), encoding="utf-8", newline="\n")
            Path("model.patch").write_text(_nl(patch), encoding="utf-8", newline="\n")
            cp_in(name, Path("test.patch"), "/tmp/test.patch")
            cp_in(name, Path("model.patch"), "/tmp/model.patch")
            os.remove("test.patch"); os.remove("model.patch")
            err = setup_env(name, inst)
            if err:
                return {"resolved": False, "detail": f"eval setup: {err[-200:]}"}
            rc, out = sh([DOCKER, "exec", name, "bash", "-lc",
                          "cd /testbed && git apply --whitespace=nowarn /tmp/model.patch"],
                         timeout=120)
            if rc != 0:
                return {"resolved": False,
                        "detail": f"model patch failed to apply: {out[-300:]}"}
            ids = " ".join(f"'{t}'" for t in inst["fail_to_pass"])
            _, out = sh([DOCKER, "exec", name, "bash", "-lc",
                         "cd /testbed && source /opt/miniconda3/bin/activate testbed 2>/dev/null; "
                         f"python -m pytest -v {ids} 2>&1; true"], timeout=900)
            f2p = inst["fail_to_pass"]
            passed = [t for t in f2p
                      if any(t in line and "PASSED" in line for line in out.splitlines())]
            return {"resolved": len(passed) == len(f2p),
                    "detail": f"F2P {len(passed)}/{len(f2p)}"}
        finally:
            subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", required=True)
    ap.add_argument("--model", default="deepseek/deepseek-flash")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sys_t, ins_t = load_templates()
    instances = [i for i in (json.loads(l) for l in
                 open(args.instances, encoding="utf-8")) if i.get("fail_to_pass")]
    if args.limit:
        instances = instances[:args.limit]
    print(f"running {len(instances)} verified instances with {args.model}\n")

    results = []
    for inst in instances:
        iid = inst["instance_id"]
        print(f"=== {iid} ===")
        meta = run_agent_on(inst, out_dir, args.model, sys_t, ins_t)
        ev = evaluate(inst, out_dir / f"{iid}.patch")
        meta.update(ev)
        results.append(meta)
        print(f"  resolved={meta['resolved']} {meta.get('detail','')} "
              f"cost=${meta.get('cost', 0):.4f} calls={meta.get('n_calls', '?')}")

    (out_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    n_ok = sum(1 for r in results if r["resolved"])
    total_cost = sum(r.get("cost", 0) for r in results)
    lines = [f"# fresh-swe-bench baseline: {args.model}", "",
             f"**resolved {n_ok}/{len(results)}**, total agent cost ${total_cost:.4f}", "",
             "| instance | resolved | detail | cost | calls |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['instance_id']} | {'✅' if r['resolved'] else '❌'} "
                     f"| {r.get('detail','')} | ${r.get('cost', 0):.4f} | {r.get('n_calls','-')} |")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nresolved {n_ok}/{len(results)} | report -> {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
