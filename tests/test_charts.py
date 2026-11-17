import math
import subprocess
import sys

from argus_herald.charts import _log_ticks, compact

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
