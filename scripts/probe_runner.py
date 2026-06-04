#!/usr/bin/env python3
"""Standalone probe runner for subprocess execution.

Invoked by the agent probe loop as a child process.  Runs
``run_qemu_probe`` with the given arguments and prints the JSON result
to stdout.  Progress messages go to stderr so they don't pollute the
result stream.

Usage::

    python scripts/probe_runner.py \
        --output-dir output \
        --target-mcu STM32F407VG \
        --target-peripheral ETH \
        [--cve-findings '{"cve_id": "CVE-2021-1234"}']
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the project root is on sys.path so ``autoemu`` is importable
# when the script is executed directly (not via ``python -m``).
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QEMU probe and emit JSON result")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-mcu", required=True)
    parser.add_argument("--target-peripheral", required=True)
    parser.add_argument("--cve-findings", default=None, help="JSON-encoded CVE findings")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    cve_findings = None
    if args.cve_findings:
        try:
            cve_findings = json.loads(args.cve_findings)
        except json.JSONDecodeError:
            pass

    from autoemu.validators.qemu_probe_validator import run_qemu_probe

    def _progress(msg: str, kind: str = "info") -> None:
        print(f"[{kind}] {msg}", file=sys.stderr, flush=True)

    try:
        result = run_qemu_probe(
            output_dir=args.output_dir,
            target_mcu=args.target_mcu,
            target_peripheral=args.target_peripheral,
            qemu_build_env=None,
            cve_findings=cve_findings,
            on_progress=_progress,
        )
    except Exception as exc:
        result = {
            "success": False,
            "reason": f"Probe runner exception: {exc}",
            "probe_status": "error",
        }

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
