"""Caliper integration for FORGE project orchestration (CI / config-driven export).

The public names are resolved lazily: importing this package (or any of its
submodules) must not eagerly import ``orchestration.export``, whose import of
``projects.core.library.export_notifications`` would cycle back here when
``export_notifications`` is the import entry point.
"""

import importlib

_LAZY_EXPORTS = {
    "run_from_orchestration_config": "projects.caliper.orchestration.export",
    "CaliperExportBackendMlflow": "projects.caliper.orchestration.export_config",
    "CaliperExportBackends": "projects.caliper.orchestration.export_config",
    "CaliperExportMlflowSecretsSpec": "projects.caliper.orchestration.export_config",
    "CaliperExportMlflowVaultContentRef": "projects.caliper.orchestration.export_config",
    "CaliperOrchestrationExportConfig": "projects.caliper.orchestration.export_config",
}

__all__ = [
    "CaliperExportBackendMlflow",
    "CaliperExportBackends",
    "CaliperExportMlflowSecretsSpec",
    "CaliperExportMlflowVaultContentRef",
    "CaliperOrchestrationExportConfig",
    "run_from_orchestration_config",
]


def __getattr__(name: str):
    try:
        module_name = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    return getattr(importlib.import_module(module_name), name)
