import uuid

from app.cloud_inventory import build_report


def test_report_tiers_and_comparison():
    a1, a2, a3 = (str(uuid.uuid4()) for _ in range(3))
    objects = [
        (f"vault/archives/{a1}.tar", 4, "ARCHIVE"),
        (f"vault/index/{a1}.json", 1, "STANDARD"),
        (f"vault/movies/archives/{a2}.tar", 2, "STANDARD"),  # library subfolder, no index
        (f"vault/index/{a3}.json", 1, "STANDARD"),  # index without archive
        ("vault/archives/notes.tar", 9, "STANDARD"),  # not ours
        ("vault/photo.jpg", 9, "STANDARD"),
    ]
    gone = f"vault/archives/{uuid.uuid4()}.tar"

    r = build_report(objects, {f"vault/archives/{a1}.tar", gone})

    assert (r.archive_count, r.total_bytes) == (2, 6)
    assert r.by_tier == {"ARCHIVE": [1, 4], "STANDARD": [1, 2]}
    assert r.untracked == [f"vault/movies/archives/{a2}.tar"]
    assert r.missing == [gone]
    assert r.orphan_index == [f"vault/index/{a3}.json"]
    assert r.no_index == [f"vault/movies/archives/{a2}.tar"]
