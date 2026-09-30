from app import gcs
from app.storage import GCSBackend


class _Blob:
    def __init__(self, sink, key):
        self.sink, self.key, self.storage_class = sink, key, None

    def upload_from_string(self, data, **kw):
        self.sink.append((self.key, self.storage_class))

    def upload_from_filename(self, path, **kw):
        self.sink.append((self.key, self.storage_class))


class _Client:
    def __init__(self, sink):
        self.sink = sink

    def bucket(self, name):
        return self

    def blob(self, key):
        return _Blob(self.sink, key)


def _backend(monkeypatch, sink, **kw):
    monkeypatch.setattr(gcs, "_client", lambda *a, **k: _Client(sink))
    return GCSBackend("{}", "bkt", **kw)


def test_write_bytes_and_upload_use_the_archives_class(monkeypatch, tmp_path):
    sink = []
    b = _backend(monkeypatch, sink, storage_class="ARCHIVE")
    f = tmp_path / "a.tar"
    f.write_bytes(b"x")
    b.write_bytes("index/x.json", b"{}")
    b.upload("archives/x.tar", f)
    assert sink == [("index/x.json", "ARCHIVE"), ("archives/x.tar", "ARCHIVE")]


def test_no_class_leaves_bucket_default(monkeypatch):
    sink = []
    _backend(monkeypatch, sink).write_bytes("k", b"{}")
    assert sink == [("k", None)]
