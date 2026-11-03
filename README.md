# gh-workflows

Reusable GitHub Actions workflows and a release helper for Python/[uv](https://docs.astral.sh/uv/) projects.

Reference them as `sowwic/gh-workflows/.github/workflows/<name>.yml@v1`.

| Workflow | Purpose | Main inputs |
|---|---|---|
| `python-tests.yml` | pytest matrix; cheap OSes first, macOS only after they pass and never on draft PRs | `os`, `python-versions`, `macos-python-versions` (`[]` skips macOS), `apt-packages`, `test-command` |
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

## Versioning

Tags `vMAJOR.MINOR.PATCH` plus a moving `vMAJOR` tag. Reusable workflows reference the composite action at `@v1`, so cut `v1` before consuming them (use a branch ref while developing).
