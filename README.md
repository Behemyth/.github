# .github

Organization metadata

## Native check

`.github/workflows/native-check.yml` is the reusable pull request and merge queue check for native projects.
It runs a cheap `fast` job first, then the full `native` lane matrix only if `fast` succeeds.
The `ci-result` job is the stable aggregate status to require in branch protection.

Callers should trigger on `pull_request` and `merge_group`, use `permissions: contents: read`, and cancel superseded runs only for pull requests.
This iteration is fork-only: callers use the `Behemyth/.github` feature branch and the `Behemyth/configuration` identity branch.
Publish both branches before publishing callers that reference them.
Replace these moving branch references with immutable releases before upstream adoption.
See `workflow-templates/native-check.yml` for the starting point.

For local workflow authoring, the optional [GitHub Actions VS Code extension](https://github.com/github/vscode-github-actions) provides validation.
GitHub validates submitted workflows; no separate command-line workflow lint gate is configured.

| Input | Purpose |
| --- | --- |
| `fast-command` | Required cheap checks that gate the native matrix. |
| `fast-install` / `fast-uses-cmake` | Dependency install command and whether the fast job needs the pinned CMake. |
| `native-identities` | Required nonempty JSON array of unique canonical identity names. The shared registry supplies the runner and tool availability. |
| `native-mode` | `cmake` runs the committed multi-config workflow and representative single-config workflows; `command` runs the caller's project-wide native command. |
| `native-install` / `native-prepare` / `native-command` | Native Python installation, explicit CMake dependency preparation, and the command-mode test command. |
| `targeted` / `target-identity` | Restrict a manual development check to one identity already in `native-identities`. Give this caller job a different name from the required full check. Targeted checks are rejected for other event types. |
| `configuration-repository` / `configuration-ref` | Public configuration fork and revision containing the canonical identity table. Defaults point to the fork-only feature branch. |
| `cmake-version` | Exact Kitware release, verified against a pinned SHA-256 and exported only as `CMAKE_BINARY`. |
| `llvm-version` | Upstream LLVM major for `llvm-libcxx` lanes, exported as `LLVM_ROOT`. Ubuntu uses apt.llvm.org. macOS uses Homebrew's `llvm@<major>` when it exists, otherwise `llvm`, and fails if the major differs. |
| `python-version` / `fast-runner` | Python for PDM and the fast job runner. |

Preflight renders the canonical identity table, checks registry parity, and verifies that a CMake caller's committed table matches it before selecting a runner.
Each native lane verifies its runner and compiler against the selected identity.
Every native lane gets an isolated `CONAN_HOME`; `llvm-libcxx` lanes receive explicit Clang/libc++ Conan profiles.
Each lane restores its Conan package cache. Only `push` runs save it, so callers should also trigger on pushes to their base branch. Pull request and merge queue runs can't seed the shared cache.
The workflow never receives publishing credentials, and fork pull requests run without secrets.
Adding a CMake version requires adding its archive checksums for Linux x64 and arm64, macOS, and Windows x64 and arm64.
