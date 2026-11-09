"""Resolve the shared native CI matrix against the configuration identity table."""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

REGISTRY = {
    "windows-x64-msvc": {"runner": "windows-2025-vs2026", "toolchain": "msvc"},
    "windows-arm64-msvc": {"runner": "windows-11-vs2026-arm", "toolchain": "msvc"},
    "linux-x64-clang-libcxx": {"runner": "ubuntu-26.04", "toolchain": "llvm-libcxx"},
    "linux-arm64-clang-libcxx": {"runner": "ubuntu-26.04-arm", "toolchain": "llvm-libcxx"},
    "linux-x64-clang-libcxx-sanitize": {"runner": "ubuntu-26.04", "toolchain": "llvm-libcxx"},
    "macos-arm64-clang-libcxx": {"runner": "macos-26", "toolchain": "llvm-libcxx"},
}
_ARCHITECTURES = {
    "amd64": "x64",
    "x64": "x64",
    "x86_64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def _identity_table(data: dict[str, Any], source: str) -> dict[str, dict[str, Any]]:
    if data.get("version") != 1 or not isinstance(data.get("identities"), list) or not data["identities"]:
        raise ValueError(f"{source}: expected a nonempty version 1 identity table")
    identities: dict[str, dict[str, Any]] = {}
    for row in data["identities"]:
        if not isinstance(row, dict):
            raise ValueError(f"{source}: every identity must be a table")
        name = row.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(f"{source}: invalid identity name {name!r}")
        if name in identities:
            raise ValueError(f"{source}: duplicate identity {name}")
        configurations = row.get("configurations")
        mapping = row.get("dependency-build-types")
        if (
            not isinstance(configurations, list)
            or not configurations
            or not all(isinstance(value, str) and value for value in configurations)
            or len(configurations) != len(set(configurations))
            or not isinstance(mapping, dict)
            or set(configurations) != set(mapping)
            or not all(isinstance(value, str) and value for value in mapping.values())
        ):
            raise ValueError(f"{source}: invalid configurations for {name}")
        keys = ("os", "arch", "compiler", "standard-library")
        if not all(isinstance(row.get(key), str) and row[key] for key in keys):
            raise ValueError(f"{source}: incomplete identity facts for {name}")
        identities[name] = row
    return identities


def load_canonical(configuration: Path) -> dict[str, dict[str, Any]]:
    """Render the canonical manifest directly from the configuration checkout."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    environment = Environment(
        loader=FileSystemLoader(configuration),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = environment.get_template("template/tool/identities.toml.jinja")
    return _identity_table(tomllib.loads(template.render()), str(configuration))


def resolve_matrix(
    canonical: dict[str, dict[str, Any]],
    selection: Any,
    *,
    target: str = "",
    targeted: bool = False,
    consumer: dict[str, dict[str, Any]] | None = None,
    mode: str = "cmake",
) -> dict[str, list[dict[str, Any]]]:
    """Validate identities and return the matrix GitHub Actions consumes."""
    if mode not in {"cmake", "command"}:
        raise ValueError(f"Unknown native mode: {mode!r}")
    if set(canonical) != set(REGISTRY):
        raise ValueError(
            f"Identity registry differs from configuration: "
            f"missing={sorted(set(canonical) - set(REGISTRY))}, extra={sorted(set(REGISTRY) - set(canonical))}"
        )
    if consumer is None and mode == "cmake":
        raise ValueError("A CMake caller must commit tool/identities.toml")
    if consumer is not None and consumer != canonical:
        raise ValueError("The caller's identity table differs from the canonical configuration table")
    if not isinstance(selection, list) or not selection or not all(isinstance(value, str) for value in selection):
        raise ValueError("Native identities must be a nonempty JSON array of names")
    if len(selection) != len(set(selection)):
        raise ValueError("Native identities must be unique")
    unknown = set(selection) - set(REGISTRY)
    if unknown:
        raise ValueError(f"Unknown native identities: {', '.join(sorted(unknown))}")
    if targeted and not target:
        raise ValueError("A targeted development check requires a nonempty target identity")
    if target and not targeted:
        raise ValueError("A full check cannot be restricted to a target identity")
    if targeted:
        if target not in selection:
            raise ValueError(f"Target identity {target!r} is not in the caller's declared identity selection")
        selection = [target]

    lanes = []
    for name in selection:
        facts = canonical[name]
        lanes.append(
            {
                "identity": name,
                **REGISTRY[name],
                "os": facts["os"],
                "arch": facts["arch"],
                "compiler": facts["compiler"],
                "configurations": [configuration.lower() for configuration in facts["configurations"]],
            }
        )
    return {"include": lanes}


def verify_runner(identity: str, operating_system: str, architecture: str, compiler: str, llvm_version: str) -> None:
    """Reject a runner or compiler that does not match the selected identity."""
    host_os = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    host_arch = _ARCHITECTURES.get(platform.machine().lower())
    if (host_os, host_arch) != (operating_system, architecture):
        raise ValueError(
            f"{identity} requires {operating_system}/{architecture}, "
            f"but the runner is {platform.system()}/{platform.machine()}"
        )
    executable = shutil.which("cl" if compiler == "msvc" else "clang")
    if executable is None:
        raise ValueError(f"Compiler {compiler!r} was not found on PATH for {identity}")
    if compiler == "msvc":
        result = subprocess.run([executable, "/Bv"], capture_output=True, text=True, check=False)
        banner = result.stdout + "\n" + result.stderr
        match = re.search(r"\bVersion\s+\d+\.\d+(?:\.\d+)*\s+for\s+(\S+)", banner, re.IGNORECASE)
        target = _ARCHITECTURES.get(match.group(1).lower()) if match else None
    elif compiler == "clang":
        version = subprocess.run([executable, "-dumpversion"], capture_output=True, text=True, check=False)
        result = subprocess.run([executable, "-dumpmachine"], capture_output=True, text=True, check=False)
        banner = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
        if (
            version.returncode != 0
            or banner.returncode != 0
            or not re.search(r"\bclang version \d", banner.stdout, re.IGNORECASE)
            or "Apple clang" in banner.stdout
            or version.stdout.strip().split(".", 1)[0] != llvm_version
        ):
            raise ValueError(f"{identity} requires upstream Clang {llvm_version}: {banner.stdout.strip()}")
        target = _ARCHITECTURES.get(result.stdout.strip().split("-", 1)[0].lower())
    else:
        raise ValueError(f"Unsupported compiler {compiler!r} for {identity}")
    if target != architecture:
        raise ValueError(f"Compiler {executable} targets {target!r}, not {architecture!r}, for {identity}")
    print(f"Verified {identity}: {platform.system()} {platform.machine()}, compiler {executable}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    resolve = subcommands.add_parser("resolve")
    resolve.add_argument("--configuration", type=Path, required=True)
    resolve.add_argument("--consumer", type=Path)
    resolve.add_argument("--selection", required=True)
    resolve.add_argument("--target", default="")
    resolve.add_argument("--targeted", choices=("true", "false"), default="false")
    resolve.add_argument("--mode", required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--identity", required=True)
    verify.add_argument("--os", required=True)
    verify.add_argument("--arch", required=True)
    verify.add_argument("--compiler", required=True)
    verify.add_argument("--llvm-version", required=True)
    args = parser.parse_args()
    if args.command == "verify":
        verify_runner(args.identity, args.os, args.arch, args.compiler, args.llvm_version)
        return

    canonical = load_canonical(args.configuration)
    consumer = None
    if args.consumer is not None and args.consumer.is_file():
        consumer = _identity_table(tomllib.loads(args.consumer.read_text(encoding="utf-8")), str(args.consumer))
    matrix = resolve_matrix(
        canonical,
        json.loads(args.selection),
        target=args.target,
        targeted=args.targeted == "true",
        consumer=consumer,
        mode=args.mode,
    )
    output = json.dumps(matrix, separators=(",", ":"))
    if output_file := os.environ.get("GITHUB_OUTPUT"):
        with Path(output_file).open("a", encoding="utf-8") as file:
            file.write(f"matrix={output}\n")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"Native matrix error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
