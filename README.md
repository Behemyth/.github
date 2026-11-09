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
This iteration is fork-only: callers use the `Behemyth/.github` feature branch and the `Behemyth/configuration` identity branch.
Publish both branches before publishing callers that reference them.
Replace these moving branch references with immutable releases before upstream adoption.
See `workflow-templates/native-check.yml` for the starting point.

For local workflow authoring, the optional [GitHub Actions VS Code extension](https://github.com/github/vscode-github-actions) provides validation.
GitHub validates submitted workflows; no separate command-line workflow lint gate is configured.

| Input | Purpose |
| --- | --- |
| `fast-command` | Required cheap checks that gate the aggregate result, not native startup. |
| `fast-install` / `fast-uses-cmake` | Dependency install command and whether the fast job needs the pinned CMake. |
| `native-identities` | Required nonempty JSON array of unique canonical identity names. The shared registry supplies the runner and tool availability. |
| `native-mode` | `cmake` executes the committed multi-config workflow steps and configures every single-config preset; `command` runs the caller's project-wide native command. |
| `native-install` / `native-prepare` / `native-command` | Native Python installation, explicit CMake dependency preparation, and the command-mode test command. |
| `targeted` / `target-identity` | Restrict a manual development check to one identity already in `native-identities`. Give this caller job a different name from the required full check. Targeted checks are rejected for other event types. |
| `configuration-repository` / `configuration-ref` | Public configuration fork and revision containing the canonical identity table. Defaults point to the fork-only feature branch. |
| `cmake-version` | Exact Kitware release, verified against a pinned SHA-256 and exported only as `CMAKE_BINARY`. |
| `llvm-version` | Upstream LLVM major for `llvm-libcxx` lanes, exported as `LLVM_ROOT`. Ubuntu uses apt.llvm.org. macOS uses Homebrew's `llvm@<major>` when it exists, otherwise `llvm`, and fails if the major differs. |
| `python-version` / `fast-runner` | Python for PDM and the fast job runner. |

Preflight renders the canonical identity table, checks registry parity, and verifies that a CMake caller's committed table matches it before selecting a runner.
Each native lane verifies its runner and compiler against the selected identity.
Every native lane gets an isolated `CONAN_HOME`; `llvm-libcxx` lanes receive explicit Clang/libc++ Conan profiles.
Each lane restores its Conan package cache. Only explicit manual runs on the
default branch save it. Pull-request and merge-queue runs cannot seed the shared
cache. No scheduled warming, compiler cache, or dependency service is added.
The workflow never receives publishing credentials, and fork pull requests run without secrets.
Adding a CMake version requires adding its archive checksums for Linux x64 and arm64, macOS, and Windows x64 and arm64.

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
  diagnostics, and reproduction commands;
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

Run the workflow regression suite with CMake 4.4+ and Ninja on `PATH`:

```shell
python -m unittest discover -s tests -v
```
