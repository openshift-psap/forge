from projects.rhaiis.orchestration.runtime_config import apply_hardware_spec


def test_sets_gpu_count_from_tp_size():
    spec = {}
    result = apply_hardware_spec(spec, tp_size=4, pp_size=1, dp_size=1, gpu_type="h200")
    assert result["gpuCount"] == 4
    assert result["gpuType"] == "h200"


def test_tp_times_pp_gives_total_gpu_count():
    spec = {}
    result = apply_hardware_spec(spec, tp_size=4, pp_size=2, dp_size=1, gpu_type="h200")
    assert result["gpuCount"] == 8


def test_tp_times_dp_gives_total_gpu_count():
    spec = {}
    result = apply_hardware_spec(spec, tp_size=8, pp_size=1, dp_size=2, gpu_type="h200")
    assert result["gpuCount"] == 16


def test_tp_times_pp_times_dp_gives_total_gpu_count():
    spec = {}
    result = apply_hardware_spec(spec, tp_size=4, pp_size=2, dp_size=2, gpu_type="h200")
    assert result["gpuCount"] == 16


def test_overrides_wrong_gpu_count_when_gpu_type_already_set():
    # Regression: early-return on gpuType caused submitted gpuCount=1 to be silently kept
    spec = {"gpuCount": 1, "gpuType": "h200"}
    result = apply_hardware_spec(spec, tp_size=8, pp_size=1, dp_size=1, gpu_type="h200")
    assert result["gpuCount"] == 8


def test_all_defaults_gives_gpu_count_1():
    spec = {}
    result = apply_hardware_spec(spec, tp_size=1, pp_size=1, dp_size=1, gpu_type="h200")
    assert result["gpuCount"] == 1


def test_no_gpu_type_returns_empty():
    spec = {"gpuCount": 4}
    result = apply_hardware_spec(spec, tp_size=4, pp_size=1, dp_size=1, gpu_type=None)
    assert result == {}
