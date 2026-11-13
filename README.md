# gh-workflows

Reusable GitHub Actions workflows and a release helper for Python/[uv](https://docs.astral.sh/uv/) projects.

Reference them as `sowwic/gh-workflows/.github/workflows/<name>.yml@v1`.

| Workflow | Purpose | Main inputs |
|---|---|---|
| `python-tests.yml` | pytest matrix; cheap OSes first, macOS only after they pass and never on draft PRs | `os`, `python-versions`, `macos-python-versions` (`[]` skips macOS), `apt-packages`, `test-command` |
| `maya-tests.yml` | Run tests in Maya on a self-hosted runner (only for packages that need it) | `runs-on` (JSON labels, default `maya-2026` macOS ARM64), `sync-args`, `test-command` (default `bash ./mayapy_test.sh`) |
| `python-lint.yml` | Lint with a configurable command | `command` (default `uv run ruff check src tests`) |
| `mkdocs-deploy.yml` | `mkdocs gh-deploy` | `command` |
| `release.yml` | On tag push, create a GitHub release with notes from the changelog | `title`, `changelog-path`, `build` (attach `uv build` output) |

The composite action `actions/setup-uv-project` (uv + Python + `uv sync`) is used by all of them and can be used directly.

## Examples

```yaml
# .github/workflows/tests.yml
on:
  pull_request:
  push:
    branches: [main]
jobs:
  tests:
    uses: sowwic/gh-workflows/.github/workflows/python-tests.yml@v1
    with:
      apt-packages: libegl1 libgl1
```

```yaml
# .github/workflows/release.yml
on:
  push:
    tags: ["v*"]
jobs:
  release:
    uses: sowwic/gh-workflows/.github/workflows/release.yml@v1
    permissions:
      contents: write
    with:
      title: my-project
```

The changelog must use `vX.Y.Z` headings followed by an underline (reStructuredText style), as in `CHANGELOG.rst`.

## Releasing a project: `scripts/release.py`

Run from the project root (on the release branch, with the changelog entry and version bump already committed):

```sh
python scripts/release.py 1.2.3 [--release-branch main] [--develop-branch develop] [--title "..."] [--dry-run]
```

It errors if the changelog has no `v1.2.3` entry, you are not on the release branch, the tree is dirty or behind origin, `pyproject.toml`/`VERSION` don't match, the tag exists, or the version isn't higher than the latest tag. Otherwise it tags `v1.2.3`, pushes the tag (which triggers `release.yml`), and back-merges the release branch into the develop branch.

## Repository setup: `scripts/repo_setup.py`

Sets up the GitHub side of a project. Stdlib only, needs `git` and an authenticated `gh` (the `repo` scope).
Two subcommands that do not overlap:

```sh
# New project only: main + develop at one commit, GitHub repo, develop as default branch, rulesets
python scripts/repo_setup.py init [--repo owner/name] [--private] [--release-branch main] [--develop-branch develop] [--dry-run]

# Rulesets only: never creates, moves or deletes a branch, never changes the default branch or any setting
python scripts/repo_setup.py sync-rulesets [--repo owner/name] [--checks "a,b"] [--prune [--yes]] [--dry-run]
```

`init` refuses a folder that already has commits or an `origin` remote. It runs `git init -b main`, makes the
initial commit, creates `develop` at the same commit, creates the repository (public unless `--private`) and pushes
both branches, makes `develop` the default branch and then applies the rulesets.

The standard rulesets (all active):

| Ruleset | Applies to | Rules | Bypass |
| --- | --- | --- | --- |
| `Deletion` | release and develop branch | no deletion | nobody |
| `Main history` | release branch | no deletion, no force push, pull request (0 approvals, code owners, resolved threads), merge commits only | repository admins |
| `Develop history` | the default branch (`~DEFAULT_BRANCH`) | no deletion, no force push, pull request (same, any merge method), required status checks | repository admins |

`sync-rulesets` creates a missing ruleset or updates one **by name**, and is idempotent: a second run reports
`unchanged`. `--dry-run` prints the diff and changes nothing. Rulesets with other names are listed and kept, and
only deleted with `--prune`, after asking (`--yes` skips that). It warns, and changes nothing, when the default
branch is not the develop branch.

The required checks of `Develop history` are `--checks`, else the checks of the most recent pull request into
develop, else the ones the ruleset already has. A new repository has no pull request yet, so it starts without
required checks: run `sync-rulesets` again after the first pull request has run its CI.

Tests: `python -m unittest discover -s tests`.

## Versioning

Tags `vMAJOR.MINOR.PATCH` plus a moving `vMAJOR` tag. Reusable workflows reference the composite action at `@v1`, so cut `v1` before consuming them (use a branch ref while developing).
