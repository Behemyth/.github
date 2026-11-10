import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


first_party = load("first_party")
native_check = load("native_check")
COMMIT = "0123456789abcdef0123456789abcdef01234567"


class TemporaryDirectory(unittest.TestCase):
    def setUp(self):
        # Git's read-only object files make strict cleanup fail on Windows.
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def pins(self, text):
        path = self.root / "first-party.toml"
        path.write_text(text, encoding="utf-8")
        return path


def package(name="periapsis", repository="Behemyth/periapsis", commit=COMMIT, extra=""):
    return f'[[packages]]\nname = "{name}"\nrepository = "{repository}"\ncommit = "{commit}"\n{extra}'


class PinsTests(TemporaryDirectory):
    def test_valid_pins_keep_their_declared_order(self):
        path = self.pins("version = 1\n" + package() + package("cppaste", "Behemyth/cppaste", "f" * 40))
        pins = first_party.load_pins(path, "Behemyth/portability")
        self.assertEqual([pin["name"] for pin in pins], ["periapsis", "cppaste"])
        self.assertEqual(pins[0], {"name": "periapsis", "repository": "Behemyth/periapsis", "commit": COMMIT})

    def test_only_exact_well_formed_pins_are_accepted(self):
        invalid = {
            "missing version": package(),
            "unknown version": "version = 2\n" + package(),
            "no packages": "version = 1\n",
            "empty packages": "version = 1\npackages = []\n",
            "unknown top-level key": 'version = 1\nremote = "x"\n' + package(),
            "abbreviated commit": "version = 1\n" + package(commit=COMMIT[:7]),
            "uppercase commit": "version = 1\n" + package(commit=COMMIT.upper()),
            "branch instead of commit": "version = 1\n" + package(commit="development"),
            "repository URL": "version = 1\n" + package(repository="https://github.com/Behemyth/periapsis"),
            "repository without owner": "version = 1\n" + package(repository="periapsis"),
            "invalid name": "version = 1\n" + package(name="Periapsis"),
            "duplicate name": "version = 1\n" + package() + package(repository="Behemyth/other"),
            "unknown package key": "version = 1\n" + package(extra='branch = "development"\n'),
            "missing package key": 'version = 1\n[[packages]]\nname = "periapsis"\n',
            "non-string value": 'version = 1\n[[packages]]\nname = "periapsis"\nrepository = "a/b"\ncommit = 1\n',
        }
        for reason, text in invalid.items():
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                first_party.load_pins(self.pins(text))

    def test_a_repository_cannot_pin_itself(self):
        path = self.pins("version = 1\n" + package())
        with self.assertRaisesRegex(ValueError, "calling repository"):
            first_party.load_pins(path, "behemyth/Periapsis")

    def test_a_repository_without_pins_is_not_an_error(self):
        output = io.StringIO()
        arguments = ["first_party.py", "validate", "--pins", str(self.root / "absent.toml"),
                     "--checkouts", str(self.root / "checkouts")]
        with patch.object(sys, "argv", arguments), contextlib.redirect_stdout(output):
            first_party.main()
        self.assertIn("pins no first-party packages", output.getvalue())
        self.assertFalse((self.root / "checkouts").exists())


@unittest.skipUnless(shutil.which("git"), "Git is required")
class RepositoryTests(TemporaryDirectory):
    def setUp(self):
        super().setUp()
        self.remotes = {}
        patcher = patch.object(first_party, "remote_url", lambda repository: self.remotes[repository])
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, repository, *arguments):
        return subprocess.run(
            ["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", *arguments],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, name, files):
        """Commit files to a local stand-in for a GitHub repository and return a pin for it."""
        repository = self.root / "remotes" / name
        if not repository.exists():
            repository.mkdir(parents=True)
            self.git(repository, "init", "--quiet")
            # GitHub serves any reachable commit, not only branch tips.
            self.git(repository, "config", "uploadpack.allowAnySHA1InWant", "true")
            self.remotes[f"owner/{name}"] = repository.as_uri()
        for file, text in files.items():
            (repository / file).write_text(text, encoding="utf-8")
        self.git(repository, "add", "--all")
        self.git(repository, "commit", "--quiet", "--message", "change")
        return {"name": name, "repository": f"owner/{name}", "commit": self.git(repository, "rev-parse", "HEAD")}

    def recipe(self, name, base="conanfile_base"):
        return {
            "conanfile.py": (
                f"from {base} import Base\n\n\nclass Recipe(Base):\n"
                f'    name = "{name}"\n    version = "1.0"\n    exports = "{base}.py"\n'
            ),
            f"{base}.py": (
                "from conan import ConanFile\n\n\nclass Base(ConanFile):\n"
                f'    description = "{name} base"\n'
            ),
        }


