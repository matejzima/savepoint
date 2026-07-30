import os
from unittest.mock import patch

from app.deps import _local_time


def test_local_time_converts_utc_to_configured_tz():
    with patch.dict(os.environ, {"TZ": "Europe/Prague"}):
        result = _local_time("2026-01-15T10:00:00+00:00")
    # Europe/Prague is UTC+1 in January (no DST)
    assert result == "2026-01-15 11:00:00 CET"


def test_local_time_falls_back_to_utc_when_tz_unset():
    with patch.dict(os.environ, {}, clear=True):
        result = _local_time("2026-01-15T10:00:00+00:00")
    assert result == "2026-01-15 10:00:00 UTC"


def test_local_time_passes_through_none_and_empty_string():
    assert _local_time(None) is None
    assert _local_time("") == ""


def test_local_time_respects_summer_dst_offset():
    with patch.dict(os.environ, {"TZ": "Europe/Prague"}):
        result = _local_time("2026-07-15T10:00:00+00:00")
    # Europe/Prague is UTC+2 in July (DST)
    assert result == "2026-07-15 12:00:00 CEST"
