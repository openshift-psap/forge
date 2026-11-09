import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import product
from typing import Any

logger = logging.getLogger(__name__)

_UNSAFE_DIRNAME_RE = re.compile(r"[/='\"]+")


def _sanitize_dirname(name: str) -> str:
    return _UNSAFE_DIRNAME_RE.sub("", name)


@dataclass(frozen=True)
class MatrixCombination:
    overrides: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    artifact_dirname: str = ""


def compute_matrix(matrix_config: dict) -> list[MatrixCombination]:
    """Compute cross-product of matrix dimensions.

    Args:
        matrix_config: Dictionary with optional keys:
            dimensions: mapping of config paths to value lists
                e.g. {"test.payload": ["XS", "default", "L"]}
            labels: mapping of config paths to label names
                e.g. {"test.payload": "payload"}

    Returns:
        List of MatrixCombination, one per cross-product point.
        Empty dimensions returns a single no-op combination.
    """
    if not matrix_config.get("enabled", False):
        return [MatrixCombination()]

    dimensions = matrix_config.get("dimensions", {})
    label_map = matrix_config.get("labels", {})

    if not dimensions:
        return [MatrixCombination()]

    config_paths = list(dimensions.keys())
    value_lists = []
    for path in config_paths:
        values = dimensions[path]
        if not isinstance(values, list):
            values = [values]
        value_lists.append(values)

    label_keys = [label_map.get(path, path) for path in config_paths]

    combinations = []
    for values in product(*value_lists):
        overrides = dict(zip(config_paths, values, strict=True))
        labels = {lk: str(v) for lk, v in zip(label_keys, values, strict=True)}
        dirname_parts = [_sanitize_dirname(str(v)) for v in values]
        artifact_dirname = "_".join(dirname_parts)

        combinations.append(
            MatrixCombination(
                overrides=overrides,
                labels=labels,
                artifact_dirname=artifact_dirname,
            )
        )

    logger.info("Matrix: %d combinations from %d dimensions", len(combinations), len(config_paths))
    return combinations


@contextmanager
def activate_combination(combination: MatrixCombination):
    """Temporarily set config values for a matrix combination."""
    from projects.core.library import config

    if not combination.overrides:
        yield combination
        return

    saved = {}
    for key in combination.overrides:
        saved[key] = config.project.get_config(key, print=False)

    for key, value in combination.overrides.items():
        config.project.set_config(key, value)

    try:
        yield combination
    finally:
        for key, value in saved.items():
            config.project.set_config(key, value)
