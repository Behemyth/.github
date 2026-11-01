# .github

Organization metadata

## Native check

`.github/workflows/native-check.yml` is the reusable pull request and merge queue check for native projects.
It runs a cheap `fast` job first, then the full `native` lane matrix only if `fast` succeeds.
The `ci-result` job is the stable aggregate status to require in branch protection.

Callers should trigger on `pull_request` and `merge_group`, use `permissions: contents: read`, and cancel superseded runs only for pull requests.
Callers pin an immutable release tag such as `@v1.0.0`, not a moving branch. Publish workflow changes to this repository and tag them before pushing callers that reference the new tag.
See `workflow-templates/native-check.yml` for the starting point.

For local workflow authoring, the optional [GitHub Actions VS Code extension](https://github.com/github/vscode-github-actions) provides validation.
GitHub validates submitted workflows; no separate command-line workflow lint gate is configured.

| Input | Purpose |
| --- | --- |
| `fast-command` | Required cheap checks that gate the native matrix. |
| `fast-install` / `fast-uses-cmake` | Dependency install command and whether the fast job needs the pinned CMake. |
| `native-lanes` | Required JSON array of `{name, runner, toolchain, command, install?}`. `toolchain` is `msvc` or `llvm-libcxx`. Windows and Linux runners can be x64 or arm64. macOS runners must be arm64. |
| `cmake-version` | Exact Kitware release, verified against a pinned SHA-256 and exported only as `CMAKE_BINARY`. |
| `llvm-version` | Upstream LLVM major for `llvm-libcxx` lanes, exported as `LLVM_ROOT`. Ubuntu uses apt.llvm.org. macOS uses Homebrew's `llvm@<major>` when it exists, otherwise `llvm`, and fails if the major differs. |
| `python-version` / `fast-runner` | Python for PDM and the fast job runner. |

Every native lane gets an isolated `CONAN_HOME`; `llvm-libcxx` lanes receive explicit Clang/libc++ Conan profiles.
Each lane restores its Conan package cache. Only `push` runs save it, so callers should also trigger on pushes to their base branch. Pull request and merge queue runs can't seed the shared cache.
The workflow never receives publishing credentials, and fork pull requests run without secrets.
Adding a CMake version requires adding its archive checksums for Linux x64 and arm64, macOS, and Windows x64 and arm64.
