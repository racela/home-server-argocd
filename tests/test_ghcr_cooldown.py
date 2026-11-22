"""Run with python3 -m unittest discover -s tests -p test_ghcr_cooldown.py."""

import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/ghcr-cooldown.py"
SPEC = importlib.util.spec_from_file_location("ghcr_cooldown", SCRIPT)
cooldown = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cooldown)
REPO = "racela/home-server-argocd"


def update(digest="a", line=1):
    return {
        "filename": "apps/config/example/deployment.yaml",
        "status": "modified",
        "patch": (
            f"@@ -{line},1 +{line},1 @@\n"
            "-  image: ghcr.io/example/app:1.2.3\n"
            f"+  image: ghcr.io/example/app:1.2.4@sha256:{digest * 64}"
        ),
    }


def pull_request():
    return {
        "state": "open",
        "base": {"ref": "master"},
        "head": {"sha": "test-head", "repo": {"full_name": REPO}},
        "labels": [{"name": cooldown.AUTOMERGE_LABEL}],
        "user": {"login": cooldown.RENOVATE_BOT},
        "changed_files": 1,
        "draft": False,
        "mergeable_state": "clean",
    }


class CooldownTests(unittest.TestCase):
    def test_same_patch_across_rebases_and_new_digest(self):
        original = cooldown.candidate_fingerprint([update()], set())
        self.assertEqual(original, cooldown.candidate_fingerprint([update(line=18)], set()))
        self.assertNotEqual(original, cooldown.candidate_fingerprint([update(digest="b")], set()))

    def test_immich_chart_revision_and_docker_hub_update(self):
        candidate = {
            "filename": cooldown.IMMICH,
            "status": "modified",
            "patch": "+    targetRevision: 0.13.4",
        }
        self.assertTrue(cooldown.candidate_fingerprint([candidate], set()))
        docker_hub = update()
        docker_hub["patch"] = docker_hub["patch"].replace("ghcr.io/", "")
        self.assertIsNone(cooldown.candidate_fingerprint([docker_hub], set()))

    def test_only_trusted_bot_comments_start_the_clock(self):
        marker = "<!-- ghcr-cooldown: " + "a" * 64 + " -->"
        comments = [
            {"user": {"login": cooldown.ACTIONS_BOT}, "body": marker, "created_at": "2026-11-22T01:00:00Z"},
            {"user": {"login": "other"}, "body": marker, "created_at": "2026-11-01T01:00:00Z"},
        ]
        fingerprint, timestamp = cooldown.latest_observation(comments)
        self.assertEqual(fingerprint, "a" * 64)
        self.assertEqual(timestamp.day, 22)

    def test_clock_holds_new_and_replaced_digests(self):
        for digest, days, state in [
            ("a", 6, "pending"),
            ("a", 8, "success"),
            ("b", 8, "pending"),
        ]:
            with self.subTest(digest=digest, days=days):
                first_seen = datetime.now(timezone.utc) - timedelta(days=days)
                pull = pull_request()
                with (
                    patch.object(
                        cooldown,
                        "github_api",
                        side_effect=[
                            pull,
                            [update(digest=digest)],
                            pull,
                            [],
                            {"created_at": datetime.now(timezone.utc).isoformat()},
                        ],
                    ),
                    patch.object(
                        cooldown,
                        "latest_observation",
                        return_value=(cooldown.candidate_fingerprint([update()], set()), first_seen),
                    ),
                    patch.object(cooldown, "report_status") as report,
                    patch.object(cooldown, "merge_eligible_patch") as merge,
                ):
                    cooldown.check_pull_request(42)
                    self.assertEqual(report.call_args.args[1], state)
                    if state == "pending":
                        merge.assert_not_called()
                    else:
                        merge.assert_called_once_with(42, "test-head")

    def test_merge_is_sha_checked_and_requires_green_ci(self):
        for state in ("clean", "blocked"):
            with self.subTest(state=state):
                pull = pull_request()
                pull["mergeable_state"] = state
                with (
                    patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO}),
                    patch.object(cooldown, "github_api", return_value=pull) as api,
                ):
                    cooldown.merge_eligible_patch(42, "test-head")
                    if state == "clean":
                        api.assert_called_with(
                            "pulls/42/merge",
                            method="PUT",
                            data={"sha": "test-head", "merge_method": "squash"},
                        )
                    else:
                        api.assert_called_once_with("pulls/42")


if __name__ == "__main__":
    unittest.main()
