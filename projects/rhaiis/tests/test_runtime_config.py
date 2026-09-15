import unittest

from projects.rhaiis.orchestration.runtime_config import (
    build_guidellm_args,
    build_guidellm_saturation_monitor,
)


class GuideLLMArgumentTests(unittest.TestCase):
    def test_builds_non_enforcing_monitor_configuration(self) -> None:
        result = build_guidellm_saturation_monitor(
            {"enabled": True, "min_seconds": 60, "minimum_ttft": 0.0}
        )

        self.assertEqual(
            result,
            {"enabled": False, "min_seconds": 60, "minimum_ttft": 0.0},
        )

    def test_omits_disabled_monitor(self) -> None:
        self.assertIsNone(build_guidellm_saturation_monitor(None))
        self.assertIsNone(build_guidellm_saturation_monitor({"enabled": False}))

    def test_adds_exact_over_saturation_cli_argument(self) -> None:
        args = build_guidellm_args(
            benchmark_cfg={},
            model_id="example/model",
            data="prompt_tokens=1000,output_tokens=1000",
            rates=[1, 2],
            max_seconds=450,
            over_saturation={"min_seconds": 60, "enabled": False},
        )

        saturation_arg_index = args.index("--over-saturation")
        self.assertEqual(
            args[saturation_arg_index + 1],
            '{"enabled":false,"min_seconds":60}',
        )
        self.assertIn("--max-seconds=450", args)


if __name__ == "__main__":
    unittest.main()
