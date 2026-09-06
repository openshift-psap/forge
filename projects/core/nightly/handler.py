"""Nightly pipeline handler — single phase that orchestrates:

1. Resolve the latest available image version (via project's ImageReceiver)
2. Compare against last tested version (via project's NightlyVerifier)
3. Create a FournosJob if a new version is detected

Error visibility is provided via notification files in the CI metadata directory.
"""

from __future__ import annotations

import importlib
import logging
import os

from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library.config import requires
from projects.core.library.postprocess import write_test_labels

logger = logging.getLogger(__name__)


def _get_source_config() -> dict:
    """Read the source-specific config from nightly.sources.<source>."""
    source = config.project.get_config("nightly.source")
    return config.project.get_config(f"nightly.sources.{source}")


def _resolve_class(dotted_path: str):
    """Import a class from a dotted path like 'projects.foo.bar.MyClass'."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _receive_image(source_cfg: dict) -> str:
    """Resolve the latest available version from the source's receiver."""
    receiver_cls = _resolve_class(source_cfg["receiver"])
    receiver = receiver_cls()

    logger.info("Using receiver: %s", type(receiver).__name__)
    version = receiver.get_latest_version()
    logger.info("Resolved version: %s", version)

    version_file = env.BASE_ARTIFACT_DIR / "resolved_version.txt"
    version_file.write_text(version)

    (env.ARTIFACT_DIR / "resolved_version.txt").write_text(version)
    return version


def _confirm_nightly(project: str, image_version: str, source_cfg: dict) -> None:
    """Compare resolved version against last tested, create FournosJob if new."""
    verifier_cls = _resolve_class(config.project.get_config("nightly.verifier"))
    verifier = verifier_cls()

    logger.info("Using verifier: %s", type(verifier).__name__)
    try:
        last_version = verifier.get_last_tested_version()
    except Exception as e:
        logger.warning(
            "Verifier failed (bootstrap scenario?): %s. "
            "Treating as no previous version — will launch a new run.",
            e,
        )
        last_version = ""
    logger.info("Last tested version: '%s'", last_version or "<none>")

    if image_version == last_version:
        logger.info("Version %s already tested. No new run needed.", image_version)
        ci_lib.add_notification_file(
            "nightly-noop", f"Version {image_version} already tested — no-op."
        )
        (env.ARTIFACT_DIR / "result.txt").write_text("NO-OP")
        return

    logger.info("New version detected: %s (previous: %s)", image_version, last_version or "<none>")
    _create_fournos_job(project, image_version, source_cfg)

    result_msg = f"FournosJob created for {project} version {image_version}"
    ci_lib.add_notification_file("nightly-triggered", result_msg)
    (env.ARTIFACT_DIR / "result.txt").write_text(result_msg)


@requires(
    cluster="nightly.cluster",
    version_key="nightly.version_key",
    pipeline="nightly.pipeline",
    preset="nightly.preset",
    owner="nightly.owner",
)
def _create_fournos_job(project: str, version: str, source_cfg: dict, _cfg) -> None:
    """Create a FournosJob for the full test run using the fournos_launcher toolbox."""
    from projects.fournos_launcher.toolbox.submit_and_wait.main import run as submit_and_wait

    namespace = os.environ.get("FOURNOS_WORKLOAD_NAMESPACE", "fournos-workloads")

    args_list = [_cfg.preset] if _cfg.preset else []

    overrides = {_cfg.version_key: version}
    if mlflow_experiment := source_cfg.get("mlflow_experiment"):
        overrides["caliper.export.backend.mlflow.config.experiment"] = mlflow_experiment
        logger.info("MLflow experiment override: %s", mlflow_experiment)

    logger.info(
        "Creating FournosJob: project=%s, version=%s, cluster=%s, pipeline=%s",
        project,
        version,
        _cfg.cluster,
        _cfg.pipeline,
    )

    submit_and_wait(
        project,
        cluster_name=_cfg.cluster,
        args=args_list,
        variables_overrides=overrides,
        namespace=namespace,
        owner=_cfg.owner,
        display_name=f"nightly {project} {version}",
        pipeline_name=_cfg.pipeline,
        exclusive=True,
        wait=False,
    )


def _write_test_labels() -> None:
    """Write Caliper test metadata so the caliper export ``run_naming``
    templates can resolve the ``{outcome}`` placeholder."""
    result_file = env.ARTIFACT_DIR / "result.txt"
    if result_file.exists():
        outcome = "noop" if result_file.read_text().strip() == "NO-OP" else "launched"
    else:
        outcome = "error"

    labels = {"outcome": outcome}
    write_test_labels(env.ARTIFACT_DIR, labels, dump_config=False)
    logger.info("Wrote test labels: %s", labels)


def run():
    """Entrypoint for the nightly phase."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    project = config.project.get_config("project.name")
    source_cfg = _get_source_config()
    logger.info("Project: %s, source config: %s", project, source_cfg)

    try:
        image_version = _receive_image(source_cfg)
    except Exception as e:
        logger.error("receive-image failed: %s", e)
        ci_lib.add_notification_file("nightly-receive-image-failed", f"FATAL: {e}")
        raise

    try:
        _confirm_nightly(project, image_version, source_cfg)
    except Exception as e:
        logger.error("confirm-nightly failed: %s", e)
        ci_lib.add_notification_file("nightly-confirm-failed", f"FATAL: {e}")
        raise
    finally:
        _write_test_labels()
