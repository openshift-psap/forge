#!/usr/bin/env python3

from __future__ import annotations

import logging
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, shell, task

logger = logging.getLogger("DSL")

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "orchestration" / "poc" / "maas-deploy-test.sh"
VALID_ACTIONS = ("deploy", "test", "cleanup", "status")


@entrypoint
def run(*, action: str):
    """Run a MaaS simulated-LLM deploy/test action.

    Args:
        action: Action to perform (deploy, test, cleanup, status).
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action '{action}', must be one of {VALID_ACTIONS}")

    return execute_tasks(locals())


@task
def run_maas_sim_test(args, ctx):
    """Run the MaaS simulated-LLM deploy/test shell script."""
    shell.run([str(SCRIPT_PATH), args.action], shell=False, timeout_seconds=300)
    return f"maas-deploy-test.sh {args.action} completed"


if __name__ == "__main__":
    run.main()
