"""Report the seven-day GHCR cooldown as a status on each PR head."""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


CONTEXT = "GHCR cooldown"
LABEL = "ghcr-cooldown"
AUTOMERGE_LABEL = "ghcr-cooldown-automerge"
ACTIONS_BOT = "github-actions[bot]"
RENOVATE_BOT = "renovate[bot]"
COOLDOWN = timedelta(days=7)
OBSERVATION = re.compile(r"<!-- ghcr-cooldown: ([a-f0-9]{64}) -->$")
IMMICH = "apps/argocd-apps/immich/application.yaml"


def github_api(path, *, data=None, paginate=False, method="POST"):
    command = ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/{path}"]
    if paginate:
        command += ["--paginate", "--slurp"]
    if data is not None:
        command += ["--method", method, "--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    response = json.loads(result.stdout)
    if paginate:
        return [item for page in response for item in page]
    return response


def candidate_fingerprint(files, labels):
    """Hash proposed changes without hunk offsets or unchanged context."""
    changes = []
    needs_cooldown = LABEL in labels
    for file in sorted(files, key=lambda entry: entry["filename"]):
        if file["status"] == "removed":
            continue
        patch = file.get("patch")
        if patch is None:
            # GitHub can omit a patch. Use the full blob ID for such files.
            changes.append((file["filename"], file["sha"]))
            continue
        added = [
            line[1:].strip()
            for line in patch.splitlines()
            if line.startswith("+")
        ]
        if not added:
            continue
        # The label covers updates whose registry is outside the diff context.
        # Also cover inline references and Immich PRs that predate the label.
        needs_cooldown |= "ghcr.io/" in patch or (
            file["filename"] == IMMICH
            and any(line.startswith("targetRevision:") for line in added)
        )
        changes.append((file["filename"], added))
    if not needs_cooldown:
        return None
    return hashlib.sha256(json.dumps(changes).encode()).hexdigest()


def latest_observation(comments):
    for comment in reversed(comments):
        if comment["user"]["login"] != ACTIONS_BOT:
            continue
        marker = OBSERVATION.search(comment["body"])
        if marker:
            return marker[1], datetime.fromisoformat(comment["created_at"])
    return None, None


def report_status(head, state, description):
    statuses = github_api(f"commits/{head}/statuses?per_page=100", paginate=True)
    current = next(
        (status for status in statuses if status["context"] == CONTEXT), None
    )
    if (
        current
        and current["creator"]["login"] == ACTIONS_BOT
        and current["state"] == state
        and current["description"] == description
    ):
        return
    github_api(
        f"statuses/{head}",
        data={"context": CONTEXT, "state": state, "description": description},
    )


def merge_eligible_patch(number, head):
    path = f"pulls/{number}"
    pull = github_api(path)
    labels = {label["name"] for label in pull["labels"]}
    if (
        pull["state"] != "open"
        or pull["head"]["sha"] != head
        or (pull["head"]["repo"] or {}).get("full_name") != os.environ["GITHUB_REPOSITORY"]
        or pull["user"]["login"] != RENOVATE_BOT
        or pull["draft"]
        or pull["mergeable_state"] != "clean"
        or AUTOMERGE_LABEL not in labels
    ):
        return

    # Merge immediately under the repo rules, with a SHA precondition.
    # Queuing GitHub auto-merge could later merge a different, younger digest.
    github_api(f"{path}/merge", method="PUT", data={"sha": head, "merge_method": "squash"})


def check_pull_request(number):
    path = f"pulls/{number}"
    pull = github_api(path)
    if pull["state"] != "open" or pull["base"]["ref"] != "master":
        return
    head = pull["head"]["sha"]
    labels = {label["name"] for label in pull["labels"]}
    if pull["user"]["login"] != RENOVATE_BOT and LABEL not in labels:
        report_status(head, "success", "No Renovate GHCR update.")
        return

    files = github_api(f"{path}/files?per_page=100", paginate=True)
    if len(files) != pull["changed_files"]:
        raise RuntimeError(f"Could not read all changed files for PR #{number}")
    fingerprint = candidate_fingerprint(files, labels)
    if github_api(path)["head"]["sha"] != head:
        return  # The next synchronize event or scheduled run will check the new head.
    if fingerprint is None:
        report_status(head, "success", "No GHCR dependency update.")
        return

    comments_path = f"issues/{number}/comments"
    comments = github_api(f"{comments_path}?per_page=100", paginate=True)
    last_fingerprint, observed_at = latest_observation(comments)
    if last_fingerprint != fingerprint:
        report_status(head, "pending", "Starting the seven-day GHCR cooldown.")
        observation = github_api(
            comments_path,
            data={
                "body": (
                    "The seven-day GHCR cooldown started for this update. "
                    "See the GHCR cooldown status for the earliest merge time."
                    f"\n\n<!-- ghcr-cooldown: {fingerprint} -->"
                )
            },
        )
        observed_at = datetime.fromisoformat(observation["created_at"])

    eligible_at = observed_at + COOLDOWN
    if datetime.now(timezone.utc) >= eligible_at:
        report_status(head, "success", "GHCR update completed its seven-day cooldown.")
        if AUTOMERGE_LABEL in labels:
            merge_eligible_patch(number, head)
    else:
        date = eligible_at.strftime("%Y-%m-%d %H:%M UTC")
        report_status(head, "pending", f"GHCR update can merge after {date}.")


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if "pull_request" in event:
        check_pull_request(event["pull_request"]["number"])
        return
    pull_requests = github_api(
        "pulls?state=open&base=master&per_page=100", paginate=True
    )
    failed = False
    for pull in pull_requests:
        try:
            check_pull_request(pull["number"])
        except (RuntimeError, subprocess.CalledProcessError) as error:
            failed = True
            print(f"PR #{pull['number']}: {error}", file=sys.stderr)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
