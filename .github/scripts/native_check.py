"""Execute committed native presets and publish CI diagnostics without editing them."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CLANG = re.compile(r'^([^"\r\n<>|]+?):(\d+):(?:(\d+):)?\s*(?:fatal )?(error|warning|note):\s*(.*)$')
MSVC = re.compile(r"^(.*?)\((\d+)(?:,(\d+))?\)\s*:\s*(?:fatal )?(error|warning|note)\s+([A-Z]+\d+):\s*(.*)$")


def load_presets(source: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Read literal include files; leave preset inheritance and expansion to CMake."""
    presets = {kind: {} for kind in ("configurePresets", "buildPresets", "testPresets", "workflowPresets")}
    loaded, visiting = set(), set()

    def load(path: Path) -> None:
        path = path.resolve()
        if path in visiting:
            raise ValueError(f"Cyclic preset include: {path}")
        if path in loaded:
            return
        visiting.add(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        for include in document.get("include", []):
            if not isinstance(include, str) or "$" in include:
                raise ValueError(f"Native CI requires literal preset includes: {include!r} in {path}")
            load(path.parent / include)
        for kind, entries in presets.items():
            for preset in document.get(kind, []):
                name = preset["name"]
                if name in entries:
                    raise ValueError(f"Duplicate {kind} name: {name}")
                entries[name] = preset
        visiting.remove(path)
        loaded.add(path)

    load(source / "CMakePresets.json")
    return presets


def save_stage(
    output: Path, stage: str, command: list[str], status: str, *, started: float,
    returncode: int | None = None, reason: str | None = None, reproduction: list[str] | None = None,
) -> dict[str, Any]:
    result = {
        "stage": stage,
        "command": command,
        "reproduction": reproduction or command,
        "status": status,
        "returncode": returncode,
        "started_at": started,
        "duration_seconds": round(time.time() - started, 3),
        "log": f"{stage}.log" if status != "skipped" else None,
    }
    if reason is not None:
        result["reason"] = reason
    (output / f"stage-{stage}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def run_stage(
    source: Path, output: Path, stage: str, command: list[str], reproduction: list[str] | None = None,
) -> dict[str, Any]:
    """Stream combined output, retain the log, and preserve a failing exit status."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", stage):
        raise ValueError(f"Invalid stage name: {stage!r}")
    output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    print(f"::group::{stage}: {shlex.join(command)}", flush=True)
    with (output / f"{stage}.log").open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                command, cwd=source, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as error:
            message = f"Cannot execute {command[0]}: {error}\n"
            log.write(message)
            print(message, end="", file=sys.stderr)
            returncode = 127
        else:
            assert process.stdout is not None
            with process.stdout:
                for line in process.stdout:
                    log.write(line)
                    # Matchers require files in the original checkout, not its Dev Drive copy.
                    displayed = line
                    workspace = os.environ.get("GITHUB_WORKSPACE")
                    if workspace and Path(workspace).resolve() != source:
                        for root in {str(source), source.as_posix()}:
                            displayed = re.sub(
                                re.escape(root), lambda _: workspace, displayed,
                                flags=re.IGNORECASE if os.name == "nt" else 0,
                            )
                    print(displayed, end="", flush=True)
            returncode = process.wait()
    print("::endgroup::", flush=True)
    return save_stage(
        output, stage, command, "success" if returncode == 0 else "failure",
        started=started, returncode=returncode, reproduction=reproduction,
    )


def run_cmake(source: Path, output: Path, identity: str, configurations: list[str], cmake: Path) -> int:
    """Execute the project's workflow steps, continuing independent configurations."""
    presets = load_presets(source)
    workflow = presets["workflowPresets"].get(identity)
    if workflow is None:
        raise ValueError(f"No committed workflow preset for {identity}")
    steps = workflow["steps"]
    if not steps or steps[0] != {"type": "configure", "name": identity}:
        raise ValueError(f"Workflow {identity} must start by configuring its identity")
    if any(step["type"] not in {"build", "test"} for step in steps[1:]):
        raise ValueError(f"Workflow {identity} must contain configure followed by build/test steps")
    singles = [f"{identity}-{configuration}" for configuration in configurations]
    for name in singles:
        if name not in presets["configurePresets"]:
            raise ValueError(f"No committed single-config preset: {name}")
    ctest = cmake.with_name("ctest" + cmake.suffix)
    if not cmake.is_file() or not ctest.is_file():
        raise ValueError(f"The pinned CMake and CTest executables must exist: {cmake}, {ctest}")

    output.mkdir(parents=True, exist_ok=True)
    # CMake 4.4's --presets-file keeps CI policy out of developer and committed presets.
    tests = []
    for step in steps:
        if step["type"] == "test":
            name = step["name"]
            tests.append({
                "name": f"{name}-ci",
                "inherits": name,
                "output": {
                    "debug": False, "verbosity": "default",
                    "shortProgress": False, "outputOnFailure": True,
                    "outputJUnitFile": (output / f"junit-{name}.xml").as_posix(),
                },
                "execution": {"stopOnFailure": False, "noTestsAction": "error"},
            })
    overlay = output / "CIPresets.json"
    overlay.write_text(json.dumps({
        "version": 10, "include": [(source / "CMakePresets.json").as_posix()],
        "testPresets": tests,
    }, indent=2) + "\n", encoding="utf-8")

    results = []
    configured, built = False, False
    for step in steps:
        kind, name = step["type"], step["name"]
        stage = "configure-multi" if kind == "configure" else f"{kind}-{name}"
        if kind != "configure" and (not configured or (kind == "test" and not built)):
            results.append(save_stage(
                output, stage, [], "skipped", started=time.time(),
                reason="The prerequisite configure or build failed.",
            ))
            continue
        if kind == "configure":
            command = [str(cmake), "--preset", name]
            reproduction = ["cmake", "--preset", name]
        elif kind == "build":
            command = [str(cmake), "--build", "--preset", name, "--", "-k", "0"]
            reproduction = ["cmake", "--build", "--preset", name, "--", "-k", "0"]
        else:
            command = [str(ctest), "--presets-file", str(overlay), "--preset", f"{name}-ci"]
            reproduction = ["ctest", "--preset", name]
        result = run_stage(source, output, stage, command, reproduction)
        results.append(result)
        if kind == "configure":
            configured = result["status"] == "success"
        elif kind == "build":
            built = result["status"] == "success"
    # These are independent configure checks, including identities previously omitted.
    for name in singles:
        results.append(run_stage(
            source, output, f"configure-{name}", [str(cmake), "--preset", name],
            ["cmake", "--preset", name],
        ))
    return 0 if all(result["status"] == "success" for result in results) else 1


def diagnostics_from_log(text: str, source: Path, stage: str) -> list[dict[str, Any]]:
    diagnostics = []
    seen = set()
    for line in ANSI.sub("", text).splitlines():
        match = MSVC.match(line)
        if match:
            file, row, column, severity, code, message = match.groups()
        else:
            match = CLANG.match(line)
            if match is None:
                continue
            file, row, column, severity, message = match.groups()
            code = f"compiler-{severity}"
        if int(row) < 1 or "&&" in file:
            continue
        file = file.strip().replace("\\", "/")
        root = source.as_posix().rstrip("/") + "/"
        if file.casefold().startswith(root.casefold()):
            file = file[len(root):]
        diagnostic = {
            "file": file, "line": int(row), "column": int(column) if column and int(column) > 0 else None,
            "severity": severity, "code": code, "message": message, "stage": stage,
        }
        key = tuple(diagnostic.values())
        if key not in seen:
            diagnostics.append(diagnostic)
            seen.add(key)
    return diagnostics


def sarif_report(source: Path, identity: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for diagnostic in diagnostics:
        file = diagnostic["file"]
        absolute = file.startswith("/") or bool(re.match(r"^[A-Za-z]:/", file))
        location = {"uri": quote(("file://" if file.startswith("/") else "file:///") + file, safe="/:")
                    if absolute else quote(file, safe="/"),
                    **({} if absolute else {"uriBaseId": "%SRCROOT%"})}
        region = {"startLine": diagnostic["line"]}
        if diagnostic["column"] is not None:
            region["startColumn"] = diagnostic["column"]
        results.append({
            "ruleId": diagnostic["code"], "level": diagnostic["severity"],
            "message": {"text": diagnostic["message"]},
            "locations": [{"physicalLocation": {"artifactLocation": location, "region": region}}],
            "properties": {"stage": diagnostic["stage"]},
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Synodic native check"}},
            "automationDetails": {"id": f"native/{identity}/"},
            "originalUriBaseIds": {"%SRCROOT%": {"uri": source.as_uri() + "/"}},
            "results": results,
        }],
    }


def report(
    source: Path, output: Path, identity: str, toolchain: str, job_status: str,
    step_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Publish diagnostics as artifacts, never as a replacement for job status."""
    output.mkdir(parents=True, exist_ok=True)
    stages = sorted(
        (json.loads(path.read_text(encoding="utf-8")) for path in output.glob("stage-*.json")),
        key=lambda stage: stage["started_at"],
    )
    diagnostics = []
    for stage in stages:
        if stage["log"]:
            diagnostics.extend(diagnostics_from_log(
                (output / stage["log"]).read_text(encoding="utf-8"), source, stage["stage"],
            ))
    outcomes = {name: result["outcome"] for name, result in step_results.items()}
    failed = next((stage["stage"] for stage in stages if stage["status"] == "failure"), None)
    failed = failed or next((name for name, outcome in outcomes.items() if outcome == "failure"), None)
    complete = any(stage["stage"] == "native-command" or stage["stage"].startswith("test-") for stage in stages)
    status = "success" if (
        job_status == "success" and complete and not failed
        and all(stage["status"] == "success" for stage in stages)
    ) else "failure"
    if job_status == "cancelled":
        status = "cancelled"
    cmake_lane = any(stage["stage"] == "configure-multi" for stage in stages)
    exported = output / "first-party.json"
    first_party = json.loads(exported.read_text(encoding="utf-8"))["packages"] if exported.is_file() else []
    result = {
        "schema_version": 1, "identity": identity, "toolchain": toolchain,
        "revision": os.environ.get("GITHUB_SHA"), "status": status,
        "job_status_at_report": job_status, "failed_stage": failed,
        "workflow_steps": outcomes, "stages": stages, "diagnostics": diagnostics,
        "first_party": first_party,
        "reproduction": {
            "environment": {"CPPYTHON_IDENTITY": identity} if cmake_lane else {},
            "commands": (["pdm run prepare", f"cmake --workflow --preset {identity}"]
                         if cmake_lane else [
                             shlex.join(stage["reproduction"]) for stage in stages
                             if stage["stage"] == "native-command"
                         ]),
        },
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (output / "diagnostics.sarif").write_text(
        json.dumps(sarif_report(source, identity, diagnostics), indent=2) + "\n", encoding="utf-8",
    )
    lines = [f"### Native result: {identity}", f"- Status: **{status}**", f"- Toolchain: `{toolchain}`"]
    if failed:
        lines.append(f"- Failed stage: `{failed}`")
    lines += ["", "| Stage | Result | Seconds |", "| --- | --- | ---: |"]
    lines += [f"| {stage['stage']} | {stage['status']} | {stage['duration_seconds']} |" for stage in stages]
    if first_party:
        lines += ["", "First-party packages exported from pinned commits:", "",
                  "| Package | Repository | Commit | Recipe |", "| --- | --- | --- | --- |"]
        lines += [
            f"| {item['name']} | {item['repository']} | `{item['commit']}` | `{item['reference']}` |"
            for item in first_party
        ]
    if diagnostics:
        lines += ["", "First source diagnostics:", "", "```text"]
        lines += [
            f"{item['file']}:{item['line']}: {item['severity']}: {item['message']}".replace("```", "'''")
            for item in diagnostics[:10]
        ]
        lines.append("```")
    failures = [stage for stage in stages if stage["status"] == "failure"]
    if failures:
        lines += ["", "Reproduce after preparing this identity:", "", "```text"]
        lines += [shlex.join(stage["reproduction"]) for stage in failures]
        lines.append("```")
    lines += ["", "Full logs, JUnit, JSON, and SARIF are in the lane artifact.",
              "The required job result remains authoritative.", ""]
    summary = "\n".join(lines)
    (output / "summary.md").write_text(summary, encoding="utf-8")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)
    if job_status == "success" and status != "success":
        raise ValueError("Missing or unsuccessful native execution; refusing to report an empty lane as successful")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    command = subcommands.add_parser("run")
    command.add_argument("--stage", required=True)
    command.add_argument("--shell", required=True, help="The invoking Bash executable, not a PATH lookup.")
    cmake = subcommands.add_parser("cmake")
    cmake.add_argument("--configurations", nargs="+", required=True)
    summary = subcommands.add_parser("report")
    summary.add_argument("--job-status", required=True, choices=("success", "failure", "cancelled"))
    args = parser.parse_args()
    source = Path(os.environ["NATIVE_WORKSPACE"]).resolve()
    output = source / "build" / "native-check" / os.environ["NATIVE_IDENTITY"]
    if args.action == "run":
        result = run_stage(
            source, output, args.stage,
            [args.shell, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", os.environ["NATIVE_CHECK_COMMAND"]],
        )
        return result["returncode"]
    if args.action == "cmake":
        return run_cmake(
            source, output, os.environ["NATIVE_IDENTITY"], args.configurations, Path(os.environ["CMAKE_BINARY"]),
        )
    report(
        source, output, os.environ["NATIVE_IDENTITY"], os.environ["NATIVE_TOOLCHAIN"],
        args.job_status, json.loads(os.environ["NATIVE_STEP_RESULTS"]),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f"Native check failed: {error}", file=sys.stderr)
        sys.exit(1)
