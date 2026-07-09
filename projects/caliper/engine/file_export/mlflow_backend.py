"""Log artifacts to MLflow."""

from __future__ import annotations

import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from projects.caliper.engine.file_export.mlflow_secrets import (
    assert_tracking_uri_has_no_userinfo,
    mlflow_connection_env,
)
from projects.core.library import env

logger = logging.getLogger(__name__)


def _mlflow_artifact_subdir(artifact_root: Path, file_path: Path) -> str | None:
    """
    Parent directory of ``file_path`` relative to ``artifact_root``, as a POSIX path.

    MLflow ``log_artifact(local, artifact_path=...)`` places the file basename under that
    subdirectory, preserving hierarchy when ``artifact_root`` is a directory.
    """
    root = artifact_root.resolve()
    fp = file_path.resolve()
    if root.is_file():
        return None
    rel = fp.relative_to(root)
    parent = rel.parent
    if parent == Path("."):
        return None
    return parent.as_posix()


def _mlflow_ui_links(
    tracking_uri: str, experiment_id: str, run_id: str, workspace: str | None = None
) -> tuple[str | None, str | None]:
    """Hash-based MLflow UI URLs (http/https tracking servers only)."""
    base = tracking_uri.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None, None
    eid = str(experiment_id)
    rid = str(run_id)
    qs = f"?workspace={workspace}" if workspace else ""
    run_url = f"{base}/#/experiments/{eid}/runs/{rid}/artifacts{qs}"
    exp_url = f"{base}/#/experiments/{eid}{qs}"
    return run_url, exp_url


def _capture_mlflow_run_metadata(tracking_uri: str, workspace: str | None = None) -> dict[str, Any]:
    """Snapshot active run + experiment (call inside an active mlflow.start_run() context)."""
    import mlflow

    run = mlflow.active_run()
    if run is None:
        return {}
    client = mlflow.tracking.MlflowClient()
    rid = run.info.run_id
    eid = str(run.info.experiment_id)
    full = client.get_run(rid)
    run_name = getattr(full.info, "run_name", None) or ""
    if not run_name and full.data and full.data.tags:
        run_name = full.data.tags.get("mlflow.runName") or ""
    exp = client.get_experiment(eid)
    experiment_name = exp.name
    run_url, exp_url = _mlflow_ui_links(tracking_uri, eid, rid, workspace=workspace)
    out: dict[str, Any] = {
        "run_id": rid,
        "experiment_id": eid,
        "run_name": run_name,
        "experiment_name": experiment_name,
        "tracking_uri": tracking_uri,
    }
    if run_url:
        out["run_url"] = run_url
    if exp_url:
        out["experiment_url"] = exp_url
    return out


def _git_cwd_for_source(artifact_root: Path) -> Path:
    """Directory used for ``git -C`` when the export root is a file or a tree."""
    return artifact_root if artifact_root.is_dir() else artifact_root.parent


