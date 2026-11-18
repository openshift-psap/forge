from __future__ import annotations

from types import SimpleNamespace

import pytest

from projects.rhoai.library import deploy as rhoai_deploy


def _oc_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        success=returncode == 0,
    )


def _rhoai_config(*, custom_catalog_overrides: dict | None = None) -> dict:
    custom_catalog = {
        "enabled": True,
        "name": "rhoai-catalog-dev",
        "namespace": "openshift-marketplace",
        "image": "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
        "pull_secret": {
            "vault": {"name": "psap-rhoai-rc", "content": "rhoai_rc.secret"},
        },
    }
    if custom_catalog_overrides:
        custom_catalog.update(custom_catalog_overrides)

    return {
        "custom_catalog": custom_catalog,
        "namespace": "redhat-ods-applications",
        "datasciencecluster_name": "default-dsc",
    }


def test_cleanup_deletes_forge_managed_catalog_sources_by_label(monkeypatch) -> None:
    oc_calls: list[tuple[tuple, dict]] = []

    def fake_oc(*args, **kwargs):
        oc_calls.append((args, kwargs))
        return _oc_result(stdout="catalogsource.operators.coreos.com/rhoai-catalog-dev deleted\n")

    monkeypatch.setattr(rhoai_deploy, "oc", fake_oc)

    rhoai_deploy.cleanup_rhoai_custom_catalog(rhoai=_rhoai_config())

    assert len(oc_calls) == 1
    args, kwargs = oc_calls[0]
    assert args[:2] == ("delete", "catalogsource")
    assert "app.kubernetes.io/managed-by=forge" in args
    assert "openshift-marketplace" in args
    assert "--ignore-not-found=true" in args
    assert kwargs["check"] is False


def test_cleanup_runs_even_when_custom_catalog_disabled(monkeypatch) -> None:
    oc_calls: list[tuple[tuple, dict]] = []

    def fake_oc(*args, **kwargs):
        oc_calls.append((args, kwargs))
        return _oc_result(stdout="No resources found in openshift-marketplace namespace.\n")

    monkeypatch.setattr(rhoai_deploy, "oc", fake_oc)

    rhoai_deploy.cleanup_rhoai_custom_catalog(
        rhoai=_rhoai_config(custom_catalog_overrides={"enabled": False})
    )

    assert len(oc_calls) == 1


def test_cleanup_noop_when_nothing_labeled(monkeypatch) -> None:
    def fake_oc(*args, **kwargs):
        return _oc_result(stdout="No resources found in openshift-marketplace namespace.\n")

    monkeypatch.setattr(rhoai_deploy, "oc", fake_oc)

    # Must not raise: nothing forge-managed is a valid, non-error outcome
    rhoai_deploy.cleanup_rhoai_custom_catalog(rhoai=_rhoai_config())


def test_cleanup_raises_on_oc_failure(monkeypatch) -> None:
    def fake_oc(*args, **kwargs):
        return _oc_result(stderr="forbidden: catalogsources delete is denied", returncode=1)

    monkeypatch.setattr(rhoai_deploy, "oc", fake_oc)

    with pytest.raises(RuntimeError, match="Failed to delete the forge-managed CatalogSource"):
        rhoai_deploy.cleanup_rhoai_custom_catalog(rhoai=_rhoai_config())
