import logging
import os
import pathlib
import shlex
import tempfile

from projects.core.library import config, run

logger = logging.getLogger(__name__)

FORGE_FOREIGN_TESTING_REPO_PATH = "FORGE_FOREIGN_TESTING_REPO_PATH"
FORGE_FOREIGN_TESTING_PULL_PULL_SHA = "FORGE_FOREIGN_TESTING_PULL_PULL_SHA"


def get_repository_path():
    repository_path = os.environ.get(FORGE_FOREIGN_TESTING_REPO_PATH)
    if not repository_path:
        raise ValueError(f"{FORGE_FOREIGN_TESTING_REPO_PATH} must be set")

    path = pathlib.Path(repository_path)
    if not path.is_dir():
        raise FileNotFoundError(f"Foreign repository path does not exist: {path}")

    return path


def clone_repository(*, repo_owner, repo_name, pull_pull_sha):
    values = {
        "REPO_OWNER": repo_owner,
        "REPO_NAME": repo_name,
        FORGE_FOREIGN_TESTING_PULL_PULL_SHA: pull_pull_sha,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"Required foreign repository variables are missing: {', '.join(missing)}")

    repository_path = pathlib.Path(tempfile.mkdtemp(prefix="forge-foreign-testing-"))
    quoted_path = shlex.quote(str(repository_path))
    repository_url = shlex.quote(f"https://github.com/{repo_owner}/{repo_name}")
    commit = shlex.quote(pull_pull_sha)

    run.run(f"git clone {repository_url} {quoted_path}")
    run.run(f"git -C {quoted_path} fetch --quiet origin {commit}")
    run.run(f"git -C {quoted_path} reset --hard FETCH_HEAD")
    run.run(f"git -C {quoted_path} show --quiet FETCH_HEAD")

    os.environ[FORGE_FOREIGN_TESTING_REPO_PATH] = str(repository_path)
    logger.info("Foreign repository checked out at %s", repository_path)
    return repository_path


def initialize():
    if os.environ.get(FORGE_FOREIGN_TESTING_REPO_PATH):
        return get_repository_path()

    repository_config = config.project.get_config("foreign_testing.repo", print=False)
    pull_pull_sha = os.environ.get(FORGE_FOREIGN_TESTING_PULL_PULL_SHA)
    if not pull_pull_sha:
        if repository_config["pr"] is not None:
            pull_pull_sha = f"refs/pull/{repository_config['pr']}/head"
        else:
            pull_pull_sha = repository_config["branch"]

    return clone_repository(
        repo_owner=repository_config["owner"],
        repo_name=repository_config["name"],
        pull_pull_sha=pull_pull_sha,
    )
