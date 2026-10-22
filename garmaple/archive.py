"""定位并读取佳明导出归档。

佳明「导出您的数据」给出的是一个 zip，内部结构如下（节选）::

    DI_CONNECT/DI-Connect-Fitness/*_summarizedActivities.json
    DI_CONNECT/DI-Connect-Aggregator/UDSFile_*.json
    DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip   <- 嵌套 zip，内含全部 .fit

本模块把「一个 zip / 一个已解压目录 / 含多个包的父目录」统一成只读视图，
并对嵌套 zip 提供同样的接口，供上层解析器逐个取文件。
"""

from __future__ import annotations

import fnmatch
import io
import json
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterator

# 导出包外层若解压过，根目录下通常有这个标记目录
_ARCHIVE_SENTINELS = ("DI_CONNECT", "DI-Connect-Fitness", "DI_CONNECT_IQ")


class ArchiveError(RuntimeError):
    """归档无法读取。"""


class Archive:
    """单个导出包的只读视图。source 可以是 zip 文件、目录、或内存中的 zip 字节。"""

    def __init__(self, source: Path | bytes, label: str = "") -> None:
        self._zip: zipfile.ZipFile | None = None
        self.label = label
        self.path: Path | None = None

        if isinstance(source, bytes):
            self._zip = zipfile.ZipFile(io.BytesIO(source))
            self.label = label or "<memory>"
            self._names = self._zip_names()
        else:
            path = Path(source)
            if path.is_dir():
                self.path = path
                self.label = label or path.name
                self._names = sorted(
                    p.relative_to(path).as_posix()
                    for p in path.rglob("*")
                    if p.is_file()
                )
            elif path.suffix.lower() == ".zip":
                self.path = path
                self.label = label or path.name
                self._zip = zipfile.ZipFile(path)
                self._names = self._zip_names()
            else:
                raise ArchiveError(f"不支持的归档类型：{path}（需要 .zip 或目录）")

    # ---------- 生命周期 ----------

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 内部 ----------

    def _zip_names(self) -> list[str]:
        assert self._zip is not None
        return [n for n in self._zip.namelist() if not n.endswith("/")]

    # ---------- 只读接口 ----------

    @property
    def names(self) -> list[str]:
        """包内全部文件路径（使用 / 分隔，不含目录条目）。"""
        return list(self._names)

    def __len__(self) -> int:
        return len(self._names)

    def find(self, pattern: str, match_path: bool = False) -> list[str]:
        """按 glob 匹配文件。

        match_path=False（默认）只匹配文件名部分，例如 `*_sleepData.json`。
        """
        out: list[str] = []
        for name in self._names:
            candidate = name if match_path else name.rsplit("/", 1)[-1]
            if fnmatch.fnmatch(candidate, pattern):
                out.append(name)
        return out

    def has(self, name: str) -> bool:
        return name in self._names

    def read(self, name: str) -> bytes:
        if name not in self._names:
            raise ArchiveError(f"{self.label} 中不存在 {name}")
        if self._zip is not None:
            return self._zip.read(name)
        assert self.path is not None
        return (self.path / name).read_bytes()

    def open(self, name: str) -> BinaryIO:
        """以二进制流打开，供 fitdecode 等流式解析器使用。"""
        if name not in self._names:
            raise ArchiveError(f"{self.label} 中不存在 {name}")
        if self._zip is not None:
            return self._zip.open(name)
        assert self.path is not None
        return open(self.path / name, "rb")

    def read_json(self, name: str) -> object:
        # 佳明部分 JSON 带 BOM，用 utf-8-sig 兼容
        return json.loads(self.read(name).decode("utf-8-sig"))

    def nested(self, name: str) -> "Archive":
        """把包内的嵌套 zip 当作独立归档打开。"""
        return Archive(self.read(name), label=f"{self.label}!{name}")


def looks_like_archive(path: Path) -> bool:
    """判断是否是一个佳明导出包（zip 或解压后的目录）。"""
    if path.is_file():
        return path.suffix.lower() == ".zip"
    if path.is_dir():
        return any((path / s).exists() for s in _ARCHIVE_SENTINELS)
    return False


class Dataset:
    """一个数据目录下所有导出包的聚合视图。

    用户可能导出过多次，因此允许存在多个包；查找时跨包返回全部命中。
    """

    def __init__(self, archives: list[Archive]) -> None:
        self.archives = archives

    def close(self) -> None:
        for a in self.archives:
            a.close()

    def __enter__(self) -> "Dataset":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self.archives)

    def find(self, pattern: str, match_path: bool = False) -> Iterator[tuple[Archive, str]]:
        for archive in self.archives:
            for name in archive.find(pattern, match_path=match_path):
                yield archive, name

    def find_one(self, pattern: str, match_path: bool = False) -> tuple[Archive, str] | None:
        return next(self.find(pattern, match_path=match_path), None)

    def find_all(self, pattern: str, match_path: bool = False) -> list[tuple[Archive, str]]:
        return list(self.find(pattern, match_path=match_path))


def discover(data_dir: Path) -> Dataset:
    """扫描数据目录，收集所有导出包。

    支持：
    - 直接放 zip
    - 解压后的目录（内部含 DI_CONNECT 等标记）
    - 上述两者混放
    """
    if not data_dir.exists():
        raise ArchiveError(f"数据目录不存在：{data_dir}")

    archives: list[Archive] = []

    # 先看目录本身是否就是一个解压后的导出包
    if looks_like_archive(data_dir) and data_dir.is_dir():
        archives.append(Archive(data_dir))
    else:
        for entry in sorted(data_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if looks_like_archive(entry):
                archives.append(Archive(entry))

    if not archives:
        raise ArchiveError(
            f"在 {data_dir} 中未找到佳明导出包。\n"
            "期望看到 .zip 导出包，或解压后包含 DI_CONNECT 的目录。"
        )

    return Dataset(archives)
