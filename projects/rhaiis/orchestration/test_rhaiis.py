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
    workload_keys = runtime_config.get_test_workload_keys()

    test_phase.run(
        model_key=_cfg.model_key,
        workload_keys=workload_keys,
        namespace=_cfg.namespace,
    )
