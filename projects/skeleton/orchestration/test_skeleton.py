import json
import logging
import pathlib
import time
from datetime import UTC, datetime

import yaml

from projects.caliper.engine.kpi.dataclasses import CompletionData, TimingData
from projects.core.library import config, env
from projects.core.library.postprocess import create_test_metadata, run_and_postprocess
from projects.skeleton.toolbox.cluster_info.main import run as cluster_info

logger = logging.getLogger(__name__)


def get_iso_timestamp() -> str:
    """Get current timestamp in ISO format with Z timezone."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def create_seed_data_with_completion(
    test_start_time: str,
    test_end_time: str,
    benchmark_start_time: str,
    benchmark_end_time: str,
    cluster_info_start_time: str | None,
    cluster_info_end_time: str | None,
    success: bool,
    message: str,
) -> None:
    """Create seed data with complete timing and completion information."""

    # Build timing structure using dataclasses
    timing_data = TimingData()
    timing_data.set_phase("test", test_start_time, test_end_time)
    timing_data.set_phase("benchmark", benchmark_start_time, benchmark_end_time)

    # Add cluster info timing if available
    if cluster_info_start_time and cluster_info_end_time:
        timing_data.set_phase("cluster_info", cluster_info_start_time, cluster_info_end_time)

    # Build completion data using dataclasses
    completion_data = CompletionData(success=success, message=message)

    # Create the seed data with timing and completion
    with env.NextArtifactDir("skeleton_seed_data_for_caliper_postprocessing"):
        seed_skeleton_caliper_artifacts_with_data(timing_data, completion_data)


def seed_skeleton_caliper_artifacts_with_data(
    timing_data: TimingData, completion_data: CompletionData
) -> pathlib.Path:
    """Create minimal Caliper inputs with timing and completion data."""
    demo_dir = env.ARTIFACT_DIR

    skeleton_config = config.project.get_config("skeleton", print=False)

    # Base labels for all scenarios
    base_labels = {
        "test": "skeleton",
        "deep_testing": str(skeleton_config.get("deep_testing", False)),
        "collect_cluster_info": str(skeleton_config.get("collect_cluster_info", True)),
    }

    FAKE_DATA = (
        ("smoke", 120.5, 8.2),
        ("load", 87.0, 22.1),
    )

    for scenario, throughput, latency_ms in FAKE_DATA:
        dest_dir = demo_dir / scenario
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Combine base labels with scenario-specific label
        scenario_labels = {**base_labels, "scenario": scenario}

        # Create metadata with timing and completion data using create_test_metadata
        create_test_metadata(
            dest_dir,
            scenario_labels,
            timing=timing_data,
            completion=completion_data,
            dump_config=False,
        )
        logger.info(f"Created {scenario} metadata with timing and completion data")

        (dest_dir / "metrics.json").write_text(
            json.dumps({"throughput": throughput, "latency_ms": latency_ms}),
            encoding="utf-8",
        )

    logger.info("Seeded Caliper demo tree under %s with complete timing data", demo_dir)
    return demo_dir


def test():
    """Main test function that wraps do_test() with outcome postprocessing."""
    return run_and_postprocess(do_test)


def skeleton_take_time():
    test_duration = config.project.get_config("skeleton.test.duration_seconds")
    logger.info(f"Test duration: {test_duration} seconds")

    start_time = time.time()
    minute_count = 0

    logger.info(f"Starting {test_duration}s test loop...")

    while True:
        remaining = test_duration - (time.time() - start_time)
        wait_time = min(60.0, remaining)
        if wait_time <= 0:
            break
        time.sleep(wait_time)

        minute_count += 1
        elapsed = time.time() - start_time
        remaining = test_duration - elapsed

        if minute_count % 60 == 0:
            elapsed_h = int(elapsed // 3600)
            elapsed_m = int((elapsed % 3600) // 60)
            remaining_h = int(max(0, remaining) // 3600)
            remaining_m = int((max(0, remaining) % 3600) // 60)
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.info(
                f"Elapsed: {elapsed_h}h{elapsed_m:02d}m, "
                f"Remaining: {remaining_h}h{remaining_m:02d}m, "
                f"Timestamp: {timestamp}"
            )
        else:
            logger.info(".")

    elapsed_total = time.time() - start_time
    logger.info(f"Completed test loop in {elapsed_total:.1f}s ({minute_count} minutes)")


def do_test():
    logger.info("=== Skeleton Project Test Phase ===")

    # Capture test timing
    test_start_time = get_iso_timestamp()

    try:
        if config.project.get_config("skeleton.deep_testing"):
            logger.info("Running the (fake) deep testing ...")
        else:
            logger.info("Running the (fake) light testing ...")

        skeleton_config = config.project.get_config("skeleton", print=False)

        yaml_cfg = yaml.dump(
            {"skeleton": skeleton_config},
            indent=4,
            default_flow_style=False,
            sort_keys=False,
        )
        logger.info("")
        logger.info(f"Fake test configuration:\n{yaml_cfg}")

        if config.project.get_config("skeleton.test.fail"):
            raise RuntimeError("Configuration requested a failure")

        # Capture benchmark timing
        benchmark_start_time = get_iso_timestamp()
        try:
            skeleton_take_time()
        finally:
            benchmark_end_time = get_iso_timestamp()

        cluster_info_start_time = None
        cluster_info_end_time = None

        if config.project.get_config("skeleton.collect_cluster_info"):
            # Add timing around cluster info gathering
            cluster_info_start_time = get_iso_timestamp()
            try:
                # Demonstrate calling a toolbox from orchestration
                logger.info("Running cluster information toolbox...")

                result = cluster_info(output_format="text")

                if not result:
                    logger.warning("⚠️ Cluster information gathering didn't work")
                    test_end_time = get_iso_timestamp()
                    create_seed_data_with_completion(
                        test_start_time,
                        test_end_time,
                        benchmark_start_time,
                        benchmark_end_time,
                        cluster_info_start_time,
                        get_iso_timestamp(),
                        False,
                        "Cluster information gathering failed",
                    )
                    return 1

                cluster_nodes_dest = getattr(result, "cluster_nodes_dest", None)
                if not cluster_nodes_dest:
                    logger.warning(
                        "⚠️ Cluster information gathering didn't generate the cluster node file"
                    )
                    test_end_time = get_iso_timestamp()
                    create_seed_data_with_completion(
                        test_start_time,
                        test_end_time,
                        benchmark_start_time,
                        benchmark_end_time,
                        cluster_info_start_time,
                        get_iso_timestamp(),
                        False,
                        "Cluster information gathering incomplete - no node file generated",
                    )
                    return 1

            finally:
                cluster_info_end_time = get_iso_timestamp()

            logger.info("✅ Cluster information gathering completed successfully")

        test_end_time = get_iso_timestamp()

        # Determine completion message based on cluster info setting
        if not config.project.get_config("skeleton.collect_cluster_info"):
            completion_message = "Test completed successfully (cluster info disabled)"
        else:
            completion_message = "Test completed successfully"

        # Create seed data with all timing and completion information
        create_seed_data_with_completion(
            test_start_time,
            test_end_time,
            benchmark_start_time,
            benchmark_end_time,
            cluster_info_start_time,
            cluster_info_end_time,
            True,
            completion_message,
        )

        return 0

    except Exception as e:
        logger.exception("❌ Test failed with exception")
        test_end_time = get_iso_timestamp()

        # Create seed data even after failure so timing data is preserved
        try:
            create_seed_data_with_completion(
                test_start_time,
                test_end_time,
                locals().get("benchmark_start_time"),
                locals().get("benchmark_end_time"),
                locals().get("cluster_info_start_time"),
                locals().get("cluster_info_end_time"),
                False,
                f"Test failed: {e}",
            )
        except Exception as seed_error:
            logger.warning(f"Failed to create seed data after test failure: {seed_error}")
        raise
    logger.info(f"Check {cluster_nodes_dest.parent} directory for detailed cluster information.")

    return 0


def resolve_hardware_request(hardware_spec: dict):
    """
    Resolve hardware requirements for FournosJob based on skeleton project configuration.

    This is a stub implementation. Update spec.hardware based on project configuration.

    Args:
        hardware_spec: The current spec.hardware dict from the FournosJob. This object should be updated.

    """
    logger.info("Hardware resolution: stub implementation - no changes made")

    # Stub implementation - could be extended to:
    # - Read hardware config from project configuration
    # - Set hardware requirements based on workload needs
    # - Handle different hardware profiles (GPU, CPU, memory requirements)
    # - Example: return {"gpu": {"type": "nvidia-tesla-v100", "count": 1}, "memory": "32Gi"}

    # hardware_spec["gpuType"] = "h200"
    # hardware_spec["gpuCount"] = 4

    return hardware_spec
