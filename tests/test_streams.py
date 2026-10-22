"""逐秒数据定位层的单元测试。

重点是时间对齐：活动摘要与 FIT 文件之间没有共同 ID，只能靠开始时间匹配，
而两边的时间字符串格式不一样（库里的带 `+00:00`，FIT 的是 naive UTC）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from garmaple.streams import _parse_utc, _same_moment

START = datetime(2026, 3, 2, 7, 0, 0, tzinfo=timezone.utc)


def test_same_moment_accepts_timezone_suffix():
    """库里的 utc_start 带 +00:00，FIT 的是 naive，两边必须判为同一秒。

    这是真实踩到的 bug：只比格式化字符串时，真实数据上 6 个活动全部 MISS。
    """
    assert _same_moment(START, "2026-03-02T07:00:00+00:00")
    assert _same_moment(START, "2026-03-02T07:00:00")
    assert _same_moment(START, "2026-03-02T07:00:01+00:00")  # 1 秒容差
    assert not _same_moment(START, "2026-03-02T07:00:05+00:00")
    assert not _same_moment(START, "不是时间")


def test_parse_utc_normalises_to_naive_utc():
    assert _parse_utc("2026-03-02T07:00:00+00:00") == datetime(2026, 3, 2, 7)
    # +8 区的时间换算到 UTC 要减 8 小时，不能直接截断时区
    assert _parse_utc("2026-03-02T15:00:00+08:00") == datetime(2026, 3, 2, 7)
    assert _parse_utc("") is None
    assert _parse_utc(None) is None
    assert _parse_utc("不是时间") is None
