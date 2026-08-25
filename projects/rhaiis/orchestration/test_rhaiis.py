import logging

from projects.core.library import config
from projects.rhaiis.orchestration import runtime_config, test_phase

logger = logging.getLogger(__name__)

init = runtime_config.init


@config.requires(
    model_key="tests.rhaiis.model_key",
    workload_key="tests.rhaiis.workload_key",
    namespace="rhaiis.namespace",
)
def test(_cfg):
    workload_keys = config.project.get_config("tests.rhaiis.workload_keys", [])
    if not workload_keys:
        all_workloads = config.project.get_config("workloads", {})
        project_args = config.project.get_config("project.args", [])
        workload_keys = [a for a in project_args if a in all_workloads]
    if not workload_keys:
        workload_keys = [_cfg.workload_key]

    test_phase.run(
        model_key=_cfg.model_key,
        workload_keys=workload_keys,
        namespace=_cfg.namespace,
    )
