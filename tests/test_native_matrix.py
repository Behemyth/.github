import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "native_matrix.py"
SPEC = importlib.util.spec_from_file_location("native_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
native_matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native_matrix)


def canonical_table():
    rows = []
    for name in native_matrix.REGISTRY:
        operating_system, architecture, compiler = name.split("-", 3)[:3]
        configurations = ["Debug"] if name.endswith("-sanitize") else ["Debug", "Development", "Release"]
        rows.append(
            {
                "name": name,
                "os": operating_system,
                "arch": architecture,
                "compiler": compiler,
                "standard-library": "msvc" if compiler == "msvc" else "libc++",
                "variant": "sanitize" if name.endswith("-sanitize") else "",
                "configurations": configurations,
                "dependency-build-types": {
                    configuration: "Debug" if configuration == "Debug" else "Release"
                    for configuration in configurations
                },
            }
        )
    return native_matrix._identity_table({"version": 1, "identities": rows}, "test")


class IdentityMatrixTests(unittest.TestCase):
    def setUp(self):
        self.canonical = canonical_table()
        self.selection = list(self.canonical)

    def test_resolves_all_identities_and_single_config_coverage(self):
        result = native_matrix.resolve_matrix(self.canonical, self.selection, consumer=self.canonical)
        self.assertEqual([lane["identity"] for lane in result["include"]], self.selection)
        sanitizer = next(lane for lane in result["include"] if lane["identity"].endswith("-sanitize"))
        self.assertEqual(sanitizer["configurations"], ["debug"])
        self.assertTrue(all("single" not in lane for lane in result["include"]))

    def test_python_caller_can_use_canonical_table_without_consumer_manifest(self):
        result = native_matrix.resolve_matrix(self.canonical, self.selection, mode="command")
        self.assertEqual(len(result["include"]), 6)

    def test_cmake_caller_must_commit_matching_manifest(self):
        with self.assertRaisesRegex(ValueError, "must commit"):
            native_matrix.resolve_matrix(self.canonical, self.selection)
        changed = {name: dict(row) for name, row in self.canonical.items()}
        changed[self.selection[0]]["arch"] = "arm64"
        with self.assertRaisesRegex(ValueError, "differs"):
            native_matrix.resolve_matrix(self.canonical, self.selection, consumer=changed)

    def test_registry_must_match_canonical_table(self):
        missing = dict(self.canonical)
        del missing[self.selection[0]]
        with self.assertRaisesRegex(ValueError, "registry differs"):
            native_matrix.resolve_matrix(missing, self.selection, mode="command")

    def test_selection_rejects_empty_duplicate_unknown_and_non_array_values(self):
        for invalid in ([], self.selection[:1] * 2, ["unknown"], "windows-x64-msvc", [1]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                native_matrix.resolve_matrix(self.canonical, invalid, mode="command")

    def test_manual_target_runs_exactly_one_declared_identity(self):
        target = self.selection[2]
        result = native_matrix.resolve_matrix(
            self.canonical, self.selection, target=target, targeted=True, mode="command"
        )
        self.assertEqual([lane["identity"] for lane in result["include"]], [target])
        with self.assertRaisesRegex(ValueError, "not in the caller"):
            native_matrix.resolve_matrix(
                self.canonical, self.selection[:1], target=target, targeted=True, mode="command"
            )
        with self.assertRaisesRegex(ValueError, "nonempty target"):
            native_matrix.resolve_matrix(self.canonical, self.selection, targeted=True, mode="command")
        with self.assertRaisesRegex(ValueError, "full check"):
            native_matrix.resolve_matrix(self.canonical, self.selection, target=target, mode="command")

    def test_invalid_mode_and_duplicate_manifest_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown native mode"):
            native_matrix.resolve_matrix(self.canonical, self.selection, mode="other")
        row = next(iter(self.canonical.values()))
        with self.assertRaisesRegex(ValueError, "duplicate identity"):
            native_matrix._identity_table({"version": 1, "identities": [row, row]}, "test")


class RunnerVerificationTests(unittest.TestCase):
    @patch.object(native_matrix.shutil, "which", return_value="/llvm/bin/clang")
    @patch.object(native_matrix.platform, "machine", return_value="x86_64")
    @patch.object(native_matrix.platform, "system", return_value="Linux")
    @patch.object(native_matrix.subprocess, "run")
    def test_clang_identity_is_checked(self, run, _system, _machine, _which):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout="23.0.0\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="x86_64-unknown-linux-gnu\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="clang version 23.0.0\n", stderr=""),
        ]
        native_matrix.verify_runner("linux-x64-clang-libcxx", "linux", "x64", "clang", "23")

    @patch.object(native_matrix.shutil, "which", return_value="/llvm/bin/clang")
    @patch.object(native_matrix.platform, "machine", return_value="arm64")
    @patch.object(native_matrix.platform, "system", return_value="Darwin")
    @patch.object(native_matrix.subprocess, "run")
    def test_apple_clang_is_rejected(self, run, _system, _machine, _which):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout="23.0.0\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="arm64-apple-darwin\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="Apple clang version 23.0.0\n", stderr=""),
        ]
        with self.assertRaisesRegex(ValueError, "upstream Clang"):
            native_matrix.verify_runner("macos-arm64-clang-libcxx", "macos", "arm64", "clang", "23")

    @patch.object(native_matrix.shutil, "which", return_value="cl.exe")
    @patch.object(native_matrix.platform, "machine", return_value="AMD64")
    @patch.object(native_matrix.platform, "system", return_value="Windows")
    @patch.object(native_matrix.subprocess, "run")
    def test_msvc_target_is_checked(self, run, _system, _machine, _which):
        run.return_value = SimpleNamespace(
            returncode=2, stdout="Microsoft C/C++ Compiler Version 19.50.12345 for x64\n", stderr=""
        )
        native_matrix.verify_runner("windows-x64-msvc", "windows", "x64", "msvc", "23")
        run.return_value.stdout = "Microsoft C/C++ Compiler Version 19.50.12345 for ARM64\n"
        with self.assertRaisesRegex(ValueError, "targets"):
            native_matrix.verify_runner("windows-x64-msvc", "windows", "x64", "msvc", "23")


