# .github

Organization metadata

## Native check

`.github/workflows/native-check.yml` is the reusable pull request and merge queue check for native projects.
It starts cheap `fast` checks and identity preflight concurrently. The full
native matrix starts when preflight succeeds, without waiting for `fast`.
The `ci-result` job is the stable aggregate status to require in branch protection.
It requires fast checks, preflight, and every native lane to succeed.

Callers trigger on `pull_request` and `merge_group`, with optional
single-identity `workflow_dispatch`, read-only permissions, and cancellation of
superseded runs only for pull requests. Do not add push or scheduled checks.
This iteration is fork-only: callers pin a `Behemyth/.github` commit and read the identity table from `Behemyth/configuration`.
Publish both before publishing callers that reference them.
Replace these fork references with immutable releases before upstream adoption.
CMake projects get their caller from the `Behemyth/configuration` Copier template.
See `workflow-templates/native-check.yml` for a starting point elsewhere.

For local workflow authoring, the optional [GitHub Actions VS Code extension](https://github.com/github/vscode-github-actions) provides validation.
GitHub validates submitted workflows; no separate command-line workflow lint gate is configured.

| Input | Purpose |
| --- | --- |
| `fast-command` | Required cheap checks that gate the aggregate result, not native startup. |
| `fast-install` / `fast-uses-cmake` | Dependency install command and whether the fast job needs the pinned CMake. |
| `native-identities` | JSON array of unique canonical identity names. Leave it empty to run every identity in the canonical table. The shared registry supplies the runner and tool availability. |
| `native-mode` | `cmake` executes the committed multi-config workflow steps and configures every single-config preset; `command` runs the caller's project-wide native command. |
| `native-install` / `native-prepare` / `native-command` | Native Python installation, explicit CMake dependency preparation, and the command-mode test command. |
| `targeted` / `target-identity` | Restrict a manual development check to one selected identity. Give this caller job a different name from the required full check. Targeted checks are rejected for other event types. |
| `configuration-repository` / `configuration-ref` | Public configuration fork and the branch, tag, or commit containing the canonical identity table. With no ref, preflight uses the template revision in the caller's `.copier-answers.yml` and requires that file to name the same repository. |
| `cmake-version` | Exact Kitware release, verified against a pinned SHA-256 and exported only as `CMAKE_BINARY`. |
| `llvm-version` | Upstream LLVM major for `llvm-libcxx` lanes, exported as `LLVM_ROOT`. Ubuntu uses apt.llvm.org. macOS uses Homebrew's `llvm@<major>` when it exists, otherwise `llvm`, and fails if the major differs. |
| `python-version` / `fast-runner` | Python for PDM and the fast job runner. |

Preflight renders the canonical identity table, checks registry parity, and verifies that a CMake caller's committed table matches it before selecting a runner.
Each native lane verifies its runner and compiler against the selected identity.
Every native lane gets an isolated `CONAN_HOME`; `llvm-libcxx` lanes receive explicit Clang/libc++ Conan profiles.
Each lane restores its Conan package cache. Pull-request and manual runs save
it for their own branch or pull request, after removing source, build, and
download folders. A CMake lane saves once its dependencies are prepared, even
if the project's own build or tests then fail, so a fix does not rebuild them.
Merge-queue runs never save. GitHub shows a cache only to the
ref that saved it, except that every ref can read the default branch's caches.
No scheduled warming, compiler cache, or dependency service is added.
The workflow never receives publishing credentials, and fork pull requests run without secrets.
Adding a CMake version requires adding its archive checksums for Linux x64 and arm64, macOS, and Windows x64 and arm64.

### First-party pins

No remote serves Synodic's own Conan packages yet. Until one does, a
repository that depends on them commits `tool/first-party.toml`:

```toml
version = 1

[[packages]]
name = "periapsis"
repository = "Behemyth/periapsis"
commit = "fcc7e81c1aa641a06e9770459d1b0b5ea7ce49b9"
```

- List every first-party package in the dependency graph, including
  transitive ones. A missing package fails preparation with Conan's own
  "package not found" error.
- `commit` must be a full 40-character commit in a public repository whose
  root contains the package's `conanfile.py`. Pin a commit whose own native
  check passed.
- Preflight validates the file and fetches every commit, so a bad pin fails
  before any native lane starts.
- Each lane then exports the pinned recipes into its own Conan cache, before
  preparation. Export builds nothing. Preparation builds what the identity
  needs, as it does for any package without a binary.
- The lane's `result.json` and summary record each package's repository,
  commit, and exported recipe revision.

A repository without this file is unaffected. Delete the file and this step
when a remote serves the packages.

### Host prerequisites

A runner image does not have everything a project needs. Two mechanisms fill
the gap, and a repository that needs neither is unaffected.

- **System packages.** Some Conan recipes stand for packages that the
  operating system provides, such as the X11 libraries. On Linux lanes Conan
  installs them with `sudo`. Windows and macOS lanes do not install system
  packages.
- **Project setup script.** A repository that needs anything else commits
  `tool/native-setup.py`. Each lane runs it with `pdm run python` from the
  project root, after installing the Python environment and before exporting
  pins or preparing dependencies. A failure fails the lane.

The setup script is for continuous integration only. It reads the lane from
`NATIVE_IDENTITY` and passes results to later steps the way any Actions step
does, by appending to the files named in `GITHUB_ENV` and `GITHUB_PATH`.
Verify the checksum of anything it downloads. A developer machine gets the
same prerequisites from the project's Porringer manifest instead.

### Native execution and storage

The CI adapter reads the identity's committed workflow steps and executes the
same configure, build, and test presets. It keeps going through independent
configurations after failures and passes `-k 0` to Ninja. Tests whose build
failed are explicitly skipped; later configurations and all single-config
configure checks still run. Any failure makes the lane fail.

CMake 4.4+ supports `--presets-file`. The pinned CMake uses that feature for a
CI-only test overlay which inherits the project's test filters, disables
stop-on-first-failure and verbose/debug output, and writes JUnit XML. Neither
committed presets nor `CMakeUserPresets.json` are edited. Local workflow
behavior is unchanged. The adapter requires literal preset include paths and
rejects unsupported includes instead of guessing at their expansion.

Windows lanes create a temporary ReFS Dev Drive, copy the checkout to it, and
run commands there with `CONAN_HOME` on the same drive. Only GitHub-hosted CI
disks are affected; no developer drive is formatted. The action is SHA-pinned
and filesystem validation fails the job if the workspace is not on Dev Drive.

### Structured results

Every native lane uploads `native-<identity>-<attempt>` for seven days, including:

- `result.json`: schema version 1, identity, revision, toolchain, status at
  report time, workflow step outcomes, command/exit-code/duration records, source
  diagnostics, exported first-party packages, and reproduction commands;
- `summary.md`: stage timings, the failed stage, and the first ten diagnostics;
- per-stage logs and `junit-*.xml` test reports;
- `diagnostics.sarif`: SARIF 2.1.0 generated from Clang and MSVC source
  diagnostics in the logs, without experimental compiler output flags.

Compiler problem matchers also produce GitHub annotations. Dev Drive paths are
mapped back to the original checkout for annotations; raw logs retain actual
paths. Source-less errors remain visible in the failed command and its log.
SARIF is an artifact, not a code-scanning upload, so check jobs stay read-only.
Reporting does not convert an execution or setup failure into success. The
final job and `ci-result`, including artifact-upload failures after reporting,
remain authoritative.

Run the workflow regression suite with CMake 4.4+, Ninja, and Conan 2 on `PATH`.
Tests that need a missing tool are skipped:

```shell
python -m unittest discover -s tests -v
```
