import logging

from projects.cluster.library.prom.collection import prepare_user_workload_monitoring
from projects.core.library import config, env
from projects.gpu_operator.toolbox.validate_gpu_operator_dcgm import (
    main as validate_gpu_operator_dcgm,
)
from projects.llm_d.orchestration import prepare_phase, runtime_config

logger = logging.getLogger(__name__)


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
                prepare_phase.cleanup_previous_run()
                prepare_phase.prepare_model_cache()
                if not skip_gpu:
                    prepare_phase.verify_gpu_nodes()
                prepare_phase.capture_prepare_state()
    logger.info("Prepare sequence completed successfully - all phases executed without errors")
    return 0