class WorkflowEnvironmentTests(unittest.TestCase):
    def test_preflight_is_parallel_but_all_stages_gate_result(self):
        workflow = (SCRIPT.parents[1] / "workflows" / "native-check.yml").read_text(encoding="utf-8")
        preflight = workflow.split("\n  preflight:\n", 1)[1].split("\n  native:\n", 1)[0]
        native = workflow.split("\n  native:\n", 1)[1].split("\n  ci-result:\n", 1)[0]
        aggregate = workflow.split("\n  ci-result:\n", 1)[1]
        self.assertNotIn("needs:", preflight)
        self.assertIn("needs: preflight", native)
        self.assertIn("fail-fast: false", native)
        self.assertIn("needs: [fast, preflight, native]", aggregate)
        self.assertNotIn("matrix.single", native)
        self.assertIn("--configurations $CONFIGURATIONS", native)
        self.assertNotIn("schedule:", workflow)
        template = (SCRIPT.parents[2] / "workflow-templates" / "native-check.yml").read_text(encoding="utf-8")
        self.assertNotIn("\n  push:", template)
        self.assertNotIn("\n  schedule:", template)

    def test_dev_drive_workspace_is_used_by_commands_and_package_cache(self):
        workflow = (SCRIPT.parents[1] / "workflows" / "native-check.yml").read_text(encoding="utf-8")
        native = workflow.split("\n  native:\n", 1)[1].split("\n  ci-result:\n", 1)[0]
        self.assertIn("workspace-copy: true", native)
        self.assertIn("working-directory: ${{ env.NATIVE_WORKSPACE }}", native)
        self.assertIn('NATIVE_WORKSPACE=$env:DEV_DRIVE_WORKSPACE', native)
        self.assertIn("path: ${{ env.CONAN_HOME }}/p", native)
        self.assertNotIn("path: ${{ runner.temp }}/conan-home/p", native)
        self.assertIn("Get-Volume -FilePath $env:DEV_DRIVE_WORKSPACE", native)

    def test_project_identity_is_scoped_to_cmake_preparation(self):
        workflow = (SCRIPT.parents[1] / "workflows" / "native-check.yml").read_text(encoding="utf-8")
        native_job = workflow.split("\n  native:\n", 1)[1].split("\n  ci-result:\n", 1)[0]
        job_header = native_job.split("\n    steps:\n", 1)[0]
        preparation = native_job.split("      - name: Prepare native dependencies\n", 1)[1].split(
            "\n      - name:", 1
        )[0]
        self.assertIn("NATIVE_IDENTITY: ${{ matrix.identity }}", job_header)
        self.assertNotIn("CPPYTHON_IDENTITY:", job_header)
        self.assertIn("if: inputs.native-mode == 'cmake'", preparation)
        self.assertIn("CPPYTHON_IDENTITY: ${{ matrix.identity }}", preparation)
        self.assertEqual(native_job.count("CPPYTHON_IDENTITY:"), 1)


if __name__ == "__main__":
    unittest.main()
