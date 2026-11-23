import gzip

from projects.cluster.library.prom.replay import _decompress_openmetrics


def test_replay_quotes_extended_label_names_in_temporary_dump(tmp_path):
    original = (
        b"# TYPE servicemeshoperator3_istiod_total gauge\n"
        b'servicemeshoperator3_istiod_total{app.kubernetes.io/version="1.2",'
        rb'note="keep ,app.kubernetes.io/version=literal and \"quotes\""'
        b',service-name="istiod","already.quoted"="value"} 1 1795384007.040\n'
        b'other_metric{job="prometheus"} 2 1795384007.040\n'
        b"# EOF\n"
    )
    archive = tmp_path / "open_metrics.gz"
    archive.write_bytes(gzip.compress(original))
    output = tmp_path / "open_metrics.txt"

    _decompress_openmetrics(archive, output)

    assert output.read_bytes() == original.replace(
        b'{app.kubernetes.io/version="1.2"', b'{"app.kubernetes.io/version"="1.2"'
    ).replace(b',service-name="istiod"', b',"service-name"="istiod"')
    assert gzip.decompress(archive.read_bytes()) == original
