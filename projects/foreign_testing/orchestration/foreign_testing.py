import logging
import os
import pathlib
import shutil

from projects.core.ci_entrypoint import run_common
from projects.core.library import config, env, run
from projects.foreign_testing.library import initialize

logger = logging.getLogger(__name__)


def init():
    env.init()
    run.init()
    config.init(pathlib.Path(__file__).parent)


def get_project_config(foreign_repo_id=None):
    if not foreign_repo_id:
        foreign_repo_id = os.environ.get("PSAP_FORGE_FOREIGN_TESTING")

        if not foreign_repo_id:
            raise ValueError(
                "PSAP_FORGE_FOREIGN_TESTING must be set (and point to `foreign_testing.$NAME` field)"
            )

    foreign_repo_configs = config.project.get_config("foreign_testing")
    foreign_repo_config = foreign_repo_configs.get(foreign_repo_id, None)
    if foreign_repo_config is None:
        raise ValueError(
            f"PSAP_FORGE_FOREIGN_TESTING must point to `foreign_testing.{foreign_repo_id}` field"
        )

    return foreign_repo_config


def prepare():
    foreign_repo_config = get_project_config()

    repo_owner = os.environ.get("REPO_OWNER")
    repo_name = os.environ.get("REPO_NAME")
    pull_pull_sha = os.environ.get("PULL_PULL_SHA")

    repo_dest = initialize.clone_repository(
        repo_owner=repo_owner,
        repo_name=repo_name,
        pull_pull_sha=pull_pull_sha,
    )

    # Copy foreign projects to FORGE home
    forge_projects_dir = env.FORGE_HOME / "projects"
    forge_projects_dir.mkdir(parents=True, exist_ok=True)

    for project_name, project_src in foreign_repo_config["project_mappings"].items():
        src_path = repo_dest / project_src
        dest_path = forge_projects_dir / project_name

        logger.info(f"Copying foreign project: {src_path} -> {dest_path}")

        if not src_path.exists():
            logger.warning(f"Source path not found: {src_path}")
            raise FileNotFoundError(f"Foreign project source not found: {src_path}")

        if dest_path.exists():
            shutil.rmtree(dest_path)
        shutil.copytree(src_path, dest_path)

    return repo_dest


def submit(project_path=None):
    """
    Submit a FOURNOS deployment job

    Args:
        project_path: Path to the project source directory (will be passed as --project-source)
    """
    # Build the command arguments
    foreign_repo_config = get_project_config()

    project = foreign_repo_config["launch"]["project"]
    operation = foreign_repo_config["launch"]["operation"]
    config_args = foreign_repo_config["launch"]["args"]

    launch_args = list(config_args)

    if project_path:
        if not project_path.exists():
            raise ValueError(f"Received a project path that doesn't exist: {project_path}")
        launch_args = [*["--project-source", str(project_path)], *launch_args]
        logger.info(f"Submitting deployment with args: {launch_args}")

    run_common.execute_project_operation(
        project,
        operation,
        launch_args,
        do_prepare_ci=False,
        verbose=True,
    )
