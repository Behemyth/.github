import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "native_check.py"
SPEC = importlib.util.spec_from_file_location("native_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
native_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native_check)


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name).resolve()
        self.output = self.source / "build" / "native-check" / "probe"


class NativeCheckTests(TemporaryProject):
    def test_compiler_diagnostics_include_windows_paths_fatal_errors_and_notes(self):
        text = (
            "src/one.cpp:4:2: error: bad template\n"
            "src/two.cpp:8: warning: missing type\n"
            "C:\\project\\src\\three.ixx(16,3): fatal error C1001: compiler error\n"
            "C:/project/src/four.ixx(19): warning C4000: a warning\n"
            "CMake Error: not a compiler diagnostic\n"
            "src/one.cpp:3:1: note: instantiated here\n"
        )
        diagnostics = native_check.diagnostics_from_log(text, self.source, "build")
        self.assertEqual(len(diagnostics), 5)
        self.assertEqual(diagnostics[0]["line"], 4)
        self.assertIsNone(diagnostics[1]["column"])
        self.assertEqual(diagnostics[2]["code"], "C1001")
        self.assertEqual(diagnostics[2]["file"], "C:/project/src/three.ixx")
        sarif = native_check.sarif_report(self.source, "probe", diagnostics)
        self.assertEqual(sarif["version"], "2.1.0")
        results = sarif["runs"][0]["results"]
        self.assertEqual(results[2]["level"], "error")
        location = results[2]["locations"][0]["physicalLocation"]
        self.assertEqual(location["artifactLocation"]["uri"], "file:///C:/project/src/three.ixx")
        self.assertEqual(location["region"], {"startLine": 16, "startColumn": 3})
        self.assertEqual(results[-1]["level"], "note")

    def test_problem_matchers_match_both_compilers(self):
        matchers = json.loads((SCRIPT.parents[1] / "matchers" / "native.json").read_text())["problemMatcher"]
        for matcher, line in zip(matchers, (
            "src/file.cpp:18:4: fatal error: missing header",
            "D:\\work\\src\\file.ixx(22,9): error C1000: missing symbol",
        )):
            pattern = matcher["pattern"][0]
            match = re.match(pattern["regexp"], line)
            self.assertIsNotNone(match)
            self.assertEqual(match.group(pattern["severity"]), "error")
            self.assertGreater(int(match.group(pattern["line"])), 0)

    def test_subprocess_failure_preserves_exit_code_and_log(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = native_check.run_stage(
                self.source, self.output, "native-command",
                [sys.executable, "-c", "print('a failure'); raise SystemExit(7)"],
            )
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["status"], "failure")
        self.assertIn("a failure", (self.output / "native-command.log").read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            report = native_check.report(self.source, self.output, "probe", "msvc", "failure", {})
        self.assertEqual(report["status"], "failure")
        self.assertEqual(report["failed_stage"], "native-command")
        self.assertEqual(report["reproduction"]["environment"], {})

    def test_setup_failure_and_empty_success_do_not_report_success(self):
        with contextlib.redirect_stdout(io.StringIO()):
            report = native_check.report(
                self.source, self.output, "probe", "msvc", "failure",
                {"msvc": {"outcome": "failure"}, "cmake": {"outcome": "skipped"}},
            )
            self.assertEqual(report["failed_stage"], "msvc")
            self.assertEqual(report["stages"], [])
            with self.assertRaisesRegex(ValueError, "empty lane"):
                native_check.report(self.source, self.output, "probe", "msvc", "success", {})
            cancelled = native_check.report(self.source, self.output, "probe", "msvc", "cancelled", {})
            self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue((self.output / "diagnostics.sarif").exists())

    def test_command_success_schema_and_skipped_execution_are_explicit(self):
        with contextlib.redirect_stdout(io.StringIO()):
            native_check.run_stage(
                self.source, self.output, "native-command", [sys.executable, "-c", "print('passed')"],
            )
            result = native_check.report(self.source, self.output, "probe", "msvc", "success", {})
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["stages"][0]["returncode"], 0)
            self.assertEqual(result["diagnostics"], [])
            stage = self.output / "stage-native-command.json"
            document = json.loads(stage.read_text())
            document["status"] = "skipped"
            stage.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "unsuccessful"):
                native_check.report(self.source, self.output, "probe", "msvc", "success", {})

    def test_dev_drive_output_is_remapped_for_annotations_only(self):
        original = self.source / "original-checkout"
        text = str(self.source / "src" / "file.cpp") + ":4:2: error: a failure"
        console = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(original)}), contextlib.redirect_stdout(console):
            native_check.run_stage(self.source, self.output, "probe", [sys.executable, "-c", f"print({text!r})"])
        self.assertIn(str(original), console.getvalue())
        self.assertIn(str(self.source / "src"), (self.output / "probe.log").read_text())

    def test_explicit_bash_cli_streams_output_and_preserves_pipe_failure(self):
        bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        shell = str(bash) if os.name == "nt" and bash.is_file() else shutil.which("bash")
        if shell is None:
            self.skipTest("Bash is required for command-mode CI")
        environment = dict(
            os.environ, NATIVE_WORKSPACE=str(self.source), NATIVE_IDENTITY="probe",
            NATIVE_CHECK_COMMAND="printf 'from bash\\n'; false | cat",
        )
        command = shlex.join([sys.executable, str(SCRIPT), "run", "--stage", "install"])
        result = subprocess.run(
            [shell, "--noprofile", "--norc", "-c", command + ' --shell "$BASH"'],
            env=environment, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("from bash", result.stdout)
        recorded = json.loads((self.output / "stage-install.json").read_text())
        self.assertEqual(recorded["returncode"], 1)
        self.assertEqual(recorded["status"], "failure")


@unittest.skipUnless(shutil.which("cmake") and shutil.which("ninja"), "CMake 4.4+ and Ninja are required")
class CMakeExecutionTests(TemporaryProject):
    def create_project(self, commands):
        identity = "probe"
        configure = [{
            "name": identity, "generator": "Ninja Multi-Config",
            "binaryDir": "${sourceDir}/build/multi",
            "cacheVariables": {"CMAKE_CONFIGURATION_TYPES": "Debug;Release"},
        }]
        builds, tests, steps = [], [], [{"type": "configure", "name": identity}]
        for configuration in ("Debug", "Release"):
            suffix = configuration.lower()
            name = f"{identity}-multi-{suffix}"
            configure.append({
                "name": f"{identity}-{suffix}", "generator": "Ninja",
                "binaryDir": f"${{sourceDir}}/build/{suffix}",
                "cacheVariables": {"CMAKE_BUILD_TYPE": configuration},
            })
            builds.append({"name": name, "configurePreset": identity, "configuration": configuration})
            tests.append({
                "name": name, "configurePreset": identity, "configuration": configuration,
                "output": {"debug": True, "verbosity": "verbose"},
                "execution": {"stopOnFailure": True},
                "filter": {"exclude": {"label": "benchmark"}},
            })
            steps += [{"type": "build", "name": name}, {"type": "test", "name": name}]
        document = {
            "version": 10, "configurePresets": configure,
            "buildPresets": builds, "testPresets": tests,
            "workflowPresets": [{"name": identity, "steps": steps}],
        }
        self.presets_text = json.dumps(document, indent=2)
        (self.source / "CMakePresets.json").write_text(self.presets_text)
        (self.source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 4.4)\nproject(probe LANGUAGES NONE)\nenable_testing()\n" + commands
        )

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = native_check.run_cmake(
                self.source, self.output, "probe", ["debug", "release"], Path(shutil.which("cmake")),
            )
        self.assertEqual((self.source / "CMakePresets.json").read_text(), self.presets_text)
        self.assertFalse((self.source / "CMakeUserPresets.json").exists())
        return result

    def test_success_only_builds_multi_config_and_preserves_filters(self):
        self.create_project(
            'add_custom_target(probe ALL COMMAND "${CMAKE_COMMAND}" -E touch built-$<CONFIG>.txt)\n'
            'add_test(NAME good COMMAND "${CMAKE_COMMAND}" -E true)\n'
            'add_test(NAME ignored COMMAND "${CMAKE_COMMAND}" -E false)\n'
            'set_tests_properties(ignored PROPERTIES LABELS benchmark)\n'
        )
        self.assertEqual(self.execute(), 0)
        for configuration in ("Debug", "Release"):
            self.assertTrue((self.source / "build" / "multi" / f"built-{configuration}.txt").exists())
            single = self.source / "build" / configuration.lower()
            self.assertTrue((single / "CMakeCache.txt").exists())
            self.assertFalse((single / f"built-{configuration}.txt").exists())
        for junit in self.output.glob("junit-*.xml"):
            tree = ET.parse(junit)
            self.assertEqual(len(tree.findall(".//testcase")), 1)
            self.assertEqual(tree.find(".//testcase").get("name"), "good")
        self.assertEqual(len(list(self.output.glob("junit-*.xml"))), 2)

    def test_test_failures_collect_every_test_and_continue_next_configuration(self):
        self.create_project(
            'add_test(NAME one COMMAND "${CMAKE_COMMAND}" -E false)\n'
            'add_test(NAME two COMMAND "${CMAKE_COMMAND}" -E false)\n'
        )
        self.assertEqual(self.execute(), 1)
        for junit in self.output.glob("junit-*.xml"):
            tree = ET.parse(junit)
            self.assertEqual(len(tree.findall(".//testcase")), 2)
            self.assertEqual(len(tree.findall(".//failure")), 2)
        self.assertEqual(len(list(self.output.glob("junit-*.xml"))), 2)
        log = (self.output / "test-probe-multi-debug.log").read_text()
        self.assertNotIn("Test command:", log)

    def test_build_failures_collect_independent_targets_and_next_configuration(self):
        self.create_project(
            'add_custom_target(one ALL COMMAND "${CMAKE_COMMAND}" -E echo "src/one.cpp:4:2: error: one"\n'
            '  COMMAND "${CMAKE_COMMAND}" -E false)\n'
            'add_custom_target(two ALL COMMAND "${CMAKE_COMMAND}" -E echo "src/two.cpp:5:3: error: two"\n'
            '  COMMAND "${CMAKE_COMMAND}" -E false)\n'
            'add_test(NAME unused COMMAND "${CMAKE_COMMAND}" -E true)\n'
        )
        self.assertEqual(self.execute(), 1)
        for configuration in ("debug", "release"):
            log = (self.output / f"build-probe-multi-{configuration}.log").read_text()
            self.assertIn("error: one", log)
            self.assertIn("error: two", log)
            test = json.loads((self.output / f"stage-test-probe-multi-{configuration}.json").read_text())
            self.assertEqual(test["status"], "skipped")
        with contextlib.redirect_stdout(io.StringIO()):
            result = native_check.report(self.source, self.output, "probe", "llvm-libcxx", "failure", {})
        self.assertEqual(len(result["diagnostics"]), 4)
        self.assertEqual(result["failed_stage"], "build-probe-multi-debug")

    def test_multi_config_failure_does_not_skip_single_config_checks(self):
        self.create_project(
            'if(CMAKE_GENERATOR STREQUAL "Ninja Multi-Config")\n'
            '  message(FATAL_ERROR "multi-config probe failure")\nendif()\n'
            'add_test(NAME unused COMMAND "${CMAKE_COMMAND}" -E true)\n'
        )
        self.assertEqual(self.execute(), 1)
        for configuration in ("debug", "release"):
            result = json.loads((self.output / f"stage-configure-probe-{configuration}.json").read_text())
            self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
