"""Run a task-spec JSON headlessly, without an MCP client.

A spec is an ordered list of operations::

    {"ops": [{"op": "load_model", "args": {"model": "arm26"}}, ...]}

Structured ops go to ``opensim_runner``; ``run_python`` goes to the persistent
worker, so a Moco study built in one step is still alive in the next.

    python scripts/run_spec.py template/gait-pipeline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import opensim_runner as runner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="path to a task-spec JSON file")
    ap.add_argument("--stop-on-error", action="store_true",
                    help="abort at the first failing op (default: continue)")
    ns = ap.parse_args(argv)

    spec = json.loads(Path(ns.spec).read_text(encoding="utf-8"))
    ops = spec.get("ops", [])
    if not ops:
        print("spec has no 'ops'")
        return 2

    worker = None
    failures = 0
    try:
        for i, item in enumerate(ops, 1):
            op = item.get("op")
            args = item.get("args", {})
            print(f"\n=== [{i}/{len(ops)}] {op} ===", flush=True)

            if op == "run_python":
                if worker is None:
                    import opensim_mcp_server as mcp_server

                    worker = mcp_server.WorkerClient()
                result = worker.request(
                    {"op": "exec", "source": args.get("source", ""),
                     "cwd": args.get("cwd")}, args.get("timeout", 600)
                )
                if result.get("stdout"):
                    print(result["stdout"].rstrip())
            else:
                result = runner.run_op(op, args)

            if not result.get("ok"):
                failures += 1
                print("FAILED:", result.get("error"))
                if result.get("logs"):
                    print("logs tail:", result["logs"][-400:])
                if ns.stop_on_error:
                    return 1
            else:
                print(json.dumps(result.get("data", {}), ensure_ascii=False)[:600])

        return 1 if failures else 0
    finally:
        if worker is not None:
            worker.stop()


if __name__ == "__main__":
    sys.exit(main())
