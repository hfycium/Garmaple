"""FIT workout 编码器的字节级回归测试。"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import fitdecode
import pytest
from fitdecode.records import FitCRC
from fitdecode.utils import compute_crc

from garmaple.workout_fit import encode_workout_fit


def test_encoded_workout_has_valid_header_footer_crc_and_profile_messages() -> None:
    raw = encode_workout_fit(
        "Intervals",
        "running",
        [
            {"duration_s": 300, "name": "Warmup", "intensity": "warmup"},
            {"duration_s": 60, "name": "Fast", "intensity": "interval"},
        ],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert raw[0] == 14
    assert raw[8:12] == b".FIT"
    assert int.from_bytes(raw[12:14], "little") == compute_crc(raw[:12])
    assert int.from_bytes(raw[-2:], "little") == compute_crc(raw[:-2])

    messages = []
    crc_frames = []
    for frame in fitdecode.FitReader(io.BytesIO(raw), check_crc=True):
        if isinstance(frame, fitdecode.FitDataMessage):
            messages.append(frame)
        elif isinstance(frame, FitCRC):
            crc_frames.append(frame)
    assert [m.name for m in messages] == ["file_id", "workout", "workout_step", "workout_step"]
    assert messages[0].get_value("type") == "workout"
    assert messages[0].get_value("manufacturer") == "development"
    assert messages[0].get_value("serial_number") != 0
    workout = messages[1]
    assert workout.get_value("sport") == "running"
    assert workout.get_value("num_valid_steps") == 2
    assert workout.get_value("wkt_name") == "Intervals"
    assert messages[2].get_value("duration_time") == 300
    assert messages[2].get_value("intensity") == "warmup"
    assert messages[3].get_value("duration_time") == 60
    assert messages[3].get_value("intensity") == "interval"
    assert crc_frames and all(frame.matched for frame in crc_frames)


@pytest.mark.parametrize(
    "steps",
    [[], [{"duration_s": 0}], [{"duration_s": 10, "intensity": "bad"}]],
)
def test_encoder_rejects_invalid_workout_steps(steps: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        encode_workout_fit("test", "running", steps)
