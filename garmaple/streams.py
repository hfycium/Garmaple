"""按活动定位并解析逐秒记录。

活动摘要 JSON 里有活动 ID、开始时间，但没有逐秒数据；逐秒数据只存在于导出包
内嵌 zip 的那批 `.fit` 里。两者之间**没有共同的 ID 可以 join**：

- `.fit` 文件名形如 `<设备序列号>_<内部编号>.fit`，后半段不是 `activityId`
  （实测与 305 个活动 ID 交集为 0），前半段是设备序列号，不能当键用；
- 归档里也没有「活动 ID → FIT 文件」的对照表。

唯一可靠的对齐键是**开始时间**：`session.start_time`（UTC）与活动摘要的
`startTimeGmt`（epoch 毫秒）在抽样 20 个活动上精确匹配 20/20。

因此这里分两步：先用文件头里的 `time_created` 把候选缩到一两个，再完整解析
候选并用开始时间确认。`time_created` 是设备保存文件的时刻，不是开始时间，
只当索引用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .archive import Archive, Dataset
from .parser.fit import FitStream, parse_fit, read_header

# 候选窗口。实测 `time_created` 最早出现在活动进行中、最晚比活动结束晚 37 分钟，
# 两头都留 2 小时，宁可贵一点点也不要漏配。
_WINDOW = timedelta(hours=2)

_FIT_GLOB = "UploadedFiles_*.zip"


def _open_fits(archive: Archive) -> tuple[Archive, list[str]] | None:
    """把归档内嵌的那批 `.fit` 当作独立归档打开。

    返回 (内嵌归档, `.fit` 条目列表)。条目列表的顺序就是 zip 条目顺序，
    索引里的 `entry_idx` 指的就是这个下标，因此顺序必须稳定。
    """
    found = archive.find(_FIT_GLOB)
    if not found:
        return None
    nested = archive.nested(found[0])
    return nested, [n for n in nested.names if n.lower().endswith(".fit")]


def scan_activity_fits(dataset: Dataset) -> list[tuple[int, int, str | None]]:
    """扫描全部归档，返回活动 FIT 的定位。

    每项是 `(archive_idx, entry_idx, time_created)`：`archive_idx` 是该归档在
    `Dataset.archives` 里的序号，`entry_idx` 是内嵌 zip 里第几个 `.fit`。
    **不返回文件名**，因为文件名里带设备序列号。

    只读每个文件开头的 `file_id` 帧，6487 个文件约 2 秒；完整解析要 5 分钟。
    """
    rows: list[tuple[int, int, str | None]] = []
    for aidx, archive in enumerate(dataset.archives):
        opened = _open_fits(archive)
        if opened is None:
            continue
        nested, fits = opened
        for eidx, entry in enumerate(fits):
            with nested.open(entry) as fh:
                header = read_header(fh, entry=eidx)
            if not header.is_activity:
                continue
            created = header.time_created
            rows.append(
                (aidx, eidx, created.strftime("%Y-%m-%dT%H:%M:%S") if created else None)
            )
    return rows


def load_stream(
    dataset: Dataset,
    archive_idx: int,
    entry_idx: int,
    cache: dict[int, tuple[Archive, list[str]]] | None = None,
) -> FitStream | None:
    """按定位解析某个 FIT，得到逐秒数据。

    `cache` 可传入一个按 archive_idx 缓存的内嵌归档字典：内嵌 zip 有几十 MB，
    连续解析同一次活动附近的多个候选时不必反复读回内存。
    """
    if not 0 <= archive_idx < len(dataset.archives):
        return None
    archive = dataset.archives[archive_idx]
    opened = cache.get(archive_idx) if cache is not None else None
    if opened is None:
        opened = _open_fits(archive)
        if opened is None:
            return None
        if cache is not None:
            cache[archive_idx] = opened
    nested, fits = opened
    if not 0 <= entry_idx < len(fits):
        return None
    with nested.open(fits[entry_idx]) as fh:
        return parse_fit(fh, entry=entry_idx)


def _parse_utc(text: str | None) -> datetime | None:
    """解析库里存的时间为 naive UTC。

    库里的 `utc_start` 实际带时区后缀（`2026-10-21T12:58:43+00:00`），FIT 的
    `start_time` 是 naive 的 UTC。两边都归一成 naive UTC 才能比。
    """
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _same_moment(a: datetime, b: str) -> bool:
    """FIT 的开始时间与库里的 `utc_start` 是否是同一秒。

    设备写 session 与摘要用的时间源相同，但秒以下字段的舍入方向不保证一致，
    因此允许 1 秒误差。
    """
    other = _parse_utc(b)
    if other is None:
        return False
    return abs((a.replace(tzinfo=None) - other).total_seconds()) <= 1


def find_stream(
    dataset: Dataset,
    utc_start: str,
    duration_s: float | None,
    store: object | None = None,
) -> FitStream | None:
    """按活动开始时间找到对应的逐秒数据。

    先按 `time_created` 窗口筛候选；窗口没命中（归档时间字段缺失、或活动被
    重新上传过）就退回全扫——全扫 304 个活动文件约 1.5 分钟，只作为兜底。
    """
    start = _parse_utc(utc_start)
    if start is None:
        return None

    entries: list[tuple[int, int]] = []
    if store is not None:
        end = start + timedelta(seconds=float(duration_s or 0))
        entries = [
            (row["archive_idx"], row["entry_idx"])
            for row in store.fit_candidates(start - _WINDOW, end + _WINDOW)  # type: ignore[attr-defined]
        ]
        if not entries:
            entries = [
                (row["archive_idx"], row["entry_idx"])
                for row in store.fit_entries()  # type: ignore[attr-defined]
            ]

    cache: dict[int, tuple[Archive, list[str]]] = {}
    for archive_idx, entry_idx in entries:
        stream = load_stream(dataset, archive_idx, entry_idx, cache=cache)
        if stream is not None and _same_moment(stream.utc_start, utc_start):
            return stream
    return None
