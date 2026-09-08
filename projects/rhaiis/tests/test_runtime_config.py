from projects.rhaiis.orchestration.runtime_config import build_guidellm_args


def test_build_guidellm_args_includes_per_rate_warmup() -> None:
    args = build_guidellm_args(
        benchmark_cfg={"args": {"rate_type": "concurrent"}},
        model_id="example/model",
        data="prompt_tokens=1000,output_tokens=1000",
        rates=[1, 2, 5],
        max_seconds=120,
        warmup=15,
    )

    assert args == [
        "--rate-type=concurrent",
        "--model=example/model",
        "--data=prompt_tokens=1000,output_tokens=1000",
        "--rate=1,2,5",
        "--max-seconds=120",
        "--warmup=15",
    ]
