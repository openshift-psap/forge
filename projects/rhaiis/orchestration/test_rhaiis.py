import logging

from projects.core.library import ci, config
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
        workload_keys = [_cfg.workload_key]

    result = test_phase.run(
        model_key=_cfg.model_key,
        workload_keys=workload_keys,
        namespace=_cfg.namespace,
    )
    if result:
        raise ci.CIError(
            "RHAIIS test phase failed or completed with warnings; see test logs and notifications",
            ci.ExitCategory.TEST_FAILURE,
        )

    return 0
