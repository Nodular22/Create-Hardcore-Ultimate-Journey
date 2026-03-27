#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import shlex
import subprocess
import sys
from pathlib import Path

from logging_utils import configure_logging


SCRIPTS_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("pack")
COMMAND_SCRIPTS = {
    "resolve": ("resolve_manifests.py", "Resolve manifests"),
    "readme": ("generate_readme.py", "Generate README"),
    "build": ("build_mrpack.py", "Build mrpacks"),
}


def run_step(script_name: str, extra_args: list[str], *, step_label: str) -> None:
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name), *extra_args]
    LOGGER.info("==> %s", step_label)
    LOGGER.debug("$ %s", shlex.join(cmd))
    subprocess.run(cmd, check=True)


def strip_separator(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def forwarded_args(args: list[str], *, verbose: bool) -> list[str]:
    forwarded = strip_separator(args)
    if verbose and "--verbose" not in forwarded:
        forwarded = [*forwarded, "--verbose"]
    return forwarded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified CLI for CHUJ modpack scripts")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show full subprocess commands and verbose child-script logs where supported",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve", help="run resolve_manifests.py")
    resolve.add_argument("args", nargs=argparse.REMAINDER, help="args passed to resolver")

    readme = subparsers.add_parser("readme", help="run generate_readme.py")
    readme.add_argument("args", nargs=argparse.REMAINDER, help="args passed to README generator")

    build = subparsers.add_parser("build", help="run build_mrpack.py")
    build.add_argument("args", nargs=argparse.REMAINDER, help="args passed to builder")

    all_cmd = subparsers.add_parser("all", help="resolve manifests, generate README, and build mrpacks")
    all_cmd.add_argument("--side", default="", help="pass through to build_mrpack.py --side")
    all_cmd.add_argument("--version", default="", help="pass through to build_mrpack.py --version")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)

    try:
        if args.command in COMMAND_SCRIPTS:
            script_name, step_label = COMMAND_SCRIPTS[args.command]
            run_step(
                script_name,
                forwarded_args(args.args, verbose=args.verbose),
                step_label=step_label,
            )
            return 0

        if args.command == "all":
            resolve_args = ["--target", "all"]
            if args.verbose:
                resolve_args.append("--verbose")
            run_step(
                "resolve_manifests.py",
                resolve_args,
                step_label="Resolve manifests",
            )
            run_step(
                "generate_readme.py",
                ["--verbose"] if args.verbose else [],
                step_label="Generate README",
            )

            build_args = ["--verbose"] if args.verbose else []
            if args.side:
                build_args.extend(["--side", args.side])
            if args.version:
                build_args.extend(["--version", args.version])
            run_step(
                "build_mrpack.py",
                build_args,
                step_label="Build mrpacks",
            )
            return 0

        raise ValueError(f"Unknown command: {args.command}")
    except subprocess.CalledProcessError as exc:
        if args.verbose:
            raise
        return exc.returncode
    except Exception as exc:
        if args.verbose:
            raise
        LOGGER.error("Error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
