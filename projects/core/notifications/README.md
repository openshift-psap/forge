# FORGE Notifications Integration

This directory contains the notifications system that integrates with the run_ci entrypoint to send success/failure notifications.

## Overview

The notifications system provides a generic interface for sending notifications to GitHub PR comments and Slack messages. It can be used by any component that needs to send notifications.

### Supported Platforms

1. **GitHub**: Posts comments to PR threads using GitHub App authentication
2. **Slack**: Posts messages to configured Slack channels with intelligent threading

## Configuration

### Environment Variables

- `FORGE_NOTIFICATION_DRY_RUN=true/false`: Enable dry run mode (shows what would be sent without actually sending)
- `FORGE_ENABLE_SLACK_NOTIFICATIONS=true/false`: Enable/disable Slack notifications (default: false)
- GitHub notifications are enabled by default when appropriate secrets are available

### Required Secrets

The system looks for secret files in directories specified by these environment variables (in order):
- `PSAP_ODS_SECRET_PATH`
- `CRC_MAC_AI_SECRET_PATH`
- `CONTAINER_BENCH_SECRET_PATH`

**GitHub App secrets:**
- `topsail-bot.2024-09-18.private-key.pem`: GitHub App private key
- `topsail-bot.clientid`: GitHub App client ID

**Slack secrets:**
- `topsail-bot.slack-token`: Slack bot token

### Enable Slack Notifications

```bash
export FORGE_ENABLE_SLACK_NOTIFICATIONS=true
run my_project test
```

## Architecture

```
Any calling component
└── send_notification(message, github=True, slack=False)
    ├── GitHub: send_notification_to_github()
    └── Slack: send_notification_to_slack()
```

### Usage Example

```python
from projects.core.notifications.send import send_notification

# Send a custom notification
success = send_notification(
    message="🟢 Test completed successfully\nResults: https://example.com/results",
    github=True,
    slack=False,
    dry_run=False
)
```

## Message Format

The system accepts arbitrary message content and sends it directly to the configured platforms:

- **GitHub**: Messages are posted as PR comments using Markdown formatting
- **Slack**: Messages are posted as threaded messages using Slack's formatting

The calling component is responsible for formatting the message content appropriately for the target platform.

## Dependencies

- `requests`: For GitHub API calls
- `slack_sdk`: For Slack API integration
- `pyjwt[crypto]`: For GitHub App JWT authentication

## Files

- `send.py`: Main notifications controller
- `github/api.py`: GitHub API integration
- `github/gen_jwt.py`: GitHub App JWT token generation
- `slack/api.py`: Slack API integration
