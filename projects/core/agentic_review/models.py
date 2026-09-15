"""
NOOA LLM/Model Management for FORGE Agentic Processing

This module provides NOOA-based LLM client creation for FORGE failure analysis agents.
All new agentic functionality uses NOOA (NVIDIA Object-Oriented Agents) framework.
"""

import logging
from typing import Any

import yaml

from projects.core.library import vault

# Check for NOOA availability
_NOOA_AVAILABLE = True
try:
    import nooa.unifiedllm.registry  # noqa: F401
except ImportError:
    _NOOA_AVAILABLE = False

logger = logging.getLogger(__name__)


def load_model_config(vault_name: str, content_name: str) -> dict:
    """Load model configuration from vault"""
    config_path = vault.get_vault_content_path(vault_name, content_name)

    if not config_path or not config_path.exists():
        raise FileNotFoundError(f"Model config not found at {config_path}")

    with open(config_path) as f:
        return yaml.safe_load(f)


def create_llm_client(model_config: dict[str, Any]) -> Any:
    """
    Create a direct LLM client from forge vault configuration

    Args:
        model_config: Model configuration dictionary from vault

    Returns:
        Direct LLM client instance

    Raises:
        ValueError: If required configuration is missing
    """
    model_id = model_config.get("model_id")
    if not model_id:
        raise ValueError("Missing required model configuration: model_id")

    # Extract real forge configuration
    model_api = model_config.get("model_api")
    user_key = model_config.get("user_key")

    logger.info(f"Creating forge LLM client for: {model_id} at {model_api}")

    class ForgeLLMClient:
        def __init__(self, model_id, model_api, user_key):
            self.model_id = model_id
            self.model_api = model_api
            self.user_key = user_key

        def generate(self, prompt):
            import time

            import requests

            start_time = time.time()
            logger.info(f"🚀 [LLM START] Calling {self.model_id} at {self.model_api}")
            logger.info(
                f"📤 [LLM PROMPT] ({len(prompt)} chars):\n{prompt[:500]}{'...' if len(prompt) > 500 else ''}"
            )

            try:
                # Make OpenAI-compatible API call to forge endpoint
                response = requests.post(
                    f"{self.model_api}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.user_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model_id,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 4096,
                        "temperature": 0.1,
                    },
                    timeout=300.0,  # 5 minute timeout
                )

                elapsed = time.time() - start_time
                logger.info(
                    f"⏱️ [LLM HTTP] Response received in {elapsed:.1f}s, status: {response.status_code}"
                )

                response.raise_for_status()

                result = response.json()
                logger.debug(f"🔍 [LLM RAW] Full API response: {result}")

                # Handle different response structures
                content = None
                finish_reason = None

                if "choices" in result and len(result["choices"]) > 0:
                    choice = result["choices"][0]
                    finish_reason = choice.get("finish_reason")

                    if "message" in choice:
                        message = choice["message"]
                        content = message.get("content")

                        # If content is None but we have reasoning_content, use that
                        if content is None and "reasoning_content" in message:
                            content = message.get("reasoning_content")
                    elif "text" in choice:
                        content = choice["text"]

                # Handle token limit case
                if finish_reason == "length":
                    if content:
                        total_elapsed = time.time() - start_time
                        logger.warning(
                            f"⚠️ [LLM TRUNCATED] Response hit token limit after {total_elapsed:.1f}s, partial content returned"
                        )
                        return content
                    else:
                        total_elapsed = time.time() - start_time
                        logger.error(
                            f"❌ [LLM ERROR] Hit token limit with no usable content after {total_elapsed:.1f}s"
                        )
                        raise ValueError(
                            "LLM response hit token limit with no content. Increased max_tokens to 4096."
                        )

                if content is None:
                    total_elapsed = time.time() - start_time
                    logger.error(
                        f"❌ [LLM ERROR] No content found in response after {total_elapsed:.1f}s: {result}"
                    )
                    raise ValueError(f"No content found in LLM response: {result}")

                content = content or ""  # Ensure content is never None
                total_elapsed = time.time() - start_time
                logger.info(
                    f"✅ [LLM SUCCESS] Response ({len(content)} chars) in {total_elapsed:.1f}s:\n{content[:200]}{'...' if len(content) > 200 else ''}"
                )
                return content

            except requests.exceptions.HTTPError as e:
                elapsed = time.time() - start_time
                if e.response.status_code == 500:
                    logger.error(f"🔥 [LLM 500 ERROR] Server error after {elapsed:.1f}s: {e}")
                    logger.error(f"Response: {e.response.text}")
                    return "LLM server error - analysis unavailable"
                else:
                    logger.error(
                        f"❌ [LLM HTTP ERROR] HTTP {e.response.status_code} after {elapsed:.1f}s: {e}"
                    )
                    raise
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"❌ [LLM ERROR] Failed after {elapsed:.1f}s: {e}")
                raise

        def complete(self, prompt):
            logger.info("📞 [LLM CALL] complete() method called -> generate()")
            return self.generate(prompt)

        def __call__(self, prompt):
            logger.info("📞 [LLM CALL] __call__() method called -> generate()")
            return self.generate(prompt)

    return ForgeLLMClient(model_id, model_api, user_key)


def is_nooa_available() -> bool:
    """
    Check if NOOA framework is available

    Returns:
        True if NOOA can be imported and used
    """
    return _NOOA_AVAILABLE


def validate_model_config(model_config: dict[str, Any]) -> bool:
    """
    Validate model configuration for NOOA

    Args:
        model_config: Model configuration dictionary

    Returns:
        True if configuration is valid

    Raises:
        ValueError: If configuration is invalid
    """
    if not model_config:
        raise ValueError("Model configuration cannot be empty")

    if not model_config.get("model_id"):
        raise ValueError("model_id is required in model configuration")

    return True
