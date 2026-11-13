#!/usr/bin/env python3
"""Set up the GitHub side of a project: the repository, its branches and its rulesets.

Two subcommands. Stdlib only, needs `git` and an authenticated `gh`.

`init`            New project only. git init, `main` with an initial commit, `develop` at the
                  same commit, a GitHub repository, `develop` as its default branch, then the
                  rulesets. Refuses a folder that has commits or an `origin` remote.
`sync-rulesets`   Rulesets only. Creates or updates the standard rulesets of the repository.
                  It never creates, moves or deletes a branch, never changes the default
                  branch or any other repository setting. Safe for existing repositories.

The rulesets are the standard set of these projects: `main` and `develop` cannot be
deleted or force-pushed, and changes come in through pull requests.
"""

import argparse
import difflib
import json
import pathlib
import subprocess
import sys

ACTIONS_APP_ID = 15368  # GitHub Actions, the app of check runs made by workflows
ADMIN_ROLE_ID = 5  # the "Repository admin" role
DELETION_RULESET = "Deletion"
RELEASE_RULESET = "Main history"
DEVELOP_RULESET = "Develop history"


class SetupError(Exception):
    pass


def run(cmd: list[str], payload: dict | None = None, check: bool = True) -> str:
    """Run a command, optionally feeding it a JSON payload on stdin."""
    result = subprocess.run(
        cmd,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SetupError(f"{' '.join(cmd)} failed:\n{detail}")
    return result.stdout.strip()


def gh_api(path: str, method: str = "GET", payload: dict | None = None):
    """Call the GitHub API through gh. Returns the parsed JSON, None for an empty answer."""
    cmd = ["gh", "api", "-X", method, path]
    if payload is not None:
        cmd += ["--input", "-"]
    out = run(cmd, payload)
    return json.loads(out) if out else None


# Rulesets
def standard_rulesets(
    release_branch: str = "main",
    develop_branch: str = "develop",
    checks: list[dict] | None = None,
) -> list[dict]:
    """The standard rulesets.

    Args:
        release_branch: name of the release branch.
        develop_branch: name of the develop branch.
        checks: required status checks of the develop branch, as
            `{"context": str, "integration_id": int | None}`. None or empty requires none.

    Returns:
        list[dict]: bodies for the rulesets API.
    """
    admins = [
        {"actor_id": ADMIN_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]

    def pull_request(methods: list[str]) -> dict:
        return {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "required_reviewers": [],
                "require_code_owner_review": True,
                "require_last_push_approval": False,
                "required_review_thread_resolution": True,
                "allowed_merge_methods": methods,
            },
        }

    def conditions(*include: str) -> dict:
        return {"ref_name": {"include": list(include), "exclude": []}}

    release_ref, develop_ref = f"refs/heads/{release_branch}", f"refs/heads/{develop_branch}"
    develop_rules = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        pull_request(["merge", "squash", "rebase"]),
    ]
    if checks:
        develop_rules.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": True,
                    "required_status_checks": [
                        {k: v for k, v in check.items() if v is not None}
                        for check in checks
                    ],
                },
            }
        )
    return [
        {
            "name": DELETION_RULESET,
            "target": "branch",
            "enforcement": "active",
            "conditions": conditions(release_ref, develop_ref),
            "bypass_actors": [],
            "rules": [{"type": "deletion"}],
        },
        {
            "name": RELEASE_RULESET,
            "target": "branch",
            "enforcement": "active",
            "conditions": conditions(release_ref),
            "bypass_actors": admins,
            "rules": [
                {"type": "non_fast_forward"},
                {"type": "deletion"},
                pull_request(["merge"]),
            ],
        },
        {
            # ~DEFAULT_BRANCH protects whichever branch is the default one.
            "name": DEVELOP_RULESET,
            "target": "branch",
            "enforcement": "active",
            "conditions": conditions("~DEFAULT_BRANCH"),
            "bypass_actors": admins,
            "rules": develop_rules,
        },
    ]


