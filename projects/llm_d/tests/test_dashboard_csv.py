from __future__ import annotations

import csv
import io

import pytest

from projects.llm_d.orchestration import dashboard_csv
from projects.llm_d.postprocess.llm_d.csv_dashboard import DASHBOARD_FIELDNAMES

# Header of s3://psap-dashboard-data/staging/llmd-dashboard/llmd-dashboard.csv
STAGING_DASHBOARD_COLUMNS = [
    "run",
    "accelerator",
    "model",
    "version",
    "prompt toks",
    "output toks",
    "TP",
    "DP",
    "EP",
    "replicas",
    "prefill_pod_count",
    "decode_pod_count",
    "router_config",
    "measured concurrency",
    "intended concurrency",
    "measured rps",
    "output_tok/sec",
    "total_tok/sec",
    "prompt_token_count_mean",
    "prompt_token_count_p99",
    "output_token_count_mean",
    "output_token_count_p99",
    "ttft_median",
    "ttft_p95",
    "ttft_p1",
    "ttft_p999",
    "tpot_median",
    "tpot_p95",
    "tpot_p99",
    "tpot_p999",
    "tpot_p1",
    "itl_median",
    "itl_p95",
    "itl_p999",
    "itl_p1",
    "request_latency_median",
    "request_latency_min",
    "request_latency_max",
    "successful_requests",
    "errored_requests",
    "uuid",
    "ttft_mean",
    "ttft_p99",
    "itl_mean",
    "itl_p99",
    "runtime_args",
    "guidellm_start_time_ms",
    "guidellm_end_time_ms",
    "image_tag",
    "guidellm_version",
    "mlflow_run_id",
    "mlflow_experiment_id",
    "notes",
]


