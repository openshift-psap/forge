import gzip
from types import SimpleNamespace

from projects.cluster.toolbox.capture_prometheus_db import main as capture


def test_capture_publishes_corrected_gzip(tmp_path, monkeypatch):
    original = b'metric{app.kubernetes.io/version="1.2",namespace="test"} 1 123\n# EOF\n'

    def fake_oc_exec(*command, stdout_dest, **kwargs):
        assert command[-1].endswith(" | gzip")
        stdout_dest.write_bytes(gzip.compress(original))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(capture, "oc_exec", fake_oc_exec)
    context = SimpleNamespace(
        output_dir=tmp_path,
        start_ms=123000,
        end_ms=124000,
        namespace="openshift-monitoring",
        pod_name="prometheus-k8s-0",
        container="prometheus",
    )

    capture.dump_metrics.__wrapped__(SimpleNamespace(head_only=False), context)

    archive = tmp_path / "open_metrics.gz"
    assert gzip.decompress(archive.read_bytes()) == original.replace(
        b"app.kubernetes.io/version=", b'"app.kubernetes.io/version"='
    )
    assert context.archive_size_bytes == archive.stat().st_size
    assert list(tmp_path.iterdir()) == [archive]
