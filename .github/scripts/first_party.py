"""Validate a caller's first-party pins and export the pinned Conan recipes.

A caller that depends on packages no remote serves yet commits ``tool/first-party.toml``.
Each native lane exports the pinned recipes into its own Conan cache; nothing is built here.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

_NAME = re.compile(r"[a-z0-9_][a-z0-9_+.-]{1,100}")
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_KEYS = {"name", "repository", "commit"}


def load_pins(path: Path, caller: str = "") -> list[dict[str, str]]:
    """Return the pinned packages, rejecting anything other than exact commits."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    packages = data.get("packages")
    if set(data) != {"version", "packages"} or data["version"] != 1 or not isinstance(packages, list) or not packages:
        raise ValueError(f"{path}: expected version = 1 and a nonempty [[packages]] list")
    pins: list[dict[str, str]] = []
    for row in packages:
        if not isinstance(row, dict) or set(row) != _KEYS or not all(isinstance(value, str) for value in row.values()):
            raise ValueError(f"{path}: every package needs exactly the strings {sorted(_KEYS)}")
        name, repository, commit = row["name"], row["repository"], row["commit"]
        if not _NAME.fullmatch(name):
            raise ValueError(f"{path}: invalid package name {name!r}")
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError(f"{path}: {name} needs an owner/name GitHub repository, not {repository!r}")
        if not _COMMIT.fullmatch(commit):
            raise ValueError(f"{path}: {name} needs a full lowercase 40-character commit, not {commit!r}")
        if any(pin["name"] == name for pin in pins):
            raise ValueError(f"{path}: duplicate package {name}")
        if caller and repository.casefold() == caller.casefold():
            raise ValueError(f"{path}: {name} pins the calling repository {caller}")
        pins.append({"name": name, "repository": repository, "commit": commit})
    return pins


def remote_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def fetch(pin: dict[str, str], destination: Path) -> None:
    """Check out exactly the pinned commit, with the same line endings on every host."""
    if destination.exists():
        # Git marks its object files read-only, which Windows refuses to delete.
        def writable(function, target, _error):
            os.chmod(target, stat.S_IWRITE)
            function(target)

        shutil.rmtree(destination, onexc=writable)
    destination.mkdir(parents=True)
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for arguments in (
        ["init", "--quiet"],
        ["fetch", "--quiet", "--depth", "1", remote_url(pin["repository"]), pin["commit"]],
        ["-c", "core.autocrlf=false", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
    ):
        result = subprocess.run(
            ["git", "-C", str(destination), *arguments], env=environment, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise ValueError(
                f"Cannot fetch {pin['name']} at {pin['commit']} from {pin['repository']}: "
                f"{result.stderr.strip() or result.stdout.strip()}. "
                "The repository must be public and the commit must be pushed."
            )
    head = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    if head != pin["commit"]:
        raise ValueError(f"{pin['name']}: fetched {head}, not the pinned {pin['commit']}")


def export(pins: list[dict[str, str]], checkouts: Path, output: Path) -> list[dict[str, str]]:
    """Export every pinned recipe and record what the lane will resolve."""
    conan = shutil.which("conan")
    if conan is None:
        raise ValueError("Conan is not on PATH; run the export inside the project's environment")
    evidence = []
    for pin in pins:
        checkout = checkouts / pin["name"]
        fetch(pin, checkout)
        if not (checkout / "conanfile.py").is_file():
            raise ValueError(f"{pin['repository']} at {pin['commit']} has no conanfile.py")
        result = subprocess.run(
            [conan, "export", str(checkout), "--no-remote", "--format", "json"], capture_output=True, text=True,
        )
        print(result.stderr, end="", flush=True)
        if result.returncode != 0:
            raise ValueError(f"conan export failed for {pin['name']} with exit code {result.returncode}")
        reference = json.loads(result.stdout)["reference"]
        if reference.split("/", 1)[0] != pin["name"]:
            raise ValueError(f"{pin['repository']} exports {reference}, not a package named {pin['name']}")
        print(f"Exported {reference} from {pin['repository']}@{pin['commit']}", flush=True)
        evidence.append({**pin, "reference": reference})
    output.mkdir(parents=True, exist_ok=True)
    (output / "first-party.json").write_text(
        json.dumps({"schema_version": 1, "packages": evidence}, indent=2) + "\n", encoding="utf-8",
    )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    validate = subcommands.add_parser("validate", help="Check the pins and that every commit can be fetched.")
    validate.add_argument("--repository", default="", help="The calling repository, which may not pin itself.")
    exporter = subcommands.add_parser("export", help="Export every pinned recipe into the active Conan cache.")
    exporter.add_argument("--output", type=Path, required=True)
    for command in (validate, exporter):
        command.add_argument("--pins", type=Path, required=True)
        command.add_argument("--checkouts", type=Path, required=True)
    args = parser.parse_args()
    if not args.pins.is_file():
        print(f"No {args.pins.as_posix()}; this repository pins no first-party packages.")
        return
    if args.action == "export":
        export(load_pins(args.pins), args.checkouts, args.output)
        return
    pins = load_pins(args.pins, args.repository)
    lines = ["### First-party pins", "", "| Package | Repository | Commit |", "| --- | --- | --- |"]
    for pin in pins:
        fetch(pin, args.checkouts / pin["name"])
        lines.append(f"| {pin['name']} | {pin['repository']} | `{pin['commit']}` |")
    summary = "\n".join(lines) + "\n"
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"First-party pins error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
