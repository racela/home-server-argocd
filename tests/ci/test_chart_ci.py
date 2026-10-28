"""Exercise chart CI failure paths without a Docker daemon or cluster."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from yamllint.config import YamlLintConfig
from yamllint import linter


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/preload-chart-images.sh"


class PreloadImagesTest(unittest.TestCase):
    def run_preload(self, *images, **flags):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            # Record Docker/kind calls and simulate registry or import failures.
            stub = '''#!/usr/bin/env python3
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\\n")
if tool == "docker" and args[0] == "pull":
    if os.environ.get("FAIL_ECR") and "ecr" in args[1]:
        sys.exit(1)
    if os.environ.get("FAIL_MIRROR") and args[1].startswith("mirror.gcr.io/"):
        sys.exit(1)
if tool == "kind" and os.environ.get("FAIL_LOAD"):
    sys.exit(1)
'''
            for name in ("docker", "kind"):
                executable = directory / name
                executable.write_text(stub)
                executable.chmod(0o755)
            calls = directory / "calls.jsonl"
            calls.touch()
            env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}",
                   "CALLS": str(calls), **flags}
            result = subprocess.run(
                ["bash", str(SCRIPT), "ci-test", *images], env=env,
                text=True, capture_output=True,
            )
            return result.returncode, [json.loads(line) for line in calls.read_text().splitlines()]

    def test_success_keeps_original_reference(self):
        image = "ecr-public.aws.com/docker/library/redis:8.6.4-alpine"
        status, calls = self.run_preload(image)
        self.assertEqual(status, 0)
        self.assertEqual(calls, [
            ["docker", "pull", image],
            ["kind", "load", "docker-image", "--name", "ci-test", image],
        ])

    def test_registry_failure_uses_mirror_and_preserves_tag(self):
        for registry in ("ecr-public.aws.com", "public.ecr.aws"):
            with self.subTest(registry=registry):
                image = f"{registry}/docker/library/redis:8.6.4-alpine"
                mirror = "mirror.gcr.io/library/redis:8.6.4-alpine"
                status, calls = self.run_preload(image, FAIL_ECR="1")
                self.assertEqual(status, 0)
                self.assertEqual(calls, [
                    ["docker", "pull", image], ["docker", "pull", mirror],
                    ["docker", "tag", mirror, image],
                    ["kind", "load", "docker-image", "--name", "ci-test", image],
                ])

    def test_unrelated_images_are_not_rewritten(self):
        status, calls = self.run_preload("quay.io/argoproj/argocd:v3.5.6", "---")
        self.assertEqual((status, calls), (0, []))

    def test_failed_mirror_stops_before_tagging_or_loading(self):
        status, calls = self.run_preload(
            "ecr-public.aws.com/docker/library/redis:8.6.4-alpine",
            FAIL_ECR="1", FAIL_MIRROR="1",
        )
        self.assertNotEqual(status, 0)
        self.assertEqual([call[:2] for call in calls], [["docker", "pull"], ["docker", "pull"]])

    def test_failed_cluster_import_is_not_hidden(self):
        status, _ = self.run_preload(
            "ecr-public.aws.com/docker/library/redis:8.6.4-alpine", FAIL_LOAD="1",
        )
        self.assertNotEqual(status, 0)

    def test_preloads_both_previous_and_new_versions(self):
        old = "ecr-public.aws.com/docker/library/redis:8.6.4-alpine"
        new = "ecr-public.aws.com/docker/library/redis:8.6.5-alpine"
        status, calls = self.run_preload(old, new)
        self.assertEqual(status, 0)
        self.assertEqual([call[-1] for call in calls if call[0] == "kind"], [old, new])


class UpstreamYamlLintTest(unittest.TestCase):
    config = YamlLintConfig(file=ROOT / ".github/lintconf.yaml")

    def errors(self, source):
        return [problem for problem in linter.run(source, self.config) if problem.level == "error"]

    def test_upstream_whitespace_does_not_block_validation(self):
        self.assertEqual(self.errors("first:\n    value: 1  \nsecond:\n  value: 2\n"), [])

    def test_duplicate_keys_still_fail(self):
        self.assertTrue(self.errors("value: 1\nvalue: 2\n"))

    def test_invalid_yaml_still_fails(self):
        self.assertTrue(self.errors("value: [unterminated\n"))


if __name__ == "__main__":
    unittest.main()
