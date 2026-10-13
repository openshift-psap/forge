import logging
import os

import projects.core.notifications.github.api as github_api
from projects.core.library import vault as vault_lib

logger = logging.getLogger(__name__)


GITHUB_APP_PEM_FILE = "topsail-bot.2024-09-18.private-key.pem"
GITHUB_APP_CLIENT_ID_FILE = "topsail-bot.clientid"
SLACK_TOKEN_FILE = "topsail-bot.slack-token"

DEFAULT_REPO_OWNER = "openshift-psap"
DEFAULT_REPO_NAME = "forge"

#  avoid importing projects.caliper.orchestration.postprocess here
POSTPROCESS_STATUS_FILENAME = "postprocess_status.yaml"


def send_notification(
    message,
    github=True,
    dry_run=False,
    notification_vault=None,
):
    """Send a generic notification message to GitHub, Slack, and/or Jira.

    Args:
        message: The notification message content
        github: Whether to send to GitHub (default True)
        dry_run: Whether to only log the message without sending (default False)
        notification_vault: Optional vault name to get notification secrets from

    Returns:
        bool: False if any notification failed, True if all succeeded
    """

    if github and not github_api:
        logger.info("Github API not available, don't send notification to github")
        github = False

    if github and os.environ.get("JOB_TYPE") == "periodic":
        logger.info("Running from a Periodic job, don't send notification to github")
        github = False

    if github and not (
        os.environ.get("PULL_NUMBER") or os.environ.get("FORGE_FOREIGN_TESTING_PULL_NUMBER")
    ):
        logger.info(
            "PULL_NUMBER/FORGE_FOREIGN_TESTING_PULL_NUMBER is not set; don't send notification to github"
        )
        github = False

    if not github:
        return True

    vault_def = vault_lib.get_vault_manager().get_vault(notification_vault)
    if not vault_def:
        if github:
            logger.error(
                f"Cannot send GitHub notification: vault '{notification_vault}' not available"
            )

        if not dry_run:
            return False

    failed = False
    if github and not send_notification_to_github(
        vault_def,
        message,
        dry_run,
    ):
        failed = True

    return not failed


def send_notification_to_github(vault_def, message, dry_run):
    """Send a generic notification message to GitHub."""

    pem_file, client_id = get_github_secrets(vault_def)
    pr_number = os.environ.get("PULL_NUMBER") or os.environ.get("FORGE_FOREIGN_TESTING_PULL_NUMBER")
    org, repo = get_org_repo()

    abort = False

    if None in (pem_file, client_id):
        logger.error("github: Cannot access the Github notification secrets")
        abort = True

    if None in (pr_number,):
        logger.error("github: Cannot figure out the PR number")
        abort = True

    if None in (org, repo):
        logger.error("github: Cannot access the org/repo")
        abort = True

    if abort:
        logger.error("github: Aborting due to previous error(s).")
        return False

    user_token = github_api.get_user_token(pem_file, client_id, org, repo)
    if not user_token:
        logger.error("github: Couldn't fetch the user token. Is the app installed in the repo?")
        return False

    if dry_run:
        logger.info(f"Github notification:\n{message}")
        logger.info("***")
        logger.info("***")
        logger.info("***\n")

        return True

    resp = github_api.send_notification(org, repo, user_token, pr_number, message)

    if not resp.ok:
        logger.fatal(f"Github notification post failed :/ {resp.text}")

    return resp.ok


def get_org_repo():
    return (
        os.environ.get("REPO_OWNER", DEFAULT_REPO_OWNER),
        os.environ.get("REPO_NAME", DEFAULT_REPO_NAME),
    )


def get_github_secrets(vault_def):
    pem_file = vault_def.content.get(GITHUB_APP_PEM_FILE).file_path
    client_id_file = vault_def.content.get(GITHUB_APP_CLIENT_ID_FILE).file_path

    if not pem_file:
        logger.warning(
            f"Github App private key does not exists ({GITHUB_APP_PEM_FILE}) in {vault_def.name}"
        )

    if not client_id_file:
        logger.warning(
            f"Github App clientid file does not exists ({GITHUB_APP_CLIENT_ID_FILE}) in {vault_def.name}"
        )

    if not (pem_file and client_id_file):
        return None, None

    client_id_content = client_id_file.read_text().strip()

    return pem_file, client_id_content
