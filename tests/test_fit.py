"""FIT 解析与逐秒定位的单元测试。

全部用 `fit_factory` 现造的合成 FIT，不依赖真实导出包。三条回归保护对应
真实数据上踩过的坑：

- 单位：`fitdecode` 已经应用了 scale/offset，`enhanced_speed` 就是 m/s。
  曾经按「原始值 ÷1000」的思路写过，会把配速整体缩小一千倍。
- 时间：库里的 `utc_start` 带 `+00:00` 后缀，FIT 的 `start_time` 是 naive，
  直接比字符串永远匹配不上（实测 6 个活动全部 MISS）。
- 开始前的采样：设备按下开始之前已在记录，这些点的时间偏移为负，
  照单全收会让时间轴从负数开始。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from garmaple.parser.fit import (
    FitSample,
    downsample,
    parse_fit,
    read_header,
    sample_summary,
)

from .fit_factory import build_fit, sample_at

START = datetime(2026, 3, 2, 7, 0, 0, tzinfo=timezone.utc)


def make_stream(samples=None, **kwargs):
    raw = build_fit(start=START, samples=samples, **kwargs)
    return parse_fit(io.BytesIO(raw))


# ---------------- 文件头 ----------------


def test_read_header_flags_activity():
    raw = build_fit(start=START, samples=[sample_at(START, 0)])
    header = read_header(io.BytesIO(raw))
    assert header.is_activity
    assert header.kind == "activity"
    assert header.time_created == START


def test_read_header_rejects_monitoring_file():
    raw = build_fit(start=START, samples=[sample_at(START, 0)], kind="monitoring_b")
    header = read_header(io.BytesIO(raw))
    assert not header.is_activity
    assert header.kind == "monitoring_b"


def test_read_header_survives_garbage():
    """截断的文件不该让整个索引崩掉。"""
    header = read_header(io.BytesIO(b"\x0e\x10\x00\x00garbage"))
    assert header.kind is None
    assert not header.is_activity
    assert header.time_created is None


# ---------------- 逐秒解析 ----------------


def test_parse_fit_reads_physical_units():
    """字段值必须原样落地：速度就是 m/s，海拔就是米。

    这几条断言同时钉住单位口径。若有人把 enhanced_speed 再除以 1000，
    2.8 会变成 0.0028，测试立刻变红。
    """
    stream = make_stream(
        [sample_at(START, 0, hr=120, speed_mps=2.8, altitude_m=30.4, cadence=82, distance_m=2.8)]
    )
    assert stream is not None
    point = stream.samples[0]
    assert point.hr == 120
    assert point.speed_mps == pytest.approx(2.8)
    assert point.altitude_m == pytest.approx(30.4)
    assert point.cadence == 82
    assert point.distance_m == pytest.approx(2.8)


def test_parse_fit_offsets_are_seconds_from_start():
    stream = make_stream([sample_at(START, 0), sample_at(START, 61), sample_at(START, 185)])
    assert stream is not None
    assert [s.offset_s for s in stream.samples] == [0, 61, 185]
    assert stream.utc_start == START
    assert stream.sport == "running"


def test_parse_fit_drops_samples_before_start():
    """开始时刻之前的采样偏移为负，必须丢弃，否则时间轴会倒着走。"""
    stream = make_stream(
        [
            sample_at(START - timedelta(seconds=5), 0),
            sample_at(START, 0),
            sample_at(START, 10),
        ]
    )
    assert stream is not None
    assert [s.offset_s for s in stream.samples] == [0, 10]


def test_parse_fit_requires_session_and_records():
    """缺 session 或缺 record 都不算活动文件。"""
    assert parse_fit(io.BytesIO(build_fit(start=START, samples=[sample_at(START, 0)],
                                         with_session=False))) is None
    assert parse_fit(io.BytesIO(build_fit(start=START, samples=[], with_records=False))) is None


def test_parse_fit_survives_garbage():
    assert parse_fit(io.BytesIO(b"not a fit file at all")) is None


# ---------------- 抽稀与统计 ----------------


def test_downsample_keeps_first_and_last():
    samples = [FitSample(offset_s=i, hr=100 + i) for i in range(1000)]
    picked = downsample(samples, 50)
    assert len(picked) == 50
    assert picked[0].offset_s == 0
    assert picked[-1].offset_s == 999


def test_downsample_is_noop_below_limit():
    samples = [FitSample(offset_s=i) for i in range(10)]
    assert downsample(samples, 500) == samples


def test_sample_summary_uses_full_data_not_downsampled():
    """极值必须来自全量。

    第 500 个点上有一次 200 bpm 的尖峰；抽稀到 10 个点会跳过它。
    若 summary 拿抽稀结果算，会报出偏低的最大心率，AI 就会据此下错结论。
    """
    samples = [FitSample(offset_s=i, hr=120) for i in range(1000)]
    samples[500] = FitSample(offset_s=500, hr=200)
    summary = sample_summary(samples)
    assert summary["hr_max"] == 200
    assert summary["points"] == 1000
    assert len(downsample(samples, 10)) == 10


# ---------------- 时间对齐 ----------------
# `_parse_utc` / `_same_moment` 属于定位层，测试放在 test_streams.py
