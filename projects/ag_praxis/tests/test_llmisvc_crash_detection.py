import pytest

from projects.kserve.toolbox.capture_llmisvc_state.main import _find_crashed_containers


def _container(name, state=None, last_state=None):
    status = {"name": name}
    if state is not None:
        status["state"] = state
    if last_state is not None:
        status["lastState"] = last_state
    return status


def _pod(name, containers=(), init_containers=(), ephemeral_containers=()):
    return {
        "metadata": {"name": name},
        "status": {
            "containerStatuses": list(containers),
            "initContainerStatuses": list(init_containers),
            "ephemeralContainerStatuses": list(ephemeral_containers),
        },
    }


def test_no_pods():
    assert _find_crashed_containers([]) == []


def test_healthy_pods():
    pods = [
        _pod(
            "workload-0",
            containers=[_container("kserve-container", state={"running": {}})],
        ),
        _pod(
            "workload-1",
            containers=[_container("kserve-container", state={"running": {}})],
            init_containers=[
                _container("init", state={"terminated": {"reason": "Completed", "exitCode": 0}})
            ],
        ),
    ]
    assert _find_crashed_containers(pods) == []


@pytest.mark.parametrize(
    "status,expected_fragment",
    [
        (
            _container("app", state={"waiting": {"reason": "CrashLoopBackOff"}}),
            "workload-0/app: waiting (CrashLoopBackOff)",
        ),
        (
            _container("app", state={"terminated": {"reason": "Error", "exitCode": 1}}),
            "workload-0/app: state terminated (Error, exit code 1)",
        ),
        (
            _container(
                "app",
                state={"running": {}},
                last_state={"terminated": {"reason": "Error", "exitCode": 137}},
            ),
            "workload-0/app: lastState terminated (Error, exit code 137)",
        ),
    ],
)
def test_crashed_container(status, expected_fragment):
    pods = [_pod("workload-0", containers=[status])]
    assert _find_crashed_containers(pods) == [expected_fragment]


def test_init_and_ephemeral_container_crashes():
    pods = [
        _pod(
            "workload-0",
            init_containers=[
                _container("model-fetch", state={"terminated": {"reason": "Error", "exitCode": 2}})
            ],
            ephemeral_containers=[
                _container("debug", state={"waiting": {"reason": "CrashLoopBackOff"}})
            ],
        ),
    ]
    crashes = _find_crashed_containers(pods)
    assert len(crashes) == 2
    assert "workload-0/model-fetch: state terminated (Error, exit code 2)" in crashes
    assert "workload-0/debug: waiting (CrashLoopBackOff)" in crashes


def test_completed_container_is_not_a_crash():
    pods = [
        _pod(
            "workload-0",
            containers=[
                _container("app", state={"terminated": {"reason": "Completed", "exitCode": 0}})
            ],
        ),
    ]
    assert _find_crashed_containers(pods) == []


def test_crash_reported_alongside_healthy_containers():
    pods = [
        _pod(
            "workload-0",
            containers=[
                _container("healthy", state={"running": {}}),
                _container("app", state={"waiting": {"reason": "CrashLoopBackOff"}}),
            ],
        ),
    ]
    assert _find_crashed_containers(pods) == ["workload-0/app: waiting (CrashLoopBackOff)"]
