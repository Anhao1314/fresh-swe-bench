"""Evaluate a mini-swe-agent trajectory against its SWE-bench instance.

Pipeline: extract the model patch from the trajectory JSON -> start a fresh
container from the instance image -> apply model patch + test patch -> run the
instance's FAIL_TO_PASS / PASS_TO_PASS tests -> print a per-test verdict.

Usage:
    python scripts/evaluate.py trajectories/smoke-0 [instance_id]

Requires the instance's docker image locally (swebench/sweb.eval.x86_64.*).
"""
import ast
import json
import os
import subprocess
import sys
import time
import uuid

DOCKER = os.getenv("DOCKER", "docker")
DATASET = "princeton-nlp/SWE-bench_Lite"


def sh(args: list[str], timeout: int = 300) -> tuple[int, str]:
    """Run a host command, return (returncode, combined output)."""
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)


def extract_patch(trajectory_path: str) -> str:
    """Return the last diff block found in the trajectory JSON."""
    with open(trajectory_path, encoding="utf-8") as f:
        traj = json.load(f)
    messages = traj.get("messages") or traj.get("trajectory") or []
    best = None
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str) and "diff --git a/" in content:
            start = content.index("diff --git a/")
            best = content[start:].strip()
    if not best:
        sys.exit("no patch found in trajectory")
    return best


def load_instance(instance_id: str) -> dict:
    from datasets import load_dataset

    for inst in load_dataset(DATASET, split="dev"):
        if inst["instance_id"] == instance_id:
            return inst
    sys.exit(f"instance {instance_id} not found in {DATASET}")


def find_image(instance_id: str) -> str:
    _, out = sh([DOCKER, "images", "--format", "{{.Repository}}:{{.Tag}}"])
    exact = f"swebench/sweb.eval.x86_64.{instance_id.lower()}:latest"
    if exact in out:
        return exact
    issue_no = instance_id.rsplit("-", 1)[-1]
    for line in out.splitlines():
        if "sweb.eval" in line and issue_no in line:
            return line
    sys.exit(f"no local image for {instance_id}; pull it first")


def in_container(container: str, cmd: str, timeout: int = 600) -> tuple[int, str]:
    return sh([DOCKER, "exec", container, "bash", "-lc", cmd], timeout=timeout)


def main() -> None:
    traj_path = sys.argv[1]
    instance_id = sys.argv[2] if len(sys.argv) > 2 else None

    if instance_id is None:
        with open(traj_path, encoding="utf-8") as f:
            instance_id = json.load(f)["info"].get("instance_id")
        if not instance_id:
            sys.exit("pass instance_id as argv[2] (not found in trajectory info)")

    inst = load_instance(instance_id)
    image = find_image(instance_id)
    patch = extract_patch(traj_path)
    f2p = ast.literal_eval(inst["FAIL_TO_PASS"])
    p2p = ast.literal_eval(inst["PASS_TO_PASS"])
    node_ids = list(f2p) + list(p2p)

    print(f"instance: {instance_id}\nimage:    {image}\n"
          f"patch:    {len(patch)} bytes | F2P {len(f2p)} | P2P {len(p2p)}\n")

    name = f"fsb-eval-{uuid.uuid4().hex[:8]}"
    subprocess.run([DOCKER, "run", "-d", "--name", name, image, "sleep", "1200"],
                   capture_output=True, text=True, check=True)
    try:
        with open("patch.diff", "w", newline="\n") as f:
            f.write(patch + "\n")
        with open("test.patch", "w", newline="\n") as f:
            f.write(inst["test_patch"])
        subprocess.run([DOCKER, "cp", "patch.diff", f"{name}:/tmp/patch.diff"],
                       capture_output=True, check=True)
        subprocess.run([DOCKER, "cp", "test.patch", f"{name}:/tmp/test.patch"],
                       capture_output=True, check=True)

        rc, out = in_container(name, "cd /testbed && git checkout -f "
                               + inst["base_commit"])
        rc, out = in_container(name, "cd /testbed && git apply /tmp/patch.diff")
        if rc != 0:
            sys.exit(f"model patch failed to apply:\n{out[:2000]}")
        rc, out = in_container(name, "cd /testbed && git apply /tmp/test.patch")
        if rc != 0:
            sys.exit(f"test patch failed to apply:\n{out[:2000]}")

        pytest_ids = " ".join(f"'{n}'" for n in node_ids)
        t0 = time.time()
        rc, out = in_container(
            name,
            f"cd /testbed && source /opt/miniconda3/bin/activate testbed && "
            f"python -m pytest -v {pytest_ids} 2>&1; echo EXIT:$?",
            timeout=900,
        )
        print(f"pytest done in {time.time()-t0:.0f}s (exit marker {rc})\n")

        def status(node: str) -> str:
            for line in out.splitlines():
                if node in line:
                    if "PASSED" in line or line.strip().endswith("PASS"):
                        return "PASS"
                    if "FAILED" in line or "ERROR" in line:
                        return "FAIL"
            return "MISSING"

        print(f"{'status':8} test")
        f2p_fail = [t for t in f2p if status(t) != "PASS"]
        p2p_fail = [t for t in p2p if status(t) != "PASS"]
        for t in f2p:
            print(f"{status(t):8} [F2P] {t}")
        for t in p2p:
            print(f"{status(t):8} [P2P] {t}")

        verdict = "RESOLVED ✅" if not f2p_fail and not p2p_fail else "NOT RESOLVED ❌"
        print(f"\n{verdict}  (F2P {len(f2p)-len(f2p_fail)}/{len(f2p)}, "
              f"P2P {len(p2p)-len(p2p_fail)}/{len(p2p)})")
        if f2p_fail or p2p_fail:
            sys.exit(1)
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
        for tmp in ("patch.diff", "test.patch"):
            if os.path.exists(tmp):
                os.remove(tmp)


if __name__ == "__main__":
    main()