def normalize(ruleset: dict) -> dict:
    """Reduce a ruleset to what is compared, so API order and defaults don't matter.

    A `required_status_checks` rule with no checks does nothing, so it is dropped.
    """
    rules = {}
    for rule in ruleset.get("rules", []):
        params = rule.get("parameters")
        if rule["type"] == "required_status_checks":
            checks = sorted(
                params.get("required_status_checks", []), key=lambda c: c["context"]
            )
            if not checks:
                continue
            params = {**params, "required_status_checks": checks}
        rules[rule["type"]] = params
    ref_name = ruleset.get("conditions", {}).get("ref_name", {})
    return {
        "name": ruleset["name"],
        "target": ruleset.get("target"),
        "enforcement": ruleset.get("enforcement"),
        "include": sorted(ref_name.get("include", [])),
        "exclude": sorted(ref_name.get("exclude", [])),
        "bypass_actors": sorted(
            ruleset.get("bypass_actors", []), key=lambda a: json.dumps(a, sort_keys=True)
        ),
        "rules": dict(sorted(rules.items())),
    }


def covers(live, wanted) -> bool:
    """Whether `live` holds everything in `wanted`; the API adds defaults of its own."""
    if isinstance(wanted, dict):
        return isinstance(live, dict) and all(
            key in live and covers(live[key], value) for key, value in wanted.items()
        )
    if isinstance(wanted, list):
        return (
            isinstance(live, list)
            and len(live) == len(wanted)
            and all(covers(a, b) for a, b in zip(live, wanted))
        )
    return live == wanted


def describe_diff(live: dict, wanted: dict) -> str:
    return "\n".join(
        difflib.unified_diff(
            json.dumps(live, indent=2, sort_keys=True).splitlines(),
            json.dumps(wanted, indent=2, sort_keys=True).splitlines(),
            "live",
            "standard",
            lineterm="",
        )
    )


def discover_checks(repo: str, branch: str) -> list[dict]:
    """The status checks that run on pull requests into a branch.

    Read from the head commit of the most recent pull request into the branch. The
    latest commit of the branch itself would also list what runs after a merge, such as
    the docs deployment and the release, which a pull request never reports.
    """
    pulls = gh_api(
        f"repos/{repo}/pulls?state=all&base={branch}&sort=updated&direction=desc&per_page=1"
    )
    if not pulls:
        return []
    try:
        data = gh_api(f"repos/{repo}/commits/{pulls[0]['head']['sha']}/check-runs?per_page=100")
    except SetupError:
        return []
    names = {
        run_["name"]: run_["app"]["id"]
        for run_ in data.get("check_runs", [])
        if run_.get("app", {}).get("id") == ACTIONS_APP_ID
    }
    return [{"context": name, "integration_id": app} for name, app in sorted(names.items())]


def existing_checks(live_rulesets: dict[str, dict]) -> list[dict]:
    """The required checks the develop ruleset has now."""
    ruleset = live_rulesets.get(DEVELOP_RULESET)
    if ruleset is None:
        return []
    rule = normalize(ruleset)["rules"].get("required_status_checks")
    return rule["required_status_checks"] if rule else []


def sync_rulesets(
    repo: str,
    release_branch: str = "main",
    develop_branch: str = "develop",
    checks: list[str] | None = None,
    dry_run: bool = False,
    prune: bool = False,
    assume_yes: bool = False,
) -> None:
    """Create or update the standard rulesets. Touches nothing but rulesets.

    Args:
        repo: `owner/name`.
        release_branch: name of the release branch.
        develop_branch: name of the develop branch.
        checks: required check names for develop. None discovers them from the most recent
            pull request into develop, keeping the current ones when there is none.
        dry_run: print what would happen and change nothing.
        prune: delete rulesets that are not standard, after asking.
        assume_yes: do not ask before pruning.
    """
    summaries = gh_api(f"repos/{repo}/rulesets?per_page=100") or []
    live = {s["name"]: gh_api(f"repos/{repo}/rulesets/{s['id']}") for s in summaries}

    default_branch = (gh_api(f"repos/{repo}") or {}).get("default_branch")
    if default_branch and default_branch != develop_branch:
        print(
            f"warning: the default branch of {repo} is '{default_branch}', not "
            f"'{develop_branch}': the {DEVELOP_RULESET} ruleset protects '{default_branch}'. "
            "Nothing is changed about that."
        )

    if checks is not None:
        required = [{"context": c, "integration_id": ACTIONS_APP_ID} for c in checks]
    else:
        required = discover_checks(repo, develop_branch) or existing_checks(live)
    print(
        "required checks of "
        f"'{DEVELOP_RULESET}': {', '.join(c['context'] for c in required) or 'none'}"
    )

    wanted_rulesets = standard_rulesets(release_branch, develop_branch, required)
    for wanted in wanted_rulesets:
        name = wanted["name"]
        current = live.get(name)
        if current is None:
            print(f"==> create '{name}'")
            if not dry_run:
                gh_api(f"repos/{repo}/rulesets", "POST", wanted)
            continue
        if covers(normalize(current), normalize(wanted)):
            print(f"==> '{name}' is unchanged")
            continue
        print(f"==> update '{name}'")
        print(describe_diff(normalize(current), normalize(wanted)))
        if not dry_run:
            gh_api(f"repos/{repo}/rulesets/{current['id']}", "PUT", wanted)

    standard = {w["name"] for w in wanted_rulesets}
    extra = [name for name in live if name not in standard]
    if not extra:
        return
    print(f"other rulesets: {', '.join(extra)}")
    if not prune:
        print("They are kept. Use --prune to delete them.")
        return
    if not dry_run and not assume_yes:
        if input(f"Delete {', '.join(extra)}? [y/N] ").strip().lower() != "y":
            print("Kept.")
            return
    for name in extra:
        print(f"==> delete '{name}'")
        if not dry_run:
            gh_api(f"repos/{repo}/rulesets/{live[name]['id']}", "DELETE")


