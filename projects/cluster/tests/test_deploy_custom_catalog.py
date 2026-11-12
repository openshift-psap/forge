from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import jinja2
import yaml

TEMPLATES_DIR = Path(__file__).parents[1] / "toolbox" / "deploy_custom_catalog" / "templates"


def _render_catalog_source(**overrides) -> dict:
    values = {
        "catalog_source_name": "rhoai-catalog-dev",
        "catalog_namespace": "openshift-marketplace",
        "catalog_image": "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
        "display_name": "Red Hat OpenShift AI",
        "publisher": "",
    }
    values.update(overrides)
    args = SimpleNamespace(**values)
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("catalogsource.yaml.j2").render(args=args)
    return yaml.safe_load(rendered)


def test_catalog_source_is_marked_as_forge_managed() -> None:
    manifest = _render_catalog_source()

    assert manifest["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "forge"


def test_catalog_source_metadata() -> None:
    manifest = _render_catalog_source()

    assert manifest["apiVersion"] == "operators.coreos.com/v1alpha1"
    assert manifest["kind"] == "CatalogSource"
    assert manifest["metadata"]["name"] == "rhoai-catalog-dev"
    assert manifest["metadata"]["namespace"] == "openshift-marketplace"
    assert manifest["spec"]["image"].startswith("quay.io/rhoai/")


def test_catalog_source_publisher_omitted_when_empty() -> None:
    manifest = _render_catalog_source()

    assert "publisher" not in manifest["spec"]

    manifest = _render_catalog_source(publisher="RHOAI Development Catalog")

    assert manifest["spec"]["publisher"] == "RHOAI Development Catalog"
