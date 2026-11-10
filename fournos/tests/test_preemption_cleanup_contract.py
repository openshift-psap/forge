"""Exercise the deployed-image cleanup result emitted by the shared Tekton Task."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

TASK_PATH = Path(__file__).resolve().parents[1] / "gitops/base/workflows/task-forge-step.yaml"


@pytest.mark.parametrize(
    ("annotations", "expected_result"),
    [
        (
            {
                "fournos.dev/preemption-cleanup-project": "llm_d",
                "fournos.dev/preemption-cleanup-task": "04-post-cleanup",
            },
            True,
        ),
        ({}, False),  # PR-checkout runs have no cleanup contract.
    ],
)
def test_shared_post_cleanup_result_requires_deployed_contract(
    tmp_path: Path, annotations: dict[str, str], expected_result: bool
) -> None:
    task = yaml.safe_load(TASK_PATH.read_text())
    script = next(
        step["script"]
        for step in task["spec"]["steps"]
        if step["name"] == "verify-preemption-cleanup"
    )
    result_path = tmp_path / "cleanup-result"
    script = script.replace("$(results.preemption-cleanup-verified.path)", str(result_path))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / "pipelinerun.json").write_text(
        json.dumps({"metadata": {"annotations": annotations}})
    )
    fake_oc = fake_bin / "oc"
    fake_oc.write_text('#!/bin/sh\ncat "$FAKE_PIPELINERUN_JSON"\n')
    fake_oc.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_PIPELINERUN_JSON": str(tmp_path / "pipelinerun.json"),
        "FOURNOS_STEP": "post-cleanup",
        "FJOB_NAME": "example-job",
        "FOURNOS_WORKLOAD_NAMESPACE": "fournos",
    }

    subprocess.run(["bash", "-c", script], env=env, check=True)

    assert result_path.exists() is expected_result
    if expected_result:
        assert result_path.read_text() == "true"
