"""
Censoring rules and patterns for Caliper artifact filtering.

This module defines the patterns and rules used to identify sensitive content
in artifacts before upload.
"""

from __future__ import annotations

import re

# Keyword patterns to detect in file content
# These are compiled regex patterns that match common sensitive data patterns
KEYWORD_PATTERNS = [
    # Password patterns - exclude function calls, env lookups, already redacted content
    r"password\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[|''|\"\"|\$)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"passwd\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[|''|\"\"|\$)['\"]?[^'\"\s<>\[\]()]{3,}",
    # API key patterns - improved to exclude function calls and already redacted content
    r"api[_-]?key\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[|''|\"\"|\$)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"apikey\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[|''|\"\"|\$)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"api[_-]?secret\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[|''|\"\"|\$)['\"]?[^'\"\s<>\[\]()]{3,}",
    # Token patterns (general token pattern still disabled, but specific ones enabled)
    # r"(?:^|[^a-zA-Z])token\s*[:=]\s*(?![=~<>])\S+", # DISABLED, too sensitive for inference work ...
    r"secret[_-]?token\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"access[_-]?token\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"api[_-]?token\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"refresh[_-]?token\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    # Bearer tokens (compiled with IGNORECASE, so one pattern covers both cases)
    # Require minimum 20 characters and avoid matching descriptive text like "bearer token file"
    r"Bearer\s+[A-Za-z0-9+/=]{20,}",
    # Specific service API keys
    r"sk-[a-zA-Z0-9]{32,}",  # OpenAI API keys
    r"ghp_[a-zA-Z0-9]{36}",  # GitHub personal access tokens
    r"gho_[a-zA-Z0-9]{36}",  # GitHub OAuth tokens
    r"ghu_[a-zA-Z0-9]{36}",  # GitHub user-to-server tokens
    r"ghs_[a-zA-Z0-9]{36}",  # GitHub server-to-server tokens
    r"ghr_[a-zA-Z0-9]{36}",  # GitHub refresh tokens
    # AWS patterns
    r"AKIA[0-9A-Z]{16}",  # AWS Access Key ID
    r"aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    # Database connection strings
    r"mongodb://[^/\s]+:[^@\s]+@",
    r"mysql://[^/\s]+:[^@\s]+@",
    r"postgresql://[^/\s]+:[^@\s]+@",
    # Generic credential patterns - improved
    r"credential\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    r"private[_-]?key\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    # ML/AI service tokens
    r"(?:huggingface|anthropic|openai|claude)[_-]?(?:token|key|api[_-]?key)\s*[:=]\s*[^'\"\s<>\[\]()]{3,}",
    r"hf_[a-zA-Z0-9]{20,}",  # Hugging Face tokens
    r"ant-[a-zA-Z0-9]{20,}",  # Anthropic API keys (if they use this format)
    # Common secret/key patterns
    r"(?:jwt|webhook|slack|discord|telegram)[_-]?(?:secret|token|key)\s*[:=]\s*[^'\"\s<>\[\]()]{3,}",
    r"encryption[_-]?key\s*[:=]\s*[^'\"\s<>\[\]()]{3,}",
    r"signing[_-]?key\s*[:=]\s*[^'\"\s<>\[\]()]{3,}",
    # Slack tokens (specific patterns)
    r"xox[bpoa]-[a-zA-Z0-9-]+",  # Slack tokens (bot, app, oauth, etc.)
    # Command line arguments
    r"--(?:password|api[_-]?key|token|secret)\s+[^'\"\s]{3,}",
    # OpenShift/Kubernetes secret creation commands
    r"(?:oc|kubectl)\s+create\s+secret\s+.*--from-literal[=\s]+[^=\s]+=[^'\"\s]{3,}",
    # Environment variable exports in shell (explicit lowercase to avoid matching var names)
    r"(?:^|\s)[e][x][p][o][r][t]\s+[A-Z_]*(?:PASSWORD|API_KEY|TOKEN|SECRET)[A-Z_]*=[^'\"\s]{3,}",
]

# Compile patterns for better performance
COMPILED_KEYWORD_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in KEYWORD_PATTERNS]

# File patterns to always censor (by filename)
SENSITIVE_FILE_PATTERNS = [
    r".*\.pem$",  # PEM certificate files
    r".*\.key$",  # Private key files
    r".*\.p12$",  # PKCS#12 certificate files
    r".*\.pfx$",  # PKCS#12 certificate files (Windows)
    r".*secret.*",  # Any file with "secret" in the name
    r".*credential.*",  # Any file with "credential" in the name
    r".*password.*",  # Any file with "password" in the name
    r".*\.ssh/.*",  # SSH directory contents
    r".*/\.ssh/.*",  # SSH directory contents (with path)
    r".*id_rsa.*",  # SSH private keys
    r".*id_dsa.*",  # DSA private keys
    r".*id_ecdsa.*",  # ECDSA private keys
    r".*id_ed25519.*",  # Ed25519 private keys
    r".*\.env$",  # Environment files
    r".*\.env\..*",  # Environment files with suffixes
]

# Compile file patterns for better performance
COMPILED_FILE_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in SENSITIVE_FILE_PATTERNS]


def matches_sensitive_filename(filename: str) -> bool:
    """
    Check if a filename matches any sensitive file pattern.

    Args:
        filename: The filename or path to check

    Returns:
        bool: True if the filename indicates a sensitive file
    """
    from pathlib import Path

    # Filename-only patterns that should match just the basename
    filename_only_patterns = [
        r".*secret.*",
        r".*credential.*",
        r".*password.*",
    ]

    # Check filename-only patterns against basename
    basename = Path(filename).name
    for pattern_str in filename_only_patterns:
        pattern = re.compile(pattern_str, re.IGNORECASE)
        if pattern.match(basename):
            return True

    # Check all other patterns against full path
    for pattern in COMPILED_FILE_PATTERNS:
        pattern_str = pattern.pattern
        if pattern_str not in filename_only_patterns:
            if pattern.match(filename):
                return True

    return False