def _dashboard_csv(*rows: dict[str, str]) -> str:
    """Build a CSV as written by the llm_d plugin (DASHBOARD_FIELDNAMES)."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=DASHBOARD_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _staging_csv(*uuids: str) -> str:
    lines = [",".join(STAGING_DASHBOARD_COLUMNS)]
    for uuid in uuids:
        row = dict.fromkeys(STAGING_DASHBOARD_COLUMNS, "")
        row["uuid"] = uuid
        row["notes"] = "old, row"
        output = io.StringIO()
        csv.DictWriter(output, STAGING_DASHBOARD_COLUMNS, lineterminator="\n").writerow(row)
        lines.append(output.getvalue().rstrip("\n"))
    return "\n".join(lines) + "\n"


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_upload_columns_match_the_staging_dashboard() -> None:
    upload_columns = [
        name for name in DASHBOARD_FIELDNAMES if name not in dashboard_csv.EXCLUDED_COLUMNS
    ]
    assert upload_columns == STAGING_DASHBOARD_COLUMNS


def test_merge_creates_the_consolidated_csv_without_the_excluded_columns() -> None:
    merged, added = dashboard_csv.merge_rows(
        None, _dashboard_csv({"uuid": "run-1", "turns": "5", "turn_index": "0"})
    )

    assert added == 1
    assert merged.splitlines()[0] == ",".join(STAGING_DASHBOARD_COLUMNS)
    assert _rows(merged)[0]["uuid"] == "run-1"


def test_merge_appends_rows_and_keeps_the_existing_content() -> None:
    existing = _staging_csv("run-0")

    merged, added = dashboard_csv.merge_rows(
        existing,
        _dashboard_csv({"uuid": "run-1", "TP": "2"}, {"uuid": "run-1", "TP": "2"}),
    )

    assert added == 2
    assert merged.startswith(existing)
    assert [row["uuid"] for row in _rows(merged)] == ["run-0", "run-1", "run-1"]


def test_merge_adds_a_missing_final_newline() -> None:
    merged, _ = dashboard_csv.merge_rows(
        _staging_csv("run-0").rstrip("\n"), _dashboard_csv({"uuid": "run-1"})
    )

    assert [row["uuid"] for row in _rows(merged)] == ["run-0", "run-1"]


def test_merge_skips_runs_already_in_the_consolidated_csv() -> None:
    existing = _staging_csv("run-0")

    merged, added = dashboard_csv.merge_rows(existing, _dashboard_csv({"uuid": "run-0"}))

    assert added == 0
    assert merged == existing


def test_merge_rejects_a_column_mismatch() -> None:
    existing = _staging_csv("run-0").replace("notes", "comments", 1)

    with pytest.raises(dashboard_csv.DashboardCsvError, match="do not match"):
        dashboard_csv.merge_rows(existing, _dashboard_csv({"uuid": "run-1"}))


def test_merge_rejects_rows_without_a_run_id() -> None:
    with pytest.raises(dashboard_csv.DashboardCsvError, match="without a 'uuid'"):
        dashboard_csv.merge_rows(None, _dashboard_csv({"uuid": ""}))


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _NoSuchKey(_ClientError):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")


class _FakeS3:
    """In-memory S3 object honouring IfMatch / IfNoneMatch on put_object."""

    def __init__(self, content: str | None) -> None:
        self.content = content
        self.version = 0
        self.exceptions = type(
            "Exceptions", (), {"NoSuchKey": _NoSuchKey, "ClientError": _ClientError}
        )
        self.put_calls: list[dict] = []
        # Contents written by "another job" right before each of our next puts.
        self.concurrent_writes: list[str] = []

    def _etag(self) -> str:
        return f'"v{self.version}"'

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        if self.content is None:
            raise _NoSuchKey()
        return {"Body": io.BytesIO(self.content.encode()), "ETag": self._etag()}

    def put_object(self, **kwargs) -> None:
        self.put_calls.append(kwargs)
        if self.concurrent_writes:
            self.content = self.concurrent_writes.pop(0)
            self.version += 1
        if "IfMatch" in kwargs and (self.content is None or kwargs["IfMatch"] != self._etag()):
            raise _ClientError("PreconditionFailed")
        if kwargs.get("IfNoneMatch") == "*" and self.content is not None:
            raise _ClientError("PreconditionFailed")
        self.content = kwargs["Body"].decode()
        self.version += 1


class _FakeProject:
    def __init__(self, values: dict) -> None:
        self.values = values

    def get_config(self, key: str, default=None, **kwargs):
        return self.values.get(key, default)


@pytest.fixture
def sync_setup(monkeypatch, tmp_path):
    from projects.caliper.cli import s3_export

    def _install(*, existing: str | None, dry_run: bool = False) -> _FakeS3:
        fake_s3 = _FakeS3(existing)
        project = _FakeProject(
            {
                "caliper.postprocess.csv_dashboard.bucket": "bucket",
                "caliper.postprocess.csv_dashboard.s3_key": "staging/llmd.csv",
                "caliper.postprocess.csv_dashboard.vault.name": "vault",
                "caliper.postprocess.csv_dashboard.vault.credentials_file": "aws.credentials",
                "caliper.export.dry_run": dry_run,
            }
        )
        monkeypatch.setattr(dashboard_csv.config, "project", project, raising=False)
        monkeypatch.setattr(s3_export, "get_aws_credentials", lambda *_: tmp_path / "creds")
        monkeypatch.setattr(s3_export, "create_s3_client", lambda _: fake_s3)
        return fake_s3

    return _install


def test_sync_uploads_the_merged_csv(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=_staging_csv("run-0"))
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    assert dashboard_csv.sync_dashboard_csv(csv_path) == 1

    (put,) = fake_s3.put_calls
    assert (put["Bucket"], put["Key"], put["IfMatch"]) == ("bucket", "staging/llmd.csv", '"v0"')
    assert [row["uuid"] for row in _rows(fake_s3.content)] == ["run-0", "run-1"]


def test_sync_creates_the_consolidated_csv_when_missing(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=None)
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    assert dashboard_csv.sync_dashboard_csv(csv_path) == 1
    assert fake_s3.put_calls[0]["IfNoneMatch"] == "*"
    assert fake_s3.content.startswith("run,accelerator,")


def test_sync_does_not_upload_when_nothing_is_new(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=_staging_csv("run-1"))
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    assert dashboard_csv.sync_dashboard_csv(csv_path) == 0
    assert fake_s3.put_calls == []


def test_sync_dry_run_does_not_touch_s3(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=_staging_csv(), dry_run=True)
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    assert dashboard_csv.sync_dashboard_csv(csv_path) == 0
    assert fake_s3.put_calls == []


def test_sync_redoes_the_merge_when_another_job_wrote_first(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=_staging_csv("run-0"))
    fake_s3.concurrent_writes.append(_staging_csv("run-0", "run-other"))
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    assert dashboard_csv.sync_dashboard_csv(csv_path) == 1

    assert len(fake_s3.put_calls) == 2
    assert [row["uuid"] for row in _rows(fake_s3.content)] == ["run-0", "run-other", "run-1"]


def test_sync_creation_race_appends_to_the_file_the_other_job_created(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=None)
    fake_s3.concurrent_writes.append(_staging_csv("run-other"))
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    assert dashboard_csv.sync_dashboard_csv(csv_path) == 1
    assert [row["uuid"] for row in _rows(fake_s3.content)] == ["run-other", "run-1"]


def test_sync_gives_up_when_the_file_keeps_changing(sync_setup, tmp_path) -> None:
    fake_s3 = sync_setup(existing=_staging_csv("run-0"))
    fake_s3.concurrent_writes.extend(
        _staging_csv("run-0", f"other-{i}") for i in range(dashboard_csv.MAX_WRITE_ATTEMPTS)
    )
    csv_path = tmp_path / "dashboard.csv"
    csv_path.write_text(_dashboard_csv({"uuid": "run-1"}), encoding="utf-8")

    with pytest.raises(dashboard_csv.DashboardCsvError, match="kept changing"):
        dashboard_csv.sync_dashboard_csv(csv_path)
