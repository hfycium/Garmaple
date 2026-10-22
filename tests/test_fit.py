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


# ---------------- 速度清洗 ----------------


def test_parse_fit_drops_impossible_speed():
    """超过 20 m/s 的瞬时速度按「测坏了」处理。

    真实数据里一次 6'51"/km 的慢跑出现过 24.05 m/s（86 km/h）的单点，
    换算成配速是 42 秒/公里。不清洗会同时污染序列与汇总。
    """
    stream = make_stream(
        [
            sample_at(START, 0, speed_mps=3.0),
            sample_at(START, 2, speed_mps=24.05),  # 毛刺
            sample_at(START, 4, speed_mps=3.2),
        ]
    )
    assert stream is not None
    assert [s.speed_mps for s in stream.samples] == [pytest.approx(3.0), None, pytest.approx(3.2)]


def test_summary_reports_p95_not_max_speed():
    """汇总给 95 分位而不是最大值，避免把毛刺当能力指标。"""
    samples = [FitSample(offset_s=i, speed_mps=3.0) for i in range(99)]
    samples.append(FitSample(offset_s=99, speed_mps=19.5))  # 仍在上限内，但不该当代表值
    summary = sample_summary(samples)
    assert "speed_max_mps" not in summary
    assert summary["speed_p95_mps"] < 5
    assert summary["speed_avg_mps"] == pytest.approx(3.17, abs=0.05)


def test_percentile_interpolates():
    from garmaple.parser.fit import percentile

    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([10], 0.95) == 10
    assert percentile([1, 2], 1.0) == 2
    with pytest.raises(ValueError):
        percentile([], 0.5)


# ---------------- 主观评价与功率 ----------------


def test_workout_rpe_is_converted_from_borg_cr10_times_ten():
    """`workout_rpe` 在 FIT 里是 Borg CR10（0–10）**乘以 10** 存的。

    真实数据里出现过 80 / 70 / 10 / 100，照原样返回会被读成「RPE 80」，
    而它其实是 Borg 8.0（非常吃力）。FIT Profile 的原文是
    "Common Borg CR10 / 0-10 RPE scale, multiplied 10x"。
    """
    stream = make_stream([sample_at(START, 0)], workout_rpe=80, workout_feel=25)
    assert stream.workout_rpe == pytest.approx(8.0)
    assert stream.workout_feel == 25

    stream = make_stream([sample_at(START, 0)], workout_rpe=10, workout_feel=0)
    assert stream.workout_rpe == pytest.approx(1.0)  # 10 表示 Borg 1.0，不是 10


def test_workout_fields_absent_stays_none():
    """没填就是 None，不要补 0——RPE 0 会被读成「一点也不累」。"""
    stream = make_stream([sample_at(START, 0)])
    assert stream.workout_rpe is None
    assert stream.workout_feel is None


def test_power_is_parsed_and_invalid_values_become_none():
    """`power` 是 FIT record 的标准字段（字段号 7），与是否装功率计无关。

    有功率计的导出会写满，没装的设备整列缺省。未测到的采样写 uint16 的
    无效值 0xFFFF，必须读成 None 而不是 65535 瓦。
    """
    stream = make_stream(
        [
            sample_at(START, 0, power_w=180),
            sample_at(START, 2, power_w=None),  # 0xFFFF
            sample_at(START, 4, power_w=205),
        ]
    )
    assert [s.power_w for s in stream.samples] == [180, None, 205]


def test_power_summary_omitted_without_power_data():
    """没功率就不出功率汇总，免得空值被当成 0 瓦。"""
    samples = [FitSample(offset_s=i, hr=150) for i in range(5)]
    summary = sample_summary(samples)
    assert "power_avg_w" not in summary
    assert "power_max_w" not in summary

    powered = [FitSample(offset_s=i, hr=150, power_w=200 + i) for i in range(5)]
    summary = sample_summary(powered)
    # 功率峰值是有效指标（冲刺能力），不像速度会因 GPS 毛刺失真
    assert summary["power_avg_w"] == 202.0
    assert summary["power_max_w"] == 204