# New repositories
def init_repo(
    repo: str | None,
    release_branch: str = "main",
    develop_branch: str = "develop",
    private: bool = False,
    message: str = "Initial commit",
    dry_run: bool = False,
) -> None:
    """Create the repository of a freshly generated project. New projects only."""
    problems = []
    if pathlib.Path(".git").exists():
        if run(["git", "rev-parse", "--verify", "-q", "HEAD"], check=False):
            problems.append("it already has commits")
        if run(["git", "remote"], check=False):
            problems.append("it already has a remote")
    if problems:
        raise SetupError(
            "This folder is not a new project: " + " and ".join(problems) + ". "
            "Use sync-rulesets to bring an existing repository's rulesets in line."
        )

    if repo is None:
        owner = gh_api("user")["login"]
        repo = f"{owner}/{pathlib.Path.cwd().name}"
    steps = [
        ("create the main branch", ["git", "init", "-b", release_branch]),
        ("stage the project", ["git", "add", "-A"]),
        ("initial commit", ["git", "commit", "-m", message]),
        ("create the develop branch", ["git", "branch", develop_branch]),
        (
            "create the GitHub repository and push",
            [
                "gh", "repo", "create", repo,
                "--private" if private else "--public",
                "--source", ".", "--remote", "origin", "--push",
            ],
        ),
        ("push the develop branch", ["git", "push", "origin", develop_branch]),
        (
            "make develop the default branch",
            ["gh", "repo", "edit", repo, "--default-branch", develop_branch],
        ),
    ]
    for name, cmd in steps:
        print(f"==> {name}: {' '.join(cmd)}")
        if not dry_run:
            run(cmd)
    if dry_run:
        names = ", ".join(w["name"] for w in standard_rulesets(release_branch, develop_branch))
        print(f"==> rulesets: {names}")
        return
    sync_rulesets(repo, release_branch, develop_branch, checks=[])
    print(
        "Done. Once CI has run on develop, `make sync-rulesets` adds its checks as "
        "required."
    )


def current_repo() -> str:
    """`owner/name` of the repository in the current folder."""
    return run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("init", "new project: repository, main + develop, default branch, rulesets"),
        ("sync-rulesets", "apply the standard rulesets; rulesets only, no branch changes"),
    ):
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--repo", help="owner/name (default: from the folder)")
        command.add_argument("--release-branch", default="main")
        command.add_argument("--develop-branch", default="develop")
        command.add_argument("--dry-run", action="store_true", help="print only")
        if name == "init":
            command.add_argument("--private", action="store_true")
            command.add_argument("--message", default="Initial commit")
        else:
            command.add_argument("--checks", help="comma separated required checks of develop")
            command.add_argument("--prune", action="store_true", help="delete other rulesets")
            command.add_argument("--yes", action="store_true", help="do not ask before pruning")
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            init_repo(
                args.repo,
                args.release_branch,
                args.develop_branch,
                args.private,
                args.message,
                args.dry_run,
            )
        else:
            checks = None
            if args.checks is not None:
                checks = [c.strip() for c in args.checks.split(",") if c.strip()]
            sync_rulesets(
                args.repo or current_repo(),
                args.release_branch,
                args.develop_branch,
                checks,
                args.dry_run,
                args.prune,
                args.yes,
            )
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("Dry run OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
