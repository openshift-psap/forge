from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from projects.core.library import config as core_config
from projects.core.library import env

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"
REFERENCE_DIR = Path(__file__).resolve().parent / "reference_deployments"

# Deployment presets to test
DEPLOYMENT_PRESETS = [
    "cpt-reference-flavors",
]
CONFIG_OVERRIDES = {
    "runtime.kserve.dry_run": True,
    "caliper.postprocess.enabled": False,
    "cpt.kpi.labels.product_version": "RHOAI-XXX",
    "agentic_review.enabled": False,
}

# Check for save deployments mode via environment variable
SAVE_DEPLOYMENTS = os.environ.get("SAVE_DEPLOYMENTS", "false").lower() in ("true", "1", "yes")


@pytest.fixture(autouse=True)
def _reset_project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    # Unset KUBECONFIG as requested
    monkeypatch.delenv("KUBECONFIG", raising=False)
    env.init()
    core_config.project = None
    yield
    core_config.project = None


@pytest.mark.parametrize("preset", DEPLOYMENT_PRESETS)
def test_deployment_preset_generates_expected_llmisvc(preset: str, tmp_path: Path):
    """Test that deployment presets generate the expected LLMISVC manifests."""
    _test_preset_generates_expected_llmisvc(preset, tmp_path)


