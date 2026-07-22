"""get_time_str format at each ms/s/m/h/d boundary and between transitions."""
import pytest

from mama.util import get_time_str


@pytest.mark.parametrize('seconds,expected', [
    # below 0.1s: milliseconds (keeps truly-instant ops from rendering a meaningless 0.0s)
    (0,        '0ms'),
    (0.001,    '1ms'),
    (0.099,    '99ms'),

    # 0.1s and up under a minute: one decimal place (0.2s reads better than 200ms)
    (0.1,      '0.1s'),
    (0.2,      '0.2s'),
    (0.5,      '0.5s'),
    (1,        '1.0s'),
    (1.5,      '1.5s'),
    (42,       '42.0s'),
    (59.9,     '59.9s'),

    # 1m–59m: 'Xm Ys' (note the space - already established project style)
    (60,       '1m 0s'),
    (67,       '1m 7s'),    # the example from the user request
    (125,      '2m 5s'),
    (3599,     '59m 59s'),

    # 1h–23h: 'Xh Ym Zs'
    (3600,     '1h 0m 0s'),
    (3661,     '1h 1m 1s'),
    (86399,    '23h 59m 59s'),

    # 1d+: 'Xd Yh Zm Ws'
    (86400,    '1d 0h 0m 0s'),
    (90061,    '1d 1h 1m 1s'),
])
def test_get_time_str(seconds, expected):
    assert get_time_str(seconds) == expected
