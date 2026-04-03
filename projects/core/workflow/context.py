"""Workflow execution context."""

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import projects.core.library.env as env


@dataclass
class WorkflowContext:
    """
    Runtime context for workflow execution.

    Holds run-specific state: UUID, artifact directories, config, and env vars.
    Created once per workflow execution and passed to all steps.
    Integrates with the existing env.ARTIFACT_DIR system.
    """

    run_uuid: str
    artifact_dir: Path
    config: dict[str, Any] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Current step tracking
    step_number: int = 0
    current_step_name: str = ""

    @classmethod
    def from_environment(
        cls,
        artifact_base: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> "WorkflowContext":
        """
        Create context from environment variables.

        Reads FORGE_* environment variables and creates artifact directory.
        Integrates with env.init() if artifact_base not provided.

        Args:
            artifact_base: Base path for artifacts (uses env.ARTIFACT_DIR if not set)
            config: Optional config dict to merge

        Returns:
            Initialized WorkflowContext
        """
        run_uuid = str(uuid.uuid4())

        # Collect FORGE_* env vars
        env_vars = {k: v for k, v in os.environ.items() if k.startswith("FORGE_")}

        # Use existing env.ARTIFACT_DIR system if available
        if artifact_base:
            artifact_dir = Path(artifact_base) / run_uuid
            artifact_dir.mkdir(parents=True, exist_ok=True)
        elif env.ARTIFACT_DIR:
            artifact_dir = env.ARTIFACT_DIR / run_uuid
            artifact_dir.mkdir(parents=True, exist_ok=True)
        else:
            # Initialize env system
            env.init()
            artifact_dir = env.ARTIFACT_DIR / run_uuid
            artifact_dir.mkdir(parents=True, exist_ok=True)

        # Create _meta subdirectory
        meta_dir = artifact_dir / "_meta"
        meta_dir.mkdir(exist_ok=True)

        return cls(
            run_uuid=run_uuid,
            artifact_dir=artifact_dir,
            config=config or {},
            env_vars=env_vars,
        )

    def get_step_artifact_dir(self, step_name: str) -> Path:
        """
        Get artifact directory for a specific step.

        Creates numbered directory like: 001__deploy/

        Args:
            step_name: Name of the step

        Returns:
            Path to step's artifact directory
        """
        self.step_number += 1
        self.current_step_name = step_name
        step_dir = self.artifact_dir / f"{self.step_number:03d}__{step_name}"
        step_dir.mkdir(exist_ok=True)
        return step_dir

    def get_env(self, key: str, default: str | None = None) -> str | None:
        """
        Get environment variable with FORGE_ prefix.

        Args:
            key: Variable name (with or without FORGE_ prefix)
            default: Default value if not found

        Returns:
            Environment variable value or default
        """
        if not key.startswith("FORGE_"):
            key = f"FORGE_{key}"
        return self.env_vars.get(key, default)

    def write_metadata(self, args: dict[str, Any] | None = None) -> Path:
        """
        Write run metadata to _meta/metadata.yaml.

        Args:
            args: CLI arguments to include

        Returns:
            Path to metadata file
        """
        meta_path = self.artifact_dir / "_meta" / "metadata.yaml"
        metadata = {
            "run_uuid": self.run_uuid,
            "start_time": self.start_time.isoformat(),
            "env_vars": self.env_vars,
            "config": self.config,
            "args": args or {},
        }
        with open(meta_path, "w") as f:
            yaml.safe_dump(metadata, f, default_flow_style=False)
        return meta_path

    def write_restart_script(self, command: str) -> Path:
        """
        Write restart script to _meta/restart.sh.

        Args:
            command: Full command to replay this run

        Returns:
            Path to restart script
        """
        restart_path = self.artifact_dir / "_meta" / "restart.sh"
        script = f"""#!/bin/bash
# Restart script for run {self.run_uuid}
# Generated at {self.start_time.isoformat()}

{command}
"""
        with open(restart_path, "w") as f:
            f.write(script)
        restart_path.chmod(0o755)
        return restart_path
