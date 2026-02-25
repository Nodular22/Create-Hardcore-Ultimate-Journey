#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent


def run_step(script_name: str, extra_args: list[str]) -> None:
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name), *extra_args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def strip_separator(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified CLI for CHUJ modpack scripts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve", help="Run resolve_manifests.py")
    resolve.add_argument("args", nargs=argparse.REMAINDER, help="Args passed to resolver")

    readme = subparsers.add_parser("readme", help="Run generate_readme.py")
    readme.add_argument("args", nargs=argparse.REMAINDER, help="Args passed to README generator")

    build = subparsers.add_parser("build", help="Run build_mrpack.py")
    build.add_argument("args", nargs=argparse.REMAINDER, help="Args passed to builder")

    all_cmd = subparsers.add_parser("all", help="Resolve manifests, generate README, and build mrpacks")
    all_cmd.add_argument("--side", default="", help="Pass through to build_mrpack.py --side")
    all_cmd.add_argument("--version", default="", help="Pass through to build_mrpack.py --version")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "resolve":
        run_step("resolve_manifests.py", strip_separator(args.args))
        return 0

    if args.command == "readme":
        run_step("generate_readme.py", strip_separator(args.args))
        return 0

    if args.command == "build":
        run_step("build_mrpack.py", strip_separator(args.args))
        return 0

    if args.command == "all":
        run_step("resolve_manifests.py", ["--target", "all"])
        run_step("generate_readme.py", [])

        build_args: list[str] = []
        if args.side:
            build_args.extend(["--side", args.side])
        if args.version:
            build_args.extend(["--version", args.version])
        run_step("build_mrpack.py", build_args)
        return 0

    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
