"""
Shared utilities for Locust user classes.

Provides prompt loading, round-robin counter, and warmup hook logic
reusable across all user class modules.
"""

import json
import os
import threading
from pathlib import Path

SYNTHETIC_PROMPTS_FILENAME = "synthetic_prompts.jsonl"
SYNTHETIC_PROMPT_FILENAME = "synthetic_prompt.txt"

_user_counter = 0
_user_counter_lock = threading.Lock()


def load_prompts():
    """Load all prompts from JSON, falling back to single prompt file, then env var."""
    import sys

    output_dir = os.environ.get("LOCUST_OUTPUT_DIR", "")

    if output_dir:
        jsonl_file = Path(output_dir) / SYNTHETIC_PROMPTS_FILENAME
        if jsonl_file.exists():
            prompts = []
            for line in jsonl_file.read_text().strip().split("\n"):
                if line.strip():
                    prompts.append(json.loads(line)["prompt"])
            if prompts:
                print(
                    f"[prompt-loader] Loaded {len(prompts)} unique prompts from {jsonl_file}",
                    file=sys.stderr,
                    flush=True,
                )
                return prompts

        txt_file = Path(output_dir) / SYNTHETIC_PROMPT_FILENAME
        if txt_file.exists():
            prompt = txt_file.read_text().strip()
            if prompt:
                return [prompt]

    fallback = os.environ.get("PROMPT", "What is the capital of France?")
    return [fallback]


def get_next_index():
    """Thread-safe incrementing counter for round-robin prompt assignment."""
    global _user_counter
    with _user_counter_lock:
        idx = _user_counter
        _user_counter += 1
    return idx


def validate_response(response, *, expected_keys=("output", "choices")):
    """Validate an HTTP response inside a Locust ``catch_response=True`` block.

    Marks the response as success if status is 200 and the JSON body contains
    at least one of ``expected_keys``.  Otherwise marks it as failure with a
    descriptive message.
    """
    if response.status_code != 200:
        response.failure(f"HTTP {response.status_code}: {response.text[:200]}")
        return

    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        response.failure("Invalid JSON response")
        return

    if any(k in data for k in expected_keys):
        response.success()
    else:
        response.failure(f"Unexpected response format: {list(data.keys())}")
