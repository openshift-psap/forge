"""
Generic LLM/Model Management for FORGE Agentic Processing

This module contains model configuration loading and LangChain LLM client creation
functions that can be used across different agentic workflows.
"""

import logging
import warnings

import yaml

from projects.core.library import vault

# Check for optional agentic dependencies
_AGENTIC_AVAILABLE = True
_MISSING_PACKAGES = []

try:
    import httpx
except ImportError:
    _AGENTIC_AVAILABLE = False
    _MISSING_PACKAGES.append("httpx")

try:
    import urllib3
except ImportError:
    _AGENTIC_AVAILABLE = False
    _MISSING_PACKAGES.append("urllib3")

logger = logging.getLogger(__name__)


def load_model_config(vault_name: str, content_name: str) -> dict:
    """Load model configuration from vault"""
    config_path = vault.get_vault_content_path(vault_name, content_name)

    if not config_path or not config_path.exists():
        raise FileNotFoundError(f"Model config not found at {config_path}")

    with open(config_path) as f:
        return yaml.safe_load(f)


def create_llm_client(model_config: dict):
    """Create a LangChain LLM client from vault configuration"""
    # Check if required dependencies are available
    if not _AGENTIC_AVAILABLE:
        raise ImportError(
            f"Required packages missing for agentic processing: {', '.join(_MISSING_PACKAGES)}"
        )

    model_api = model_config.get("model_api")
    model_id = model_config.get("model_id")
    user_key = model_config.get("user_key")

    if not all([model_api, model_id, user_key]):
        raise ValueError("Missing required model configuration: model_api, model_id, or user_key")

    # Configure ChatOpenAI for internal Red Hat endpoint with SSL bypass
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

        # Create HTTP client with proper headers for litellm endpoint
        # Note: ChatOpenAI takes ownership of the http_client and manages its lifecycle
        http_client = httpx.Client(
            verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0",
                "Accept": "*/*",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
            },
        )

        # Ensure the base_url is correct (should be the base URL without /v1)
        if model_api.endswith("/v1"):
            base_url = model_api
        elif model_api.endswith("/v1/"):
            base_url = model_api[:-1]  # Remove trailing slash
        else:
            base_url = f"{model_api}/v1"

        logger.info(f"Using base_url: {base_url}")

        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError(
                "langchain_openai package is required for agentic processing. Install with: pip install langchain_openai"
            ) from None

        llm = ChatOpenAI(
            model=model_id,
            base_url=base_url,
            api_key=user_key,
            temperature=0.7,
            max_tokens=4096,  # Increased for Qwen compatibility
            http_client=http_client,
            # Disable streaming to match simpler request format
            streaming=False,
            # 5-minute timeout for LLM requests
            request_timeout=300,
        )

    return llm
