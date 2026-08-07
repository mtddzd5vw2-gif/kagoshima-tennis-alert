from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


WORKFLOW_PATH = Path(".github/workflows/update-availability.yml")


def load_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def commit_step_script(workflow: dict[str, Any]) -> str:
    steps = workflow["jobs"]["update"]["steps"]
    return next(
        step["run"]
        for step in steps
        if step.get("name") == "Commit changed availability and notification state"
    )


def push_retry_loop(script: str) -> str:
    match = re.search(
        r"for attempt in .*?; do(?P<body>.*?)^\s*done$",
        script,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def matching_step(workflow: dict[str, Any]) -> dict[str, Any]:
    return next(
        step
        for step in workflow["jobs"]["update"]["steps"]
        if step.get("name") == "Match notification rules"
    )


def test_data_update_concurrency_is_global_and_non_cancelling() -> None:
    workflow = load_workflow()

    assert workflow["concurrency"] == {
        "group": "tennis-availability-writer",
        "cancel-in-progress": False,
    }


def test_push_retry_is_bounded_to_three_attempts() -> None:
    script = commit_step_script(load_workflow())

    assert "MAX_PUSH_ATTEMPTS=3" in script
    assert 'seq 1 "${MAX_PUSH_ATTEMPTS}"' in script
    assert '"${attempt}" -eq "${MAX_PUSH_ATTEMPTS}"' in script
    assert "exit 1" in script


def test_rejected_push_fetches_and_rebases_before_retry() -> None:
    script = commit_step_script(load_workflow())
    loop = push_retry_loop(script)

    push = loop.index("git push")
    rejection_check = loop.index("grep -Eiq")
    fetch = loop.index("git fetch origin main")
    rebase = loop.index("git rebase origin/main")
    wait = loop.index("sleep 2")

    assert push < rejection_check < fetch < rebase < wait


def test_rebase_conflict_aborts_and_fails_without_force_push() -> None:
    script = commit_step_script(load_workflow())
    conflict_handler = re.search(
        r"if ! git rebase origin/main; then(?P<body>.*?)^\s*fi$",
        script,
        re.MULTILINE | re.DOTALL,
    )

    assert conflict_handler is not None
    handler = conflict_handler.group("body")
    assert handler.index("git rebase --abort") < handler.index("exit 1")
    assert "Rebase onto origin/main conflicted" in handler
    assert not re.search(
        r"git\s+push[^\n]*(?:--force(?:-with-lease)?|-f\b)",
        WORKFLOW_PATH.read_text(encoding="utf-8"),
    )


def test_push_retry_loop_only_retries_git_integration_and_push() -> None:
    loop = push_retry_loop(commit_step_script(load_workflow()))

    assert "scripts/scrape.py" not in loop
    assert "LINE" not in loop
    assert "notification" not in loop.lower()
    assert "upload-artifact" not in loop
    assert "run-output" not in loop


def test_deploy_pages_still_requires_a_successful_non_dry_run_update() -> None:
    workflow = load_workflow()

    assert workflow["jobs"]["deploy-pages"]["if"] == (
        "needs.update.result == 'success' && "
        "needs.update.outputs.deploy_pages == 'true'"
    )


def test_notification_matching_is_variable_gated_after_scraping() -> None:
    workflow = load_workflow()
    steps = workflow["jobs"]["update"]["steps"]
    step = matching_step(workflow)
    names = [item.get("name") for item in steps]

    assert step["if"] == "vars.ENABLE_NOTIFICATION_MATCHING == 'true'"
    assert step["continue-on-error"] is True
    assert names.index("Update availability and notification state") < names.index(
        "Match notification rules"
    )
    assert names.index("Match notification rules") < names.index(
        "Upload run data and reservation page snapshots"
    )
    assert "python scripts/match_notification_rules.py" in step["run"]
    assert "--availability run-output/availability.json" in step["run"]


def test_service_role_key_is_scoped_to_the_matching_step() -> None:
    workflow = load_workflow()
    step = matching_step(workflow)

    assert step["env"] == {
        "SUPABASE_URL": "${{ vars.SUPABASE_URL }}",
        "SUPABASE_SERVICE_ROLE_KEY": (
            "${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}"
        ),
    }
    assert "SUPABASE_SERVICE_ROLE_KEY" not in workflow["jobs"]["update"]["env"]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in workflow["jobs"]["deploy-pages"]["env"]
    assert "echo" not in step["run"].lower()


def test_matching_details_are_not_added_to_artifacts_or_pages() -> None:
    workflow = load_workflow()
    update_steps = workflow["jobs"]["update"]["steps"]
    artifact_step = next(
        step
        for step in update_steps
        if step.get("name") == "Upload run data and reservation page snapshots"
    )
    artifact_path = artifact_step["with"]["path"]
    pages_script = next(
        step["run"]
        for step in workflow["jobs"]["deploy-pages"]["steps"]
        if step.get("name") == "Prepare GitHub Pages files"
    )

    assert "match-result" not in artifact_path
    assert "match-result" not in pages_script
    assert "match_candidates" not in artifact_path
    assert "match_candidates" not in pages_script
