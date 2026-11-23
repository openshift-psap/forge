import logging

from projects.cluster.library.prom.collection import prepare_user_workload_monitoring
from projects.core.library import config, env
from projects.core.library.ci import ExitCategory
from projects.gpu_operator.toolbox.validate_gpu_operator_dcgm import (
    main as validate_gpu_operator_dcgm,
)
from projects.llm_d.orchestration import prepare_phase, runtime_config

logger = logging.getLogger(__name__)

PROFILER_INJECTOR_LABELS = {
    "app.kubernetes.io/managed-by": "forge",
    "forge.openshift.io/project": "llm_d",
}


def _profiler_injector_namespace() -> str:
    return config.project.get_config("profiler.injector_namespace", default_value="vllm-profiler")


def prepare_profiler_injector(namespace: str) -> None:
    """Deploy the vLLM profiler env-injector for the test namespace.

    Deployed only when the profiler is enabled, so profiling works on a
    clean cluster without manual injector setup (the injection itself only
    affects pods labelled vllm-profiler/enabled=true in the namespace).
    """
    from projects.kserve.toolbox.deploy_profiler_injector.main import (
        run as deploy_profiler_injector,
    )

    deploy_profiler_injector(
        namespace=namespace,
        injector_namespace=_profiler_injector_namespace(),
        profiling_ranges=config.project.get_config("profiler.ranges", default_value="500-503"),
        labels=PROFILER_INJECTOR_LABELS,
    )


def teardown_profiler_injector_if_configured(namespace: str) -> None:
    """Remove the env-injector in post-cleanup when configured to do so.

    Only touches resources carrying PROFILER_INJECTOR_LABELS, so an
    injector deployed by someone else and reused by an idempotent prepare
    is never removed.
    """
    if not config.project.get_config("profiler.teardown_after_run", default_value=False):
        return

    from projects.kserve.toolbox.teardown_profiler_injector.main import run as teardown

    teardown(
        namespace=namespace,
        injector_namespace=_profiler_injector_namespace(),
        labels=PROFILER_INJECTOR_LABELS,
    )


def run_prepare_sequence() -> int:
    """Run the prepare phase sequence using global config"""
    prepare_phase.verify_oc_access()
    prepare_phase.verify_cluster_version()
    prepare_user_workload_monitoring(during="prepare")
    prepare_phase.prepare_cert_manager()
    prepare_phase.prepare_leader_worker_set()

    skip_gpu = config.project.get_config("platform.cluster.skip_gpu_readiness")
    if skip_gpu:
        logger.info("Skipping GPU readiness (NFD + GPU operator): skip_gpu_readiness is enabled")
    else:
        prepare_phase.prepare_nfd()
        prepare_phase.prepare_gpu_operator()
        validate_gpu_operator_dcgm.run()

    prepare_phase.prepare_rhoai_operator()
    prepare_phase.apply_datasciencecluster()
    prepare_phase.wait_for_datasciencecluster_ready()
    prepare_phase.ensure_required_crds()
    prepare_phase.ensure_gateway()
    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            with env.NextArtifactDir(f"prepare_{run_spec.artifact_dirname}"):
                prepare_phase.ensure_test_namespace()
                if config.project.get_config("profiler.enabled", default_value=False):
                    prepare_profiler_injector(run_spec.namespace)
                prepare_phase.cleanup_previous_run()
                prepare_phase.prepare_model_cache()
                if not skip_gpu:
                    prepare_phase.verify_gpu_nodes()
                prepare_phase.capture_prepare_state()

    exit_msg = "Prepare sequence completed successfully - all phases executed without errors"
    logger.info(exit_msg)
    return 0, ExitCategory.SUCCESS, exit_msg
