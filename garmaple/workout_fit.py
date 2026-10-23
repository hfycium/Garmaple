"""生成可导入的 Garmin FIT 训练课表。"""

from __future__ import annotations

import re
import secrets
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fitdecode.utils import compute_crc

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SPORTS = {"running": 1, "cycling": 2, "swimming": 5, "walking": 11, "training": 10}
INTENSITIES = {"active": 0, "rest": 1, "warmup": 2, "cooldown": 3,
               "recovery": 4, "interval": 5, "other": 6}
MAX_STEPS = 100


def _definition(local: int, global_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    return bytes((0x40 | local, 0, 0)) + struct.pack("<H", global_num) + bytes((len(fields),)) + b"".join(
        bytes(field) for field in fields
    )


def _data(local: int, *values: bytes) -> bytes:
    return bytes((local,)) + b"".join(values)


def _string(value: str, size: int) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) >= size:
        raise ValueError(f"字符串 UTF-8 长度必须小于 {size} 字节")
    return encoded + b"\0" * (size - len(encoded))


def encode_workout_fit(
    name: str,
    sport: str,
    steps: list[dict[str, Any]],
    *,
    created_at: datetime | None = None,
) -> bytes:
    """将限时、开放目标的步骤编码为标准 FIT workout 文件。"""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("课表名称不能为空")
    if sport not in SPORTS:
        raise ValueError(f"sport 必须是以下之一：{', '.join(SPORTS)}")
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"步骤数必须在 1~{MAX_STEPS} 之间")

    normalized: list[tuple[str, int, int]] = []
    for index, step in enumerate(steps, 1):
        try:
            seconds = int(step["duration_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 步缺少有效的 duration_s") from exc
        intensity = str(step.get("intensity", "active")).lower()
        step_name = str(step.get("name", f"Step {index}")).strip()
        if not 1 <= seconds <= 86_400:
            raise ValueError(f"第 {index} 步 duration_s 必须在 1~86400 之间")
        if intensity not in INTENSITIES:
            raise ValueError(f"第 {index} 步 intensity 无效：{intensity}")
        if not step_name:
            step_name = f"Step {index}"
        normalized.append((step_name, seconds, INTENSITIES[intensity]))

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    created = int((timestamp - FIT_EPOCH).total_seconds())

    body = bytearray()
    # file_id: type, manufacturer, product, serial_number, time_created
    file_fields = [(0, 1, 0), (1, 2, 0x84), (2, 2, 0x84), (3, 4, 0x8C), (4, 4, 0x86)]
    body += _definition(0, 0, file_fields)
    body += _data(0, bytes((5,)), struct.pack("<H", 255), struct.pack("<H", 0),
                  struct.pack("<I", secrets.randbits(32) or 1), struct.pack("<I", created))

    # workout: sport, capabilities, num_valid_steps, wkt_name
    workout_fields = [(4, 1, 0), (5, 4, 0x8C), (6, 2, 0x84), (8, 64, 0x07)]
    body += _definition(1, 26, workout_fields)
    body += _data(1, bytes((SPORTS[sport],)), struct.pack("<I", 0),
                  struct.pack("<H", len(normalized)), _string(clean_name, 64))

    # workout_step: index, time duration, open target, intensity, display name.
    step_fields = [(254, 2, 0x84), (1, 1, 0), (2, 4, 0x86), (3, 1, 0),
                   (4, 4, 0x86), (5, 4, 0x86), (6, 4, 0x86), (7, 1, 0),
                   (0, 32, 0x07)]
    body += _definition(2, 27, step_fields)
    for index, (step_name, seconds, intensity) in enumerate(normalized):
        body += _data(2, struct.pack("<H", index), bytes((0,)),
                      struct.pack("<I", seconds * 1000), bytes((2,)),
                      struct.pack("<I", 0), struct.pack("<I", 0),
                      struct.pack("<I", 0),
                      bytes((intensity,)), _string(step_name, 32))

    header = bytearray((14, 0x20)) + struct.pack("<H", 2160) + struct.pack("<I", len(body)) + b".FIT"
    header += struct.pack("<H", compute_crc(header))
    content = bytes(header + body)
    return content + struct.pack("<H", compute_crc(content))


def safe_workout_filename(name: str) -> str:
    """生成不含路径组件的安全文件名。"""
    stem = re.sub(r"[^\w.-]+", "_", name.strip(), flags=re.UNICODE).strip("._")[:64]
    return (stem or "workout") + ".fit"


def write_workout_fit(
    directory: Path,
    name: str,
    sport: str,
    steps: list[dict[str, Any]],
) -> Path:
    """在指定目录创建 FIT 课表，不覆盖已有文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    filename = safe_workout_filename(name)
    target = directory / filename
    suffix = 2
    while target.exists():
        target = directory / f"{Path(filename).stem}_{suffix}.fit"
        suffix += 1
    target.write_bytes(encode_workout_fit(name, sport, steps))
    return target
