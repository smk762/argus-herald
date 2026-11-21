import math
import subprocess
import sys
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from argus_herald.charts import TOP, Scale, _log_ticks, compact, nice_ticks, range_ticks, series_panel

# Run apart, with a one-second alarm: without the guard this loop never ends and eats memory.
_PROBE = """
import math, signal
signal.setitimer(signal.ITIMER_REAL, 1)
from argus_herald.charts import _log_ticks
assert _log_ticks(math.inf) == [0.0, 1.0]
assert _log_ticks(math.nan) == [0.0, 1.0]
"""


def test_log_ticks_end_for_a_top_that_is_not_finite():
    done = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr.decode()[-300:]
    assert _log_ticks(0) == [0.0, 1.0]
    assert _log_ticks(250) == [0.0, 1.0, 10.0, 100.0, 1000.0]


def test_compact_gives_a_placeholder_for_nan_and_infinity():
    assert compact(math.nan) == "–"
    assert compact(math.inf) == "–" and compact(-math.inf) == "–"
    assert compact(3) == "3" and compact(2.5) == "2.5" and compact(12_000) == "12K"


@pytest.mark.parametrize(("top", "count"), [(math.nan, 3), (math.inf, 3), (5, 0), (5, -2), (5e-324, 3)])
def test_nice_ticks_fall_back_for_a_top_or_count_they_cannot_use(top, count):
    assert nice_ticks(top, count) == [0.0, 1.0]


def test_nice_ticks_are_whole_steps_not_a_running_sum():
    assert nice_ticks(0.3) == [0.0, 0.1, 0.2, 0.3]
    assert nice_ticks(0.6) == [0.0, 0.2, 0.4, 0.6]
    assert nice_ticks(250) == [0.0, 100.0, 200.0, 300.0]
    assert nice_ticks(1e308)[-1] == 1e308


def test_range_ticks_survive_a_flat_or_non_finite_range():
    big = 2.0**60  # big + 1 == big, so a span of 1 is no span
    ticks = range_ticks(big, big)
    assert ticks[0] <= big < ticks[-1]
    assert range_ticks(5, 5) == [5.0, 5.5, 6.0]
    assert range_ticks(math.nan, 1) == [0.0, 1.0]
    assert range_ticks(0, math.inf) == [0.0, 1.0]
    assert range_ticks(-1e308, 1e308) == [-1e308, 1e308]


def test_range_ticks_are_clean_and_end_at_the_top():
    assert range_ticks(0.1, 0.3) == [0.1, 0.2, 0.3]
    assert range_ticks(0.7, 0.9) == [0.6, 0.7, 0.8, 0.9]
    assert range_ticks(1.1, 1.3) == [1.1, 1.2, 1.3]
    assert range_ticks(100, 103) == [100.0, 101.0, 102.0, 103.0]


def test_series_panel_escapes_its_key():
    days = [date(2026, 1, 1), date(2026, 1, 2)]
    svg = str(series_panel('k"><script>x</script>', "Title", days, [1.0, 2.0], None, [], []))
    assert "<script>" not in svg
    assert 'aria-labelledby="k&#34;&gt;&lt;script&gt;x&lt;/script&gt;-title"' in svg


_NEAR_MAX_PROBE = """
import math, signal, sys
signal.setitimer(signal.ITIMER_REAL, 1)
from argus_herald.charts import _log_ticks
ticks = _log_ticks(sys.float_info.max)
assert all(math.isfinite(t) for t in ticks) and ticks[-1] == sys.float_info.max, ticks[-3:]
"""


def test_log_ticks_end_for_a_top_near_the_float_maximum():
    done = subprocess.run([sys.executable, "-c", _NEAR_MAX_PROBE], capture_output=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr.decode()[-300:]


@pytest.mark.parametrize(
    ("value", "label"),
    [(999_999, "1M"), (-999_700, "-1M"), (999_499, "999K"), (9_999.7, "10K"), (999.96, "1,000"), (999.9, "999.9")],
)
def test_compact_picks_the_unit_after_rounding(value, label):
    assert compact(value) == label


def test_a_scale_whose_top_tick_is_zero_keeps_zero_at_the_top():
    scale = Scale(3, 0, 130, False, lo=-10)
    assert scale.ticks[-1] == 0 and scale.top == 0
    assert scale.y(0) == TOP


def test_a_broadcast_marker_links_only_an_https_url():
    days = [date(2026, 1, 1), date(2026, 1, 2)]

    def post(link):
        return SimpleNamespace(
            platform="bluesky",
            at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            subject="s",
            format="f",
            logged=True,
            text="t",
            link=link,
        )

    unsafe = str(series_panel("k", "T", days, [1.0, 2.0], None, [], [post("javascript:alert(1)")]))
    assert "javascript:" not in unsafe and "<a " not in unsafe
    assert '<g class="marker broadcast">' in unsafe
    safe = str(series_panel("k", "T", days, [1.0, 2.0], None, [], [post("https://bsky.app/p/1")]))
    assert 'href="https://bsky.app/p/1"' in safe
