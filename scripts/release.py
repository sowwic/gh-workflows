#!/usr/bin/env python3
"""Validate, tag, push and back-merge a release. Run from the project root.

The GitHub release itself is created by the shared `release.yml` workflow when
the tag is pushed. Stdlib only.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(Exception):
    pass


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def changelog_has(version: str, changelog: pathlib.Path) -> bool:
    if not changelog.is_file():
        raise ReleaseError(f"Changelog not found: {changelog}")
    lines = changelog.read_text().splitlines()
    return any(
        line == f"v{version}" and i + 1 < len(lines) and set(lines[i + 1]) == {"-"}
        for i, line in enumerate(lines)
    )


def validate(args: argparse.Namespace, tag: str, project: dict) -> None:
    version = args.version
    if not SEMVER.match(version):
        raise ReleaseError(f"'{version}' is not a valid x.y.z version")

    if not changelog_has(version, args.changelog):
        raise ReleaseError(f"No entry for v{version} in {args.changelog}")

    branch = git("branch", "--show-current")
    if branch != args.release_branch:
        raise ReleaseError(
            f"On branch '{branch}', releases must be made from '{args.release_branch}'"
        )

    if git("status", "--porcelain"):
        raise ReleaseError("Working tree is not clean; commit or stash your changes")

    git("fetch", "--tags", "origin")
    behind = git("rev-list", "--count", f"HEAD..origin/{branch}")
    if behind != "0":
        raise ReleaseError(f"'{branch}' is {behind} commit(s) behind origin; pull first")

    if project["version"] != version:
        raise ReleaseError(f"pyproject.toml version is {project['version']}, expected {version}")
    version_file = pathlib.Path("VERSION")
    if version_file.is_file() and version_file.read_text().strip() != version:
        raise ReleaseError(f"VERSION file does not contain {version}")

    tags = git("tag", "--list", "v*").splitlines()
    if tag in tags:
        raise ReleaseError(f"Tag {tag} already exists")
    existing = [as_tuple(t[1:]) for t in tags if SEMVER.match(t[1:])]
    if existing and as_tuple(version) <= max(existing):
        latest = ".".join(map(str, max(existing)))
        raise ReleaseError(f"{version} is not higher than the latest release v{latest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="x.y.z (no leading v)")
    parser.add_argument("--release-branch", default="main")
    parser.add_argument("--develop-branch", default="develop")
    parser.add_argument("--changelog", type=pathlib.Path, default=pathlib.Path("CHANGELOG.rst"))
    parser.add_argument("--title", default="", help="Release title (stored as the tag annotation)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the steps only")
    args = parser.parse_args()
    tag = f"v{args.version}"

    try:
        project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]
        validate(args, tag, project)
        title = args.title or f"{project['name']} {tag}"
        steps = [
            ("tag", ["tag", "-a", tag, "-m", title]),
            ("push tag", ["push", "origin", tag]),
            ("checkout develop", ["checkout", args.develop_branch]),
            ("merge", ["merge", "--no-ff", "-m", f"Merge {args.release_branch} into {args.develop_branch} ({tag})", args.release_branch]),
            ("push develop", ["push", "origin", args.develop_branch]),
            ("checkout back", ["checkout", args.release_branch]),
        ]
        for name, cmd in steps:
            print(f"==> {name}: git {' '.join(cmd)}")
            if args.dry_run:
                continue
            try:
                git(*cmd)
            except ReleaseError as exc:
                if name in {"checkout develop", "merge", "push develop"}:
                    raise ReleaseError(
                        f"{exc}\nTag {tag} is already pushed and CI will create the release. "
                        f"Finish the back-merge of {args.release_branch} into "
                        f"{args.develop_branch} manually."
                    ) from exc
                raise
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Released {tag}." if not args.dry_run else "Dry run OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