def _test_preset_generates_expected_llmisvc(preset: str, tmp_path: Path):
    """Helper function to test a preset generates the expected LLMISVC manifest."""
    # Set up environment
    artifact_dir = tmp_path / "artifacts" / preset
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Create variable overrides to ensure dry-run mode
    variable_overrides_path = artifact_dir / "000__ci_metadata" / "variable_overrides.yaml"
    variable_overrides_path.parent.mkdir(parents=True, exist_ok=True)
    variable_overrides_path.write_text(
        yaml.safe_dump(
            CONFIG_OVERRIDES,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    env_vars = os.environ.copy()
    env_vars["ARTIFACT_DIR"] = str(artifact_dir)
    env_vars.pop("KUBECONFIG", None)

    # Run the CI script with the preset
    ci_script = PROJECT_ROOT / "projects" / "llm_d" / "orchestration" / "ci.py"
    cmd = [str(ci_script), "--preset", preset, "test"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env_vars,
            timeout=60,  # 60-second timeout
        )
    except subprocess.TimeoutExpired as e:
        # Show stdout when timeout occurs
        stdout_output = e.stdout.decode() if e.stdout else "No stdout available"
        stderr_output = e.stderr.decode() if e.stderr else "No stderr available"
        pytest.fail(
            f"CI command timed out after 60 seconds for preset {preset}:\n"
            f"Command: {' '.join(cmd)}\n\n"
            f"STDOUT (before timeout):\n{stdout_output}\n\n"
            f"STDERR (before timeout):\n{stderr_output}\n\n"
        )

    # Check that the command succeeded
    if result.returncode != 0:
        pytest.fail(
            f"CI command failed for preset {preset}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n\n"
            f"💡 To save generated deployments as references, use:\n"
            f"   SAVE_DEPLOYMENTS=true python -m pytest {__name__} -v -s"
        )

    # Find all generated LLMISVC manifests
    deployments = _find_generated_llmisvcs(artifact_dir)

    if not deployments:
        pytest.fail(
            f"No generated LLMISVC manifests found in {artifact_dir}\n\n"
            f"💡 To save generated deployments as references, use:\n"
            f"   SAVE_DEPLOYMENTS=true python -m pytest {__name__} -v -s"
        )

    # If saving deployments, save them and skip comparison
    if SAVE_DEPLOYMENTS:
        _save_deployments(preset, deployments)
        pytest.skip(f"Saved {len(deployments)} deployments for preset {preset}")
        return

    # Test each deployment against its reference
    for deployment in deployments:
        profile_name = deployment["profile_name"]
        generated_manifest = deployment["manifest_path"]

        print(f"Testing deployment for profile: {profile_name}")

        if not generated_manifest.exists():
            pytest.fail(
                f"Generated LLMISVC manifest not found at {generated_manifest}\n\n"
                f"💡 To save generated deployments as references, use:\n"
                f"   SAVE_DEPLOYMENTS=true python -m pytest {__name__} -v -s"
            )

        # Load the generated manifest
        with generated_manifest.open("r", encoding="utf-8") as f:
            generated_content = yaml.safe_load(f)

        # Determine reference manifest path
        # Look in preset-specific directory structure: preset/deployment-{profile_name}/llmisvc.yaml
        reference_preset_dir = REFERENCE_DIR / preset
        reference_deployment_dir = reference_preset_dir / f"deployment-{profile_name}"
        reference_manifest = reference_deployment_dir / "llmisvc.yaml"

        if not reference_manifest.exists():
            pytest.fail(
                f"Reference manifest not found at {reference_manifest}\n\n"
                f"💡 To save generated deployments as references, use:\n"
                f"   SAVE_DEPLOYMENTS=true python -m pytest {__name__} -v -s"
            )

        with reference_manifest.open("r", encoding="utf-8") as f:
            reference_content = yaml.safe_load(f)

        # Compare the manifests
        if generated_content != reference_content:
            # Generate a diff for better error reporting
            import difflib

            generated_yaml = yaml.dump(generated_content, default_flow_style=False, sort_keys=True)
            reference_yaml = yaml.dump(reference_content, default_flow_style=False, sort_keys=True)

            diff = list(
                difflib.unified_diff(
                    reference_yaml.splitlines(keepends=True),
                    generated_yaml.splitlines(keepends=True),
                    fromfile=f"reference/{preset}/deployment-{profile_name}/llmisvc.yaml",
                    tofile=f"generated/{preset}/{profile_name}/llmisvc.yaml",
                    lineterm="",
                )
            )

            pytest.fail(
                f"Generated LLMISVC does not match reference for profile {profile_name} in preset {preset}:\n{''.join(diff)}\n\n"
                f"💡 To update reference deployments with new output, use:\n"
                f"   SAVE_DEPLOYMENTS=true python -m pytest {__name__} -v -s"
            )


def _save_deployments(preset: str, deployments: list[dict[str, Path]]):
    """Save generated deployments as reference manifests."""
    print(f"\nSaving {len(deployments)} deployments for preset {preset}:")

    for deployment in deployments:
        profile_name = deployment["profile_name"]
        generated_manifest = deployment["manifest_path"]

        # Create reference directory for this preset and deployment profile
        reference_preset_dir = REFERENCE_DIR / preset
        reference_deployment_dir = reference_preset_dir / f"deployment-{profile_name}"
        reference_deployment_dir.mkdir(parents=True, exist_ok=True)

        # Copy the generated manifest to the reference location
        reference_manifest = reference_deployment_dir / "llmisvc.yaml"

        with generated_manifest.open("r", encoding="utf-8") as f:
            content = f.read()

        with reference_manifest.open("w", encoding="utf-8") as f:
            f.write(content)

        print(f"  Saved {profile_name} → {reference_manifest}")

    print(f"Reference deployments saved to: {reference_preset_dir}")


def _find_generated_llmisvcs(artifact_dir: Path) -> list[dict[str, Path]]:
    """Find all generated LLMISVC manifests in the artifact directory.

    Returns:
        List of dictionaries with 'profile_name' and 'manifest_path' keys
    """
    # Look for the pattern: $ARTIFACT_DIR/**/deploy_llmisvc/src/llminferenceservice.yaml
    deploy_llmisvc_dirs = list(artifact_dir.glob("**/*__deploy_llmisvc"))

    if not deploy_llmisvc_dirs:
        raise FileNotFoundError(f"No deploy_llmisvc directory found in {artifact_dir}")

    deployments = []
    for deploy_dir in deploy_llmisvc_dirs:
        llmisvc_path = deploy_dir / "src" / "llminferenceservice.yaml"
        if llmisvc_path.exists():
            # Read the manifest and extract profile name from annotations
            with llmisvc_path.open("r", encoding="utf-8") as f:
                manifest = yaml.safe_load(f)

            ANNOTATION = "forge.openshift.io/deployment-profile"
            profile_name = manifest.get("metadata", {}).get("annotations", {}).get(ANNOTATION)

            if not profile_name:
                raise ValueError(f"Annotation '{ANNOTATION}' missing in {llmisvc_path}")

            deployments.append({"profile_name": profile_name, "manifest_path": llmisvc_path})

    return deployments


def _find_generated_llmisvc(artifact_dir: Path) -> Path:
    """Find the generated LLMISVC manifest in the artifact directory."""
    deployments = _find_generated_llmisvcs(artifact_dir)

    if len(deployments) == 1:
        return deployments[0]["manifest_path"]
    elif len(deployments) > 1:
        raise ValueError(
            f"Multiple deploy_llmisvc directories found in {artifact_dir}. "
            f"Use _find_generated_llmisvcs() instead for multi-deployment presets."
        )
    else:
        raise FileNotFoundError(f"No deploy_llmisvc directory found in {artifact_dir}")
