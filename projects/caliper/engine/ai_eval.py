"""AI agent evaluation JSON (FR-011)."""

from __future__ import annotations

from pathlib import Path

from projects.caliper.engine.parse import run_parse


def run_ai_eval_export(
    *,
    base_dir: Path,
    plugin_module: str,
    plugin: object,
    output: Path,
    use_cache: bool,
    include_label_filter: dict[str, list[str]] | None = None,
    exclude_label_filter: dict[str, list[str]] | None = None,
    verbose_parsing: bool = False,
) -> dict[str, object]:
    """Export AI evaluation payload with structured directories and copied artifacts."""
    import json  # noqa: PLC0415

    model = run_parse(
        base_dir=base_dir,
        plugin_module=plugin_module,
        plugin=plugin,
        use_cache=use_cache,
        include_label_filter=include_label_filter,
        exclude_label_filter=exclude_label_filter,
        verbose_parsing=verbose_parsing,
        show_parameter_matrix=verbose_parsing,  # Only show matrix in verbose mode
    )

    # Build base payload from plugin
    if not hasattr(plugin, "build_ai_data_payload"):
        raise AttributeError(
            f"Plugin {plugin_module} does not support AI evaluation export. "
            "Missing build_ai_data_payload method."
        )

    build = plugin.build_ai_data_payload
    payload = build(model)

    # Create AI evaluation directory structure
    ai_eval_dir = output.parent / "ai_eval"
    ai_eval_dir.mkdir(parents=True, exist_ok=True)

    # Export test entries with artifact copying
    exported_entries = _export_test_entries_with_artifacts_engine(
        model, ai_eval_dir, base_dir, plugin
    )

    # Add exported entries info to payload
    payload["exported_test_entries"] = exported_entries

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    return payload


def _export_test_entries_with_artifacts_engine(
    model, ai_eval_dir: Path, base_dir: Path, plugin
) -> list[dict]:
    """
    Export test entries by creating directories and copying specific artifacts.

    Args:
        model: Unified model containing test results
        ai_eval_dir: Directory where test entries should be exported
        base_dir: Base directory of the test artifacts (test directory)
        plugin: Plugin instance to get artifact file list

    Returns:
        List of exported test entry information
    """
    import json
    import logging
    import shutil

    logger = logging.getLogger(__name__)
    exported_entries = []

    # Get the specific files we want to copy from the plugin
    if hasattr(plugin, "get_ai_eval_artifact_files"):
        target_files = plugin.get_ai_eval_artifact_files(model)
    else:
        # Fallback: copy common test artifacts
        target_files = ["*.log", "*.json", "*.yaml", "*.yml"]

    for idx, record in enumerate(model.unified_result_records):
        # Create directory for this test entry
        test_entry_dir = ai_eval_dir / f"test_entry_{idx:03d}"
        test_entry_dir.mkdir(parents=True, exist_ok=True)

        # Record test entry metadata
        entry_info = {
            "entry_id": f"test_entry_{idx:03d}",
            "test_base_path": str(record.test_base_path),
            "distinguishing_labels": record.distinguishing_labels,
            "copied_files": [],
            "missing_files": [],
        }

        # Copy target files if they exist
        for target_file in target_files:
            # Handle plugin-provided paths directly, expand fallback patterns
            if hasattr(plugin, "get_ai_eval_artifact_files"):
                # Plugin provided specific files - treat as direct paths
                source_file = base_dir / target_file
                if source_file.exists():
                    # Create target directory structure in test entry dir
                    target_path = test_entry_dir / Path(target_file).name
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    try:
                        shutil.copy2(source_file, target_path)
                        entry_info["copied_files"].append(
                            {
                                "source": str(source_file),
                                "target": str(target_path),
                                "size_bytes": source_file.stat().st_size,
                            }
                        )
                        logger.debug(f"Copied {source_file} -> {target_path}")
                    except Exception as e:
                        logger.warning(f"Failed to copy {source_file}: {e}")
                        entry_info["missing_files"].append(
                            {"file": str(source_file), "error": str(e)}
                        )
                else:
                    entry_info["missing_files"].append(
                        {"file": str(source_file), "error": "File does not exist"}
                    )
            else:
                # Fallback mode - expand glob patterns
                import glob

                pattern_path = base_dir / target_file
                matched_files = glob.glob(str(pattern_path))

                if matched_files:
                    # Copy all matched files
                    for matched_file_str in matched_files:
                        matched_file = Path(matched_file_str)
                        target_path = test_entry_dir / matched_file.name
                        target_path.parent.mkdir(parents=True, exist_ok=True)

                        try:
                            shutil.copy2(matched_file, target_path)
                            entry_info["copied_files"].append(
                                {
                                    "source": str(matched_file),
                                    "target": str(target_path),
                                    "size_bytes": matched_file.stat().st_size,
                                }
                            )
                            logger.debug(f"Copied {matched_file} -> {target_path}")
                        except Exception as e:
                            logger.warning(f"Failed to copy {matched_file}: {e}")
                            entry_info["missing_files"].append(
                                {"file": str(matched_file), "error": str(e)}
                            )
                else:
                    # Pattern matched no files
                    entry_info["missing_files"].append(
                        {"file": str(pattern_path), "error": "Pattern matched no files"}
                    )

        # Write entry metadata
        entry_metadata_file = test_entry_dir / "entry_metadata.json"
        with open(entry_metadata_file, "w") as f:
            json.dump(entry_info, f, indent=2)

        exported_entries.append(entry_info)

    return exported_entries