def _git_remote_repo_name(cwd: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    url = r.stdout.strip()
    tail = url.rstrip("/").split("/")[-1].split(":")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or None


def _git_source_metadata(artifact_root: Path) -> dict[str, str]:
    """
    Best-effort git name + HEAD commit for the tree containing ``artifact_root``.

    Name prefers ``origin`` URL basename, else the git worktree top-level directory name.
    """
    out: dict[str, str] = {}
    cwd = _git_cwd_for_source(artifact_root)
    try:
        chk = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return out
    if chk.returncode != 0 or chk.stdout.strip() != "true":
        return out
    try:
        head = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return out
    if head.returncode == 0 and head.stdout.strip():
        out["source_commit"] = head.stdout.strip()
    name = _git_remote_repo_name(cwd)
    if not name:
        try:
            top = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            top = None
        if top and top.returncode == 0 and top.stdout.strip():
            name = Path(top.stdout.strip()).name
    if name:
        out["source_name"] = name
    return out


def merge_run_metadata_with_git_source(
    artifact_root: Path,
    run_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Fill ``source_name`` / ``source_commit`` from git when missing, then apply YAML on top.

    Explicit YAML (or CLI-merged) values override auto-detected git.
    """
    auto = _git_source_metadata(artifact_root)
    if not run_metadata:
        return dict(auto) if auto else None
    merged: dict[str, Any] = {**auto, **run_metadata}
    return merged if merged else None


def _apply_run_metadata(run_metadata: dict[str, Any] | None) -> None:
    """
    Set run description and tags from ``mlflow-config.yaml`` (non-secret section).

    Uses ``mlflow.note.content`` for the description (MLflow UI notes field).
    ``source_script`` (preferred) or ``source_name`` sets ``mlflow.source.name`` (e.g. script file).
    ``source_commit`` uses ``mlflow.source.git.commit`` when set (or auto-detected from git).
    User-supplied ``parameters`` are logged first with ``mlflow.log_param``.
    ``metrics`` are logged with ``mlflow.log_metric`` (Run → Metrics).
    """
    if not run_metadata:
        return
    import mlflow

    params = run_metadata.get("parameters")

    if isinstance(params, dict):
        for pk, pv in params.items():
            mlflow.log_param(str(pk), "" if pv is None else str(pv))

    mets = run_metadata.get("metrics")
    if isinstance(mets, dict):
        for mk, mv in mets.items():
            mlflow.log_metric(str(mk), float(mv))

    if desc := run_metadata.get("description"):
        mlflow.set_tag("mlflow.note.content", str(desc))

    if tags := run_metadata.get("tags"):
        if isinstance(tags, dict):
            for k, v in tags.items():
                mlflow.set_tag(str(k), str(v) if v is not None else "")

    src_script = run_metadata.get("source_script")
    src_name = run_metadata.get("source_name")
    if src_script:
        mlflow.set_tag("mlflow.source.name", str(src_script))
    elif src_name:
        mlflow.set_tag("mlflow.source.name", str(src_name))
    if sc := run_metadata.get("source_commit"):
        mlflow.set_tag("mlflow.source.git.commit", str(sc))


def _resolve_log_model_path(artifact_root: Path, path_str: str) -> Path:
    """Resolve ``path`` from config: absolute paths as-is, else relative to ``artifact_root``."""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (artifact_root / p).resolve()


def _apply_log_model(
    artifact_root: Path, run_metadata: dict[str, Any] | None, *, verbose: bool
) -> None:
    """
    Call ``mlflow.<flavor>.log_model`` from optional ``log_model`` config (serialized file on disk).

    Loading pickle/joblib can execute arbitrary code; use only trusted files.
    """
    if not run_metadata:
        return
    block = run_metadata.get("log_model")
    if not block:
        return
    if not isinstance(block, dict):
        return
    flavor = str(block.get("flavor", "")).strip().lower()
    path_raw = block.get("path")
    if not path_raw:
        return
    artifact_path = (block.get("artifact_path") or "model").strip() or "model"
    registered_model_name = block.get("registered_model_name")
    loader = str(block.get("loader") or "joblib").strip().lower()

    resolved = _resolve_log_model_path(artifact_root, str(path_raw))
    if not resolved.is_file():
        raise FileNotFoundError(f"log_model path does not exist or is not a file: {resolved}")

    import mlflow

    if flavor == "sklearn":
        if loader not in ("joblib", "pickle"):
            raise ValueError(
                f"log_model.loader must be 'joblib' or 'pickle' for sklearn, got {loader!r}"
            )
        if loader == "joblib":
            try:
                import joblib
            except ImportError as e:
                raise RuntimeError(
                    "log_model with loader=joblib requires the joblib package "
                    "(install with: pip install joblib)"
                ) from e
            model = joblib.load(resolved)
        else:
            import pickle

            with resolved.open("rb") as f:
                model = pickle.load(f)

        import mlflow.sklearn

        kw: dict[str, Any] = {}
        rn = (registered_model_name or "").strip()
        if rn:
            kw["registered_model_name"] = rn
        if verbose:
            reg = kw.get("registered_model_name")
            extra = f", registry={reg!r}" if reg else ""
            logger.info(
                f"mlflow.sklearn.log_model({resolved.name!r} -> artifact {artifact_path!r}{extra})"
            )
        mlflow.sklearn.log_model(model, artifact_path, **kw)
        return

    raise ValueError(f"Unsupported log_model.flavor {flavor!r}; supported values: sklearn")


def _parallel_workers(requested: int, n_files: int) -> int:
    if n_files <= 0:
        return 1
    return max(1, min(requested, n_files))


def _upload_mlflow_files_parallel(
    *,
    client: Any,
    run_id: str,
    file_paths: list[Path],
    artifact_root: Path,
    upload_workers: int,
    verbose: bool,
) -> None:
    """Upload files using ``MlflowClient.log_artifact`` (safe for concurrent HTTP)."""
    workers = _parallel_workers(upload_workers, len(file_paths))
    lock = threading.Lock()

    def _one(p: Path) -> None:
        subdir = _mlflow_artifact_subdir(artifact_root, p)
        if verbose:
            ar = artifact_root.resolve()
            pr = p.resolve()
            if ar.is_file():
                rel_name = p.name
            else:
                rel_name = pr.relative_to(ar).as_posix()
            with lock:
                logger.debug(f"log artifact {p} -> {rel_name}")
        client.log_artifact(run_id, str(p), artifact_path=subdir)

    if workers <= 1:
        for p in file_paths:
            _one(p)
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, p) for p in file_paths]
        for fut in as_completed(futures):
            fut.result()


def log_artifacts(
    *,
    artifact_root: Path,
    paths: list[Path],
    tracking_uri: str | None,
    experiment: str | None,
    run_id: str | None,
    run_name: str | None = None,
    insecure_tls: bool = False,
    connection: dict[str, Any] | None = None,
    verbose: bool = False,
    upload_workers: int = 10,
    run_metadata: dict[str, Any] | None = None,
    workspace: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    try:
        import mlflow
    except ImportError as e:
        raise RuntimeError(
            "mlflow is required for MLflow export. Install with: pip install -e '.[caliper]'"
        ) from e

    def _run(uri: str | None) -> tuple[str, dict[str, Any] | None]:
        import os

        if uri:
            assert_tracking_uri_has_no_userinfo(uri)
        insecure = insecure_tls or bool(connection and connection.get("insecure_tls"))
        if insecure:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        if workspace:
            os.environ["MLFLOW_WORKSPACE"] = workspace
            if verbose:
                logger.info("Set MLFLOW_WORKSPACE=%s", workspace)
        if uri:
            mlflow.set_tracking_uri(uri)
        if experiment:
            mlflow.set_experiment(experiment)
        file_paths = [p for p in paths if p.is_file()]
        workers = _parallel_workers(upload_workers, len(file_paths))
        if verbose:
            n = len(file_paths)
            logger.info(
                f"MLflow upload starting ({n} file(s), workers={workers}, "
                f"experiment={experiment or 'default'})"
            )

            # Save file list to artifact directory to avoid log spam
            try:
                if env.ARTIFACT_DIR:
                    upload_list_file = Path(env.ARTIFACT_DIR) / "mlflow_upload_files.txt"
                    with open(upload_list_file, "w") as f:
                        f.write("MLflow Upload File List\n")
                        f.write("======================\n")
                        f.write(f"Experiment: {experiment or 'default'}\n")
                        f.write(f"Total files: {n}\n")
                        f.write(f"Workers: {workers}\n")
                        f.write(f"Artifact root: {artifact_root}\n\n")

                        for file_path in file_paths:
                            # Calculate relative path for display
                            try:
                                rel_path = file_path.relative_to(artifact_root)
                            except ValueError:
                                rel_path = file_path
                            f.write(f"{file_path} -> {rel_path}\n")

                    logger.info(f"MLflow upload file list saved to: {upload_list_file}")
            except Exception as e:
                logger.warning(f"Failed to save MLflow upload file list: {e}")

        effective_meta = merge_run_metadata_with_git_source(artifact_root, run_metadata)

        start_kw: dict[str, Any] = {}
        if run_id:
            start_kw["run_id"] = run_id
        elif run_name:
            start_kw["run_name"] = run_name

        meta: dict[str, Any] | None = None
        client = mlflow.tracking.MlflowClient()
        with mlflow.start_run(**start_kw):
            rid = mlflow.active_run().info.run_id
            _apply_run_metadata(effective_meta)
            _apply_log_model(artifact_root, effective_meta, verbose=verbose)

            _log_metrics_and_params_from_tree(artifact_root)

            _upload_mlflow_files_parallel(
                client=client,
                run_id=rid,
                file_paths=file_paths,
                artifact_root=artifact_root,
                upload_workers=upload_workers,
                verbose=verbose,
            )
            tu = mlflow.get_tracking_uri() or ""
            meta = _capture_mlflow_run_metadata(tu, workspace=workspace)
        if verbose:
            logger.info(f"MLflow upload finished ({mlflow.get_tracking_uri()})")
        return f"mlflow:{mlflow.get_tracking_uri()}", meta

    if connection is not None:
        with mlflow_connection_env(connection):
            return _run(tracking_uri or connection.get("tracking_uri"))
    return _run(tracking_uri)


def _load_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning an empty dict on error."""
    import json

    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s", path, e)
        return {}


def _log_metrics_and_params_from_tree(artifact_root: Path) -> None:
    """Find metrics.json/parameters.json under __test_labels__.yaml-marked dirs and log them."""
    import mlflow

    for marker in sorted(artifact_root.rglob("__test_labels__.yaml")):
        if not marker.is_file():
            continue
        run_dir = marker.parent

        mf = run_dir / "metrics.json"
        if mf.is_file():
            for k, v in _load_json_file(mf).items():
                if isinstance(v, int | float) and not isinstance(v, bool):
                    mlflow.log_metric(str(k), float(v))

        pf = run_dir / "parameters.json"
        if pf.is_file():
            for k, v in _load_json_file(pf).items():
                mlflow.log_param(str(k), "" if v is None else str(v))


def log_multi_run_artifacts(
    *,
    all_artifact_paths: list[Path],
    artifact_root: Path,
    run_dirs: list[Path],
    metrics_file: str,
    parameters_file: str,
    tracking_uri: str | None,
    experiment: str | None,
    parent_run_name: str | None = None,
    insecure_tls: bool = False,
    connection: dict[str, Any] | None = None,
    verbose: bool = False,
    upload_workers: int = 10,
    run_metadata: dict[str, Any] | None = None,
    workspace: str | None = None,
    child_run_names: dict[Path, str] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Create a parent MLflow run with all artifacts and nested child runs per test directory.

    The parent run receives every artifact.  Each child run receives only the
    files that live under its own ``run_dir`` subtree, so artifacts are never
    duplicated across children.
    If ``child_run_names`` is provided, those names are used instead of ``run_dir.name``.
    """
    try:
        import mlflow
    except ImportError as e:
        raise RuntimeError(
            "mlflow is required for MLflow export. Install with: pip install -e '.[caliper]'"
        ) from e

    def _run(uri: str | None) -> tuple[str, dict[str, Any] | None]:
        import os

        if uri:
            assert_tracking_uri_has_no_userinfo(uri)
        insecure = insecure_tls or bool(connection and connection.get("insecure_tls"))
        if insecure:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        if workspace:
            os.environ["MLFLOW_WORKSPACE"] = workspace
            if verbose:
                logger.info("Set MLFLOW_WORKSPACE=%s", workspace)
        if uri:
            mlflow.set_tracking_uri(uri)
        if experiment:
            mlflow.set_experiment(experiment)

        file_paths = [p for p in all_artifact_paths if p.is_file()]
        effective_meta = merge_run_metadata_with_git_source(artifact_root, run_metadata)

        if verbose:
            logger.info(
                "Multi-run export: %d artifact file(s), %d test run(s), experiment=%s",
                len(file_paths),
                len(run_dirs),
                experiment or "default",
            )

        parent_meta: dict[str, Any] | None = None
        child_runs_meta: list[dict[str, Any]] = []
        client = mlflow.tracking.MlflowClient()

        start_kw: dict[str, Any] = {}
        if parent_run_name:
            start_kw["run_name"] = parent_run_name

        with mlflow.start_run(**start_kw) as parent:
            parent_rid = parent.info.run_id
            _apply_run_metadata(effective_meta)

            _upload_mlflow_files_parallel(
                client=client,
                run_id=parent_rid,
                file_paths=file_paths,
                artifact_root=artifact_root,
                upload_workers=upload_workers,
                verbose=verbose,
            )

            mlflow.set_tag("forge.multi_run", "true")
            mlflow.set_tag("forge.child_count", str(len(run_dirs)))

            tu = mlflow.get_tracking_uri() or ""
            eid = str(parent.info.experiment_id)
            parent_url, _ = _mlflow_ui_links(tu, eid, parent_rid, workspace=workspace)
            if parent_url:
                logger.info("Parent run: %s", parent_url)

            for run_dir in sorted(run_dirs):
                child_name = (
                    child_run_names.get(run_dir, run_dir.name) if child_run_names else run_dir.name
                )

                resolved_run_dir = run_dir.resolve()
                child_files = [
                    p for p in file_paths if p.resolve().is_relative_to(resolved_run_dir)
                ]

                with mlflow.start_run(run_name=child_name, nested=True):
                    child_rid = mlflow.active_run().info.run_id

                    mf = run_dir / metrics_file
                    if mf.is_file():
                        for k, v in _load_json_file(mf).items():
                            if isinstance(v, int | float) and not isinstance(v, bool):
                                mlflow.log_metric(str(k), float(v))

                    pf = run_dir / parameters_file
                    if pf.is_file():
                        for k, v in _load_json_file(pf).items():
                            mlflow.log_param(str(k), "" if v is None else str(v))

                    _upload_mlflow_files_parallel(
                        client=client,
                        run_id=child_rid,
                        file_paths=child_files,
                        artifact_root=artifact_root,
                        upload_workers=upload_workers,
                        verbose=verbose,
                    )

                    if verbose:
                        logger.info(
                            "  Child run %s: %d artifact(s) (of %d total)",
                            child_name,
                            len(child_files),
                            len(file_paths),
                        )
                    child_url, _ = _mlflow_ui_links(tu, eid, child_rid, workspace=workspace)
                    if child_url:
                        logger.info("  Child run %s: %s", child_name, child_url)

                    child_entry: dict[str, Any] = {
                        "run_id": child_rid,
                        "run_name": child_name,
                    }
                    if child_url:
                        child_entry["run_url"] = child_url
                    child_runs_meta.append(child_entry)

            parent_meta = _capture_mlflow_run_metadata(tu, workspace=workspace)
            if child_runs_meta:
                parent_meta["child_runs"] = child_runs_meta

        if verbose:
            logger.info("MLflow multi-run upload finished (%s)", mlflow.get_tracking_uri())
        return f"mlflow:{mlflow.get_tracking_uri()}", parent_meta

    if connection is not None:
        with mlflow_connection_env(connection):
            return _run(tracking_uri or connection.get("tracking_uri"))
    return _run(tracking_uri)


def update_artifacts(
    *,
    run_id: str,
    files: dict[Path, str | None] | list[Path],
    tracking_uri: str | None = None,
    connection: dict[str, Any] | None = None,
) -> None:
    """Re-upload multiple files to an existing MLflow run, replacing previous copies.

    Args:
        run_id: The MLflow run ID to update
        files: Either a dict mapping file paths to artifact paths (None for root),
               or a list of file paths (all uploaded to root)
        tracking_uri: MLflow tracking URI
        connection: MLflow connection configuration

    Intended to be called after all post-export work (notifications, etc.) completes,
    so uploaded files contain the full session output.
    """
    try:
        import mlflow
    except ImportError as e:
        raise RuntimeError(
            "mlflow is required for run update. Install with: pip install -e '.[caliper]'"
        ) from e

    # Normalize input to dict format
    if isinstance(files, list):
        files_dict = dict.fromkeys(files)
    else:
        files_dict = files

    def _upload(uri: str | None) -> None:
        import os

        if uri:
            assert_tracking_uri_has_no_userinfo(uri)
            mlflow.set_tracking_uri(uri)

        os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"

        for handler in logging.getLogger().handlers:
            handler.flush()

        client = mlflow.tracking.MlflowClient()

        # Upload each file to its specified artifact path
        for file_path, artifact_path in files_dict.items():
            if file_path.exists():
                client.log_artifact(run_id, str(file_path), artifact_path=artifact_path)

    if connection is not None:
        with mlflow_connection_env(connection):
            _upload(tracking_uri or connection.get("tracking_uri"))
    else:
        _upload(tracking_uri)
