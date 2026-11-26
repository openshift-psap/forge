"""
Censoring rules and patterns for Caliper artifact filtering.

This module defines the patterns and rules used to identify sensitive content
in artifacts before upload.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    r"(?<!X-Amz-Credential=)AKIA[0-9A-Z]{16}",  # AWS Access Key ID (exclude CDN presigned URLs)
    r"aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
    # Database connection strings
    r"mongodb://[^/\s]+:[^@\s]+@",
    r"mysql://[^/\s]+:[^@\s]+@",
    r"postgresql://[^/\s]+:[^@\s]+@",
    # Generic credential patterns - improved
    r"(?<!X-Amz-)credential\s*[:=]\s*(?!get_|os\.|env\.|<|>|\[)['\"]?[^'\"\s<>\[\]()]{3,}",
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
    # Note: [^\n]* instead of .* (identical without DOTALL, but explicit): the scan stays
    # line-bounded, and the --from-literal pre-filter below keeps the backtracking cost
    # away from files that do not contain --from-literal at all.
    r"(?:oc|kubectl)\s+create\s+secret\s+[^\n]*--from-literal[=\s]+[^=\s]+=[^'\"\s]{3,}",
    # Environment variable exports in shell (explicit lowercase to avoid matching var names)
    r"(?:^|\s)[e][x][p][o][r][t]\s+[A-Z_]*(?:PASSWORD|API_KEY|TOKEN|SECRET)[A-Z_]*=[^'\"\s]{3,}",
]

# Compile patterns for better performance
COMPILED_KEYWORD_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in KEYWORD_PATTERNS]

# Bytes-compiled variants for the fast file-scan path in censoring.py: scanning raw
# bytes avoids decoding files that end up clean (the common case).
# Note: IGNORECASE on bytes patterns folds ASCII case only.
COMPILED_KEYWORD_PATTERNS_BYTES = [
    re.compile(pattern.encode("ascii"), re.IGNORECASE) for pattern in KEYWORD_PATTERNS
]

# Required literals, index-aligned with KEYWORD_PATTERNS: a match of pattern i is
# impossible unless the (lowercased) content contains at least one of these byte
# sequences. Used as a cheap substring pre-filter to skip the regex scan entirely
# on files that cannot match — see ArtifactCensor._sanitize_file_content.
# /!\ Keep aligned with KEYWORD_PATTERNS when adding, removing or reordering patterns.
KEYWORD_PATTERN_REQUIRED_LITERALS: list[tuple[bytes, ...]] = [
    (b"password",),  # password
    (b"passwd",),  # passwd
    (b"api",),  # api key
    (b"apikey",),  # apikey
    (b"api",),  # api secret
    (b"secret",),  # secret token
    (b"access",),  # access token
    (b"api",),  # api token
    (b"refresh",),  # refresh token
    (b"bearer",),  # Bearer tokens
    (b"sk-",),  # OpenAI API keys
    (b"ghp_",),  # GitHub personal access tokens
    (b"gho_",),  # GitHub OAuth tokens
    (b"ghu_",),  # GitHub user-to-server tokens
    (b"ghs_",),  # GitHub server-to-server tokens
    (b"ghr_",),  # GitHub refresh tokens
    (b"akia",),  # AWS Access Key ID
    (b"aws",),  # AWS secret access key
    (b"mongodb://",),
    (b"mysql://",),
    (b"postgresql://",),
    (b"credential",),
    (b"private",),  # private key
    (b"huggingface", b"anthropic", b"openai", b"claude"),  # ML/AI service tokens
    (b"hf_",),  # Hugging Face tokens
    (b"ant-",),  # Anthropic API keys
    (b"jwt", b"webhook", b"slack", b"discord", b"telegram"),  # service secrets
    (b"encryption",),
    (b"signing",),
    (b"xox",),  # Slack tokens
    (b"--password", b"--api", b"--token", b"--secret"),  # command line arguments
    (b"--from-literal",),  # oc/kubectl create secret
    (b"export",),  # shell exports
]

if len(KEYWORD_PATTERN_REQUIRED_LITERALS) != len(KEYWORD_PATTERNS):
    raise ValueError(
        "KEYWORD_PATTERN_REQUIRED_LITERALS is misaligned with KEYWORD_PATTERNS "
        f"({len(KEYWORD_PATTERN_REQUIRED_LITERALS)} vs {len(KEYWORD_PATTERNS)} entries)"
    )

# Filename-only patterns: matched against the file basename
SENSITIVE_BASENAME_PATTERNS = [
    r".*secret.*",  # Any file with "secret" in the name
    r".*credential.*",  # Any file with "credential" in the name
    r".*password.*",  # Any file with "password" in the name
]

# Path patterns: matched against the full file path
SENSITIVE_PATH_PATTERNS = [
    r".*\.pem$",  # PEM certificate files
    r".*\.key$",  # Private key files
    r".*\.p12$",  # PKCS#12 certificate files
    r".*\.pfx$",  # PKCS#12 certificate files (Windows)
    r".*\.ssh/.*",  # SSH directory contents
    r".*/\.ssh/.*",  # SSH directory contents (with path)
    r".*id_rsa.*",  # SSH private keys
    r".*id_dsa.*",  # DSA private keys
    r".*id_ecdsa.*",  # ECDSA private keys
    r".*id_ed25519.*",  # Ed25519 private keys
    r".*\.env$",  # Environment files
    r".*\.env\..*",  # Environment files with suffixes
]

# All sensitive file patterns (basename + path)
SENSITIVE_FILE_PATTERNS = SENSITIVE_BASENAME_PATTERNS + SENSITIVE_PATH_PATTERNS

# Compile file patterns for better performance
COMPILED_FILE_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in SENSITIVE_FILE_PATTERNS]
_COMPILED_BASENAME_PATTERNS = [
    re.compile(pattern, re.IGNORECASE) for pattern in SENSITIVE_BASENAME_PATTERNS
]
_COMPILED_PATH_PATTERNS = [
    re.compile(pattern, re.IGNORECASE) for pattern in SENSITIVE_PATH_PATTERNS
]


def matches_sensitive_filename(filename: str) -> bool:
    """
    Check if a filename matches any sensitive file pattern.

    Args:
        filename: The filename or path to check

    Returns:
        bool: True if the filename indicates a sensitive file
    """
    basename = Path(filename).name
    if any(pattern.match(basename) for pattern in _COMPILED_BASENAME_PATTERNS):
        return True

    return any(pattern.match(filename) for pattern in _COMPILED_PATH_PATTERNS)
