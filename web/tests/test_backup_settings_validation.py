# web/tests/test_auth.py has no TestClient/httpx pattern to follow (checked
# per docs/backup-plan/steps/01-settings.md), so these exercise the extracted
# validate_archive_sizes helper directly rather than the route.
from app.backup_settings import GIB, MIB, validate_archive_sizes


def test_rejects_non_positive_size():
    assert validate_archive_sizes(0, 67108864, 1073741824) == "Archive+sizes+must+be+positive"
    assert validate_archive_sizes(-1, 67108864, 1073741824) == "Archive+sizes+must+be+positive"
    assert validate_archive_sizes(2621440, 0, 1073741824) == "Archive+sizes+must+be+positive"
    assert validate_archive_sizes(2621440, 67108864, 0) == "Archive+sizes+must+be+positive"


def test_rejects_min_not_below_clump():
    sixty_four_mib = 64 * MIB
    assert (
        validate_archive_sizes(sixty_four_mib, sixty_four_mib, GIB)
        == "Min+size+must+be+smaller+than+the+clump+size"
    )


def test_rejects_clump_above_max():
    assert validate_archive_sizes(2621440, 2 * GIB, GIB) == "Clump+size+cannot+exceed+the+max+object+size"


def test_rejects_max_at_or_below_one_mib():
    assert validate_archive_sizes(1024, 2048, int(0.5 * MIB)) == "Max+object+size+must+be+larger+than+1+MiB"


def test_accepts_spec_defaults():
    min_bytes = int(round(2.5 * MIB))
    clump_bytes = int(round(64 * MIB))
    max_bytes = int(round(1024 * MIB))
    assert (min_bytes, clump_bytes, max_bytes) == (2621440, 67108864, 1073741824)
    assert validate_archive_sizes(min_bytes, clump_bytes, max_bytes) is None
