"""运行配置：数据目录、缓存目录、隐私级别。

全部可通过环境变量覆盖，避免把任何路径写死在代码里
（开源项目的硬编码路径会让别人 clone 下来直接跑不通）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PRIVACY_LEVELS = ("strict", "normal", "raw")
DEFAULT_PRIVACY = "strict"


class ConfigError(RuntimeError):
    """配置非法或数据目录不可用。"""


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


@dataclass
class Config:
    data_dir: Path
    cache_dir: Path
    privacy: str = DEFAULT_PRIVACY

    @classmethod
    def load(
        cls,
        data_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        privacy: str | None = None,
    ) -> "Config":
        """优先级：显式参数 > 环境变量 > 当前目录下的 data/ 与 cache/。"""
        resolved_data = (
            Path(data_dir).expanduser()
            if data_dir
            else _env_path("GARMAPLE_DATA") or Path.cwd() / "data"
        )
        resolved_cache = (
            Path(cache_dir).expanduser()
            if cache_dir
            else _env_path("GARMAPLE_CACHE") or Path.cwd() / "cache"
        )
        resolved_privacy = (
            privacy or os.environ.get("GARMAPLE_PRIVACY", DEFAULT_PRIVACY)
        ).strip().lower()

        if resolved_privacy not in PRIVACY_LEVELS:
            raise ConfigError(
                f"未知的隐私级别 {resolved_privacy!r}，可选：{', '.join(PRIVACY_LEVELS)}"
            )

        return cls(
            data_dir=resolved_data.resolve(),
            cache_dir=resolved_cache.resolve(),
            privacy=resolved_privacy,
        )

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "garmaple.sqlite3"

    def require_data(self) -> Path:
        if not self.data_dir.exists():
            raise ConfigError(
                f"数据目录不存在：{self.data_dir}\n"
                "请从 Garmin 官方导出数据，把下载的 .zip（或解压后的文件夹）\n"
                "放进该目录（默认找当前目录下的 data/，见 README 开始使用）。"
            )
        return self.data_dir
