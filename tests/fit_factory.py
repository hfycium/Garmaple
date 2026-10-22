"""构造最小 FIT 文件，供单元测试使用。

FIT 是二进制格式，`fitdecode` 只读不写，因此这里手写一个只够测试用的编码器，
覆盖 `file_id` / `session` / `record` 三种消息。

**只用合成数据。** 真实导出包里的 `.fit` 文件名和内容带设备序列号，
抄进测试夹具就等于把序列号推进公开仓库（这个坑踩过一次）。

格式要点（都与真实文件核对过）：
- 文件尾 CRC 覆盖「14 字节 header + 数据」；header 内部那 2 字节 CRC 只覆盖前 12 字节
- 时间的 epoch 是 1989-12-31T00:00:00Z
- 带 scale 的字段写进去要乘回去：`enhanced_speed` ×1000、
  `enhanced_altitude` ×5 再 +500 偏移量、`distance` ×100
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from fitdecode.utils import compute_crc

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

ENUM = 0x00
UINT8 = 0x02
UINT16 = 0x84
UINT32 = 0x86

# FIT profile 里的全局消息号
_FILE_ID = 0
_SESSION = 18
_RECORD = 20


def _ts(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


class FitSampleSpec(tuple):
    """一条 record：`(时间, 心率, 速度 m/s, 海拔 m, 步频, 距离 m, 功率 W)`。

    用具名构造器读起来清楚，测试里少写几个位置参数错位。
    """

    __slots__ = ()

    @classmethod
    def make(
        cls,
        when: datetime,
        hr: int = 140,
        speed_mps: float = 3.0,
        altitude_m: float = 20.0,
        cadence: int = 85,
        distance_m: float = 0.0,
        power_w: int | None = None,
    ) -> "FitSampleSpec":
        return cls((when, hr, speed_mps, altitude_m, cadence, distance_m, power_w))


def build_fit(
    *,
    start: datetime,
    samples: list[tuple] | None = None,
    sport: str = "running",
    created: datetime | None = None,
    kind: str = "activity",
    with_session: bool = True,
    with_records: bool = True,
    workout_rpe: int | None = None,
    workout_feel: int | None = None,
) -> bytes:
    """返回一个可被 fitdecode 解析的最小 FIT 文件字节串。

    只填了解析层真正用到的字段。`kind` 用来模拟非活动文件（例如每日监测），
    `with_session` / `with_records` 用来构造「不是活动」的边界样本。

    `workout_rpe` / `workout_feel` 是 session 级的主观评价（FIT 字段号 193 / 192）。
    两者都是可选的——真实数据里 304 次活动只有 9 次填过，缺是常态。
    """
    data = bytearray()

    def defmsg(local: int, mesg_num: int, fields: list[tuple[int, int, int]]) -> None:
        data.append(0x40 | local)
        data.extend(b"\x00\x00")  # reserved + little endian
        data.extend(mesg_num.to_bytes(2, "little"))
        data.append(len(fields))
        for num, size, base in fields:
            data.extend(bytes([num, size, base]))

    def datamsg(local: int, payload: bytes) -> None:
        data.append(local)
        data.extend(payload)

    file_id_fields: list[tuple[int, int, int]] = [(0, 1, ENUM), (4, 4, UINT32)]
    defmsg(0, _FILE_ID, file_id_fields)
    datamsg(0, bytes([_KIND_CODES.get(kind, 0)]) + _ts(created or start).to_bytes(4, "little"))

    if with_session:
        session_fields: list[tuple[int, int, int]] = [(2, 4, UINT32), (5, 1, ENUM)]
        payload = bytearray(_ts(start).to_bytes(4, "little"))
        payload.append(_SPORT_CODES.get(sport, 0))
        if workout_feel is not None:
            session_fields.append((192, 1, UINT8))
            payload.append(workout_feel)
        if workout_rpe is not None:
            session_fields.append((193, 1, UINT8))
            payload.append(workout_rpe)
        defmsg(1, _SESSION, session_fields)
        datamsg(1, bytes(payload))

    if with_records:
        # 功率列一旦定义就要每条 record 都有值；没有功率的采样写 uint16 的
        # 「无效」值 0xFFFF，fitdecode 会把它读成 None，不会污染统计。
        has_power = any(len(s) > 6 and s[6] is not None for s in (samples or []))
        record_fields: list[tuple[int, int, int]] = [
            (253, 4, UINT32),  # timestamp
            (3, 1, UINT8),  # heart_rate
            (73, 4, UINT32),  # enhanced_speed, ×1000
            (78, 4, UINT32),  # enhanced_altitude, ×5 +500
            (4, 1, UINT8),  # cadence
            (5, 4, UINT32),  # distance, ×100
        ]
        if has_power:
            record_fields.append((7, 2, UINT16))  # power, watts
        defmsg(2, _RECORD, record_fields)
        for spec in samples or []:
            when, hr, speed, alt, cadence, distance = spec[:6]
            power = spec[6] if len(spec) > 6 else None
            payload = bytearray()
            payload.extend(_ts(when).to_bytes(4, "little"))
            payload.append(hr)
            payload.extend(int(round(speed * 1000)).to_bytes(4, "little"))
            payload.extend(int(round((alt + 500) * 5)).to_bytes(4, "little"))
            payload.append(cadence)
            payload.extend(int(round(distance * 100)).to_bytes(4, "little"))
            if has_power:
                payload.extend(
                    (0xFFFF if power is None else int(power)).to_bytes(2, "little")
                )
            datamsg(2, bytes(payload))

    header = bytearray([14, 0x10, 0x00, 0x00])
    header.extend(len(data).to_bytes(4, "little"))
    header.extend(b".FIT")
    header.extend(compute_crc(bytes(header)).to_bytes(2, "little"))

    footer = compute_crc(bytes(header) + bytes(data))
    return bytes(header) + bytes(data) + footer.to_bytes(2, "little")


# FIT profile 的 file / sport 枚举值
_KIND_CODES = {"device": 1, "activity": 4, "monitoring_b": 32, "workout": 5}
_SPORT_CODES = {
    "generic": 0, "running": 1, "cycling": 2, "transition": 3,
    "fitness_equipment": 4, "swimming": 5, "walking": 11, "training": 10,
}


def as_stream(raw: bytes) -> io.BytesIO:
    """包成 fitdecode 能吃的文件对象。"""
    return io.BytesIO(raw)


def sample_at(start: datetime, seconds: int, **kwargs) -> FitSampleSpec:
    """按「开始后第几秒」造一条采样，省得每次手算时间。"""
    return FitSampleSpec.make(start + timedelta(seconds=seconds), **kwargs)
