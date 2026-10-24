import logging

from projects.cluster.library.prom.collection import prepare_user_workload_monitoring
from projects.core.library import config, env
from projects.core.library.ci import ExitCategory
from projects.gpu_operator.toolbox.validate_gpu_operator_dcgm import (
    main as validate_gpu_operator_dcgm,
)
from projects.llm_d.orchestration import observability, prepare_phase, runtime_config

logger = logging.getLogger(__name__)


def run_prepare_sequence() -> int:
    """Run the prepare phase sequence using global config"""
    run_specs = runtime_config.get_run_specs()
    observability_context = observability.get_observability_context(
        run_spec.namespace for run_spec in run_specs
    )

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
    ensured_namespaces: set[str] = set()
    for run_spec in run_specs:
        with runtime_config.activate_run_spec(run_spec):
            with env.NextArtifactDir(f"prepare_{run_spec.artifact_dirname}"):
                prepare_phase.ensure_test_namespace()
                if observability_context:
                    from projects.observability.library.runtime import (
                        claim_workload_namespace,
                    )

                    _, run_id = observability_context
                    claim_workload_namespace(run_spec.namespace, run_id)
                prepare_phase.cleanup_previous_run()
                if observability_context and run_spec.namespace not in ensured_namespaces:
                    from projects.observability.library.runtime import ensure_observability

                    observability_settings, run_id = observability_context
                    ensure_observability(
                        observability_settings,
                        workload_namespace=run_spec.namespace,
                        run_id=run_id,
                    )
                    ensured_namespaces.add(run_spec.namespace)
                prepare_phase.prepare_model_cache()
                if not skip_gpu:
                    prepare_phase.verify_gpu_nodes()
                prepare_phase.capture_prepare_state()

    exit_msg = "Prepare sequence completed successfully - all phases executed without errors"
    logger.info(exit_msg)
    return 0, ExitCategory.SUCCESS, exit_msg
