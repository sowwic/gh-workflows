"""Tests for scripts/repo_setup.py: `python -m unittest discover -s tests`."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "repo_setup.py"
spec = importlib.util.spec_from_file_location("repo_setup", SCRIPT)
repo_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repo_setup)

# Standard rulesets as the API returns them: other rule order, extra default
# parameters, and an empty required_status_checks rule on main.
STANDARD_LIVE = [
    {
        "id": 1,
        "name": "Deletion",
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"exclude": [], "include": ["refs/heads/main", "refs/heads/develop"]}
        },
        "rules": [{"type": "deletion"}],
        "bypass_actors": [],
        "node_id": "x",
    },
    {
        "id": 2,
        "name": "Main history",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
        "rules": [
            {"type": "non_fast_forward"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [],
                },
            },
            {"type": "deletion"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "required_reviewers": [],
                    "require_code_owner_review": True,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                    "require_extra_approval_for_unattributed_changes": False,
                    "allowed_merge_methods": ["merge"],
                },
            },
        ],
        "bypass_actors": [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        ],
        "current_user_can_bypass": "always",
    },
]


class FakeGitHub:
    """An in-memory GitHub that records every command given to `run`."""

    def __init__(self, rulesets=(), default_branch="develop", check_runs=(), pulls=None):
        self.rulesets = {r["id"]: copy.deepcopy(r) for r in rulesets}
        self.default_branch = default_branch
        self.check_runs = list(check_runs)
        # By default a pull request exists whenever there are check runs to find.
        self.pulls = pulls if pulls is not None else ([{"head": {"sha": "abc"}}] if check_runs else [])
        self.commands = []
        self.next_id = 100

    def __call__(self, cmd, payload=None, check=True):
        self.commands.append(list(cmd))
        if cmd[:2] != ["gh", "api"]:
            return ""
        method, path = cmd[3], cmd[4]
        if path == "user":
            return json.dumps({"login": "me"})
        if path.startswith("repos/") and path.endswith("/rulesets?per_page=100"):
            return json.dumps([{"id": i, "name": r["name"]} for i, r in self.rulesets.items()])
        if "/rulesets/" in path:
            ruleset_id = int(path.rsplit("/", 1)[1])
            if method == "DELETE":
                del self.rulesets[ruleset_id]
                return ""
            if method == "PUT":
                self.rulesets[ruleset_id] = {"id": ruleset_id, **copy.deepcopy(payload)}
                return json.dumps(self.rulesets[ruleset_id])
            return json.dumps(self.rulesets[ruleset_id])
        if path.endswith("/rulesets") and method == "POST":
            self.next_id += 1
            self.rulesets[self.next_id] = {"id": self.next_id, **copy.deepcopy(payload)}
            return json.dumps(self.rulesets[self.next_id])
        if "/pulls?" in path:
            return json.dumps(self.pulls)
        if "check-runs" in path:
            return json.dumps({"check_runs": self.check_runs})
        if path.startswith("repos/"):
            return json.dumps({"default_branch": self.default_branch})
        raise AssertionError(f"unexpected call {cmd}")

    def writes(self):
        return [c for c in self.commands if c[:2] == ["gh", "api"] and c[3] != "GET"]


def sync(github, **kwargs):
    with mock.patch.object(repo_setup, "run", github), contextlib.redirect_stdout(io.StringIO()) as out:
        repo_setup.sync_rulesets("me/proj", **kwargs)
    return out.getvalue()


class SyncRulesetsTests(unittest.TestCase):
    def test_creates_the_three_standard_rulesets_and_then_is_idempotent(self):
        github = FakeGitHub()
        sync(github)
        self.assertEqual(
            sorted(r["name"] for r in github.rulesets.values()),
            ["Deletion", "Develop history", "Main history"],
        )
        github.commands.clear()
        out = sync(github)
        self.assertEqual(github.writes(), [])
        self.assertEqual(out.count("is unchanged"), 3)

    def test_the_api_form_of_the_standard_rulesets_counts_as_unchanged(self):
        github = FakeGitHub(STANDARD_LIVE)
        out = sync(github, checks=[])
        self.assertIn("'Deletion' is unchanged", out)
        self.assertIn("'Main history' is unchanged", out)
        self.assertIn("create 'Develop history'", out)

    def test_only_rulesets_are_touched(self):
        github = FakeGitHub(default_branch="main")
        out = sync(github, prune=True, assume_yes=True)
        self.assertTrue(all(c[:2] == ["gh", "api"] for c in github.commands))
        for command in github.writes():
            self.assertRegex(command[4], r"/rulesets(/\d+)?$")
        self.assertIn("default branch of me/proj is 'main'", out)

    def test_dry_run_changes_nothing(self):
        github = FakeGitHub(STANDARD_LIVE[:1])
        sync(github, dry_run=True)
        self.assertEqual(github.writes(), [])

    def test_changed_ruleset_is_updated_with_a_diff(self):
        github = FakeGitHub()
        sync(github)
        main = next(r for r in github.rulesets.values() if r["name"] == "Main history")
        main["rules"][-1]["parameters"]["allowed_merge_methods"] = ["merge", "squash"]
        github.commands.clear()
        out = sync(github)
        self.assertIn("update 'Main history'", out)
        self.assertIn('-        "squash"', out)
        self.assertEqual(len(github.writes()), 1)

    def test_other_rulesets_are_kept_unless_pruned(self):
        old = {"id": 7, "name": "Protection", "target": "branch", "enforcement": "active"}
        github = FakeGitHub([old])
        out = sync(github)
        self.assertIn("They are kept", out)
        self.assertIn(7, github.rulesets)
        sync(github, prune=True, assume_yes=True)
        self.assertNotIn(7, github.rulesets)

    def test_prune_asks_first(self):
        old = {"id": 7, "name": "Protection", "target": "branch", "enforcement": "active"}
        github = FakeGitHub([old])
        with mock.patch("builtins.input", return_value="n"):
            sync(github, prune=True)
        self.assertIn(7, github.rulesets)

    def test_checks_come_from_the_latest_pull_request_into_develop(self):
        runs = [
            {"name": "tests / unit-tests (ubuntu-latest, 3.14)", "app": {"id": 15368}},
            {"name": "linting / lint", "app": {"id": 15368}},
            {"name": "codecov", "app": {"id": 999}},
        ]
        github = FakeGitHub(check_runs=runs)
        sync(github)
        develop = next(r for r in github.rulesets.values() if r["name"] == "Develop history")
        rule = next(r for r in develop["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(
            [c["context"] for c in rule["parameters"]["required_status_checks"]],
            ["linting / lint", "tests / unit-tests (ubuntu-latest, 3.14)"],
        )

    def test_no_pull_request_means_no_discovered_checks(self):
        github = FakeGitHub(check_runs=[{"name": "release / release", "app": {"id": 15368}}], pulls=[])
        out = sync(github)
        self.assertIn("required checks of 'Develop history': none", out)

    def test_current_checks_are_kept_when_there_are_no_runs(self):
        github = FakeGitHub()
        sync(github, checks=["linting / lint"])
        sync(github)
        develop = next(r for r in github.rulesets.values() if r["name"] == "Develop history")
        self.assertTrue(any(r["type"] == "required_status_checks" for r in develop["rules"]))

    def test_branch_names_are_parameters(self):
        github = FakeGitHub()
        sync(github, release_branch="stable", develop_branch="dev")
        deletion = next(r for r in github.rulesets.values() if r["name"] == "Deletion")
        self.assertEqual(
            deletion["conditions"]["ref_name"]["include"],
            ["refs/heads/stable", "refs/heads/dev"],
        )


class InitRepoTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.folder.name)
        self.addCleanup(os.chdir, self.cwd)

    def dry_run(self, **kwargs):
        github = FakeGitHub()
        with mock.patch.object(repo_setup, "run", github), contextlib.redirect_stdout(io.StringIO()) as out:
            repo_setup.init_repo("me/proj", dry_run=True, **kwargs)
        return out.getvalue(), github

    def test_steps_in_order_and_nothing_is_run(self):
        out, github = self.dry_run()
        order = [
            "git init -b main",
            "git add -A",
            "git commit -m Initial commit",
            "git branch develop",
            "gh repo create me/proj --public",
            "git push origin develop",
            "gh repo edit me/proj --default-branch develop",
            "rulesets: Deletion, Main history, Develop history",
        ]
        positions = [out.index(step) for step in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(github.commands, [])

    def test_private_flag(self):
        out, _ = self.dry_run(private=True)
        self.assertIn("gh repo create me/proj --private", out)

    def test_refuses_a_folder_with_history_or_a_remote(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@example.com:a/b.git"], check=True)
        with self.assertRaises(repo_setup.SetupError) as raised:
            repo_setup.init_repo("me/proj", dry_run=True)
        self.assertIn("already has a remote", str(raised.exception))
        self.assertIn("sync-rulesets", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