class FetchTests(RepositoryTests):
    def test_fetch_checks_out_the_pinned_commit_rather_than_the_branch_tip(self):
        pinned = self.commit("alpha", {"value.txt": "pinned\n"})
        self.commit("alpha", {"value.txt": "later\n"})
        checkout = self.root / "checkouts" / "alpha"
        for _ in range(2):  # A second fetch replaces the first checkout.
            first_party.fetch(pinned, checkout)
            self.assertEqual((checkout / "value.txt").read_bytes(), b"pinned\n")
            self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), pinned["commit"])

    def test_an_unavailable_commit_names_the_package_and_repository(self):
        pin = dict(self.commit("alpha", {"value.txt": "pinned\n"}), commit=COMMIT)
        with self.assertRaisesRegex(ValueError, f"Cannot fetch alpha at {COMMIT} from owner/alpha"):
            first_party.fetch(pin, self.root / "checkouts" / "alpha")


@unittest.skipUnless(shutil.which("conan"), "Conan 2 is required")
class ExportTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        home = self.root / "conan-home"
        (home / "profiles").mkdir(parents=True)
        (home / "profiles" / "default").write_text("[settings]\n", encoding="utf-8")
        environment = patch.dict(os.environ, {"CONAN_HOME": str(home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.output = self.root / "build" / "native-check" / "probe"

    def export(self, pins):
        with contextlib.redirect_stdout(io.StringIO()):
            return first_party.export(pins, self.root / "checkouts", self.output)

    def test_recipes_sharing_a_helper_module_name_resolve_in_one_graph(self):
        pins = [self.commit(name, self.recipe(name)) for name in ("alpha", "beta")]
        evidence = self.export(pins)
        self.assertEqual([item["commit"] for item in evidence], [pin["commit"] for pin in pins])
        for item in evidence:
            self.assertRegex(item["reference"], rf"^{item['name']}/1\.0#[0-9a-f]{{32}}$")
        recorded = json.loads((self.output / "first-party.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded, {"schema_version": 1, "packages": evidence})

        # Both recipes import a module named conanfile_base, and 1.0 must satisfy the projects' open ranges.
        graph = subprocess.run(
            ["conan", "graph", "info", "--requires=alpha/[>=0.0.0.dev1]", "--requires=beta/[>=0.0.0.dev1]",
             "--no-remote", "--format", "json"],
            check=True, capture_output=True, text=True,
        )
        nodes = json.loads(graph.stdout)["graph"]["nodes"].values()
        resolved = {node["name"]: (node["ref"], node["description"]) for node in nodes if node["name"]}
        self.assertEqual(resolved, {
            item["name"]: (item["reference"], f"{item['name']} base") for item in evidence
        })

    def test_a_new_pin_supersedes_the_recipe_already_in_the_cache(self):
        old = self.commit("alpha", self.recipe("alpha"))
        new = self.commit("alpha", {"conanfile_base.py": self.recipe("alpha")["conanfile_base.py"] + "# changed\n"})
        references = [self.export([pin])[0]["reference"] for pin in (old, new, old)]
        self.assertNotEqual(references[0], references[1])
        self.assertEqual(references[0], references[2])
        graph = subprocess.run(
            ["conan", "graph", "info", "--requires=alpha/[>=0.0.0.dev1]", "--no-remote", "--format", "json"],
            check=True, capture_output=True, text=True,
        )
        nodes = json.loads(graph.stdout)["graph"]["nodes"].values()
        self.assertEqual([node["ref"] for node in nodes if node["name"] == "alpha"], [references[2]])

    def test_a_recipe_must_export_the_pinned_package_name(self):
        pin = dict(self.commit("alpha", self.recipe("alpha")), name="beta")
        with self.assertRaisesRegex(ValueError, "not a package named beta"):
            self.export([pin])
        self.assertFalse((self.output / "first-party.json").exists())

    def test_a_pinned_repository_must_contain_a_recipe(self):
        pin = self.commit("alpha", {"README.md": "no recipe\n"})
        with self.assertRaisesRegex(ValueError, "has no conanfile.py"):
            self.export([pin])

    def test_lane_result_records_the_exported_packages(self):
        evidence = self.export([self.commit("alpha", self.recipe("alpha"))])
        with contextlib.redirect_stdout(io.StringIO()):
            native_check.run_stage(self.root, self.output, "native-command", [sys.executable, "-c", "pass"])
            result = native_check.report(self.root, self.output, "probe", "msvc", "success", {})
        self.assertEqual(result["first_party"], evidence)
        summary = (self.output / "summary.md").read_text(encoding="utf-8")
        self.assertIn(f"| alpha | owner/alpha | `{evidence[0]['commit']}` | `{evidence[0]['reference']}` |", summary)


if __name__ == "__main__":
    unittest.main()
