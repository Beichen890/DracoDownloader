"""
DracoDownloader 配置系统

提供带校验器的配置项 (ConfigItem)，支持：
- 环境变量覆盖（Agent 部署友好）
- 类型校验与范围检查
- 全局单例 DracoConfig 聚合所有配置
- JSON 序列化/反序列化（持久化到磁盘）

设计原则：不引入第三方库，纯 dataclass + typing 实现。
"""

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional, Type, Callable, List, TypeVar, Union

from .logger import get_logger

log = get_logger('config')

T = TypeVar('T')


# === 校验器 ===

class Validator:
    """校验器基类"""

    def validate(self, value: Any) -> Any:
        """校验并可能规范化值，返回最终值"""
        raise NotImplementedError


@dataclass
class RangeValidator(Validator):
    """数值范围校验器"""
    min_value: float
    max_value: float

    def validate(self, value: Any) -> float:
        v = float(value)
        if v < self.min_value or v > self.max_value:
            raise ValueError(
                f"值 {v} 超出范围 [{self.min_value}, {self.max_value}]"
            )
        return v


@dataclass
class ChoiceValidator(Validator):
    """枚举校验器"""
    choices: List[str]

    def validate(self, value: Any) -> str:
        s = str(value)
        if s not in self.choices:
            raise ValueError(f"值 '{s}' 不在允许列表 {self.choices} 中")
        return s


class IntValidator(Validator):
    """整数校验器"""

    def __init__(self, min_value: Optional[int] = None, max_value: Optional[int] = None):
        self.min = min_value
        self.max = max_value

    def validate(self, value: Any) -> int:
        v = int(value)
        if self.min is not None and v < self.min:
            raise ValueError(f"值 {v} 小于最小值 {self.min}")
        if self.max is not None and v > self.max:
            raise ValueError(f"值 {v} 大于最大值 {self.max}")
        return v


class BoolValidator(Validator):
    """布尔校验器（接受 1/0/true/false/yes/no）"""

    TRUE_SET = {"1", "true", "yes", "on", "True"}

    def validate(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        return s in self.TRUE_SET


class PathValidator(Validator):
    """路径校验器"""

    def validate(self, value: Any) -> str:
        p = Path(str(value)).expanduser()
        return str(p.resolve())


# === 配置项 ===

@dataclass
class ConfigItem:
    """单个配置项

    Attributes:
        key: 配置键（也用作环境变量名 DRACO_<KEY 大写>）
        default: 默认值
        description: 描述（供 Agent 理解）
        validator: 校验器（可选）
        env_var: 显式指定的环境变量名（默认根据 key 推导）
    """
    key: str
    default: Any
    description: str = ""
    validator: Optional[Validator] = None
    env_var: Optional[str] = None
    _value: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        # 先设默认值，再尝试从环境变量加载
        self._value = self.default
        env_name = self.env_var or f"DRACO_{self.key.upper()}"
        if env_name in os.environ:
            self.set(os.environ[env_name])

    @property
    def value(self) -> Any:
        return self._value

    def set(self, value: Any) -> Any:
        """设置值（带校验）"""
        if self.validator is not None:
            value = self.validator.validate(value)
        self._value = value
        return value

    def reset(self):
        """重置为默认值"""
        self._value = self.default


# === 全局配置容器 ===

class DracoConfig:
    """DracoDownloader 全局配置

    集中管理所有可调参数，Agent 可通过 DracoDownloader.config 访问。
    """

    def __init__(self):
        self._items: dict[str, ConfigItem] = {}

        # === 基础 ===
        self._register(ConfigItem(
            key="max_concurrent",
            default=5,
            description="最大并发下载数",
            validator=IntValidator(min_value=1, max_value=64),
        ))
        self._register(ConfigItem(
            key="task_timeout",
            default=3600,
            description="单任务超时秒数",
            validator=IntValidator(min_value=10, max_value=86400),
        ))

        # === HTTP 优化 ===
        self._register(ConfigItem(
            key="http_max_connections",
            default=64,
            description="HTTP 最大连接数",
            validator=IntValidator(min_value=1, max_value=512),
        ))
        self._register(ConfigItem(
            key="http_chunk_size",
            default=1024 * 1024,
            description="HTTP 分片大小（字节）",
            validator=RangeValidator(min_value=64 * 1024, max_value=256 * 1024 * 1024),
        ))
        self._register(ConfigItem(
            key="http_merge_buffer",
            default=16 * 1024 * 1024,
            description="HTTP 合并缓冲区大小（字节）",
            validator=RangeValidator(min_value=1024 * 1024, max_value=256 * 1024 * 1024),
        ))
        self._register(ConfigItem(
            key="auto_optimize",
            default=True,
            description="是否启用自动参数优化",
            validator=BoolValidator(),
        ))
        self._register(ConfigItem(
            key="auto_mirror",
            default=False,
            description="是否启用自动镜像选择",
            validator=BoolValidator(),
        ))
        self._register(ConfigItem(
            key="mirror_region",
            default="cn",
            description="镜像区域",
            validator=ChoiceValidator(["cn", "global", "auto"]),
        ))

        # === BitTorrent ===
        self._register(ConfigItem(
            key="bt_max_connections",
            default=20,
            description="BT 最大 peer 连接数",
            validator=IntValidator(min_value=1, max_value=200),
        ))
        self._register(ConfigItem(
            key="bt_enable_dht",
            default=True,
            description="是否启用 DHT",
            validator=BoolValidator(),
        ))
        self._register(ConfigItem(
            key="bt_enable_sequential",
            default=False,
            description="是否启用顺序下载（边下边看）",
            validator=BoolValidator(),
        ))
        self._register(ConfigItem(
            key="bt_seeding_enabled",
            default=False,
            description="下载完成后是否做种",
            validator=BoolValidator(),
        ))
        self._register(ConfigItem(
            key="bt_seeding_ratio_limit",
            default=0.0,
            description="做种分享率上限（0=不限）",
            validator=RangeValidator(min_value=0.0, max_value=100.0),
        ))
        self._register(ConfigItem(
            key="bt_seeding_time_limit",
            default=0.0,
            description="做种时长上限（秒，0=不限）",
            validator=RangeValidator(min_value=0.0, max_value=86400.0),
        ))
        self._register(ConfigItem(
            key="bt_web_trackers_enabled",
            default=True,
            description="是否启用 Web Tracker 自动合并",
            validator=BoolValidator(),
        ))

        # === M3U8 ===
        self._register(ConfigItem(
            key="m3u8_max_concurrent",
            default=16,
            description="M3U8 最大并发分片数",
            validator=IntValidator(min_value=1, max_value=128),
        ))

        # === 日志 ===
        self._register(ConfigItem(
            key="log_level",
            default="INFO",
            description="日志级别",
            validator=ChoiceValidator(["DEBUG", "INFO", "WARNING", "ERROR"]),
        ))

    def _register(self, item: ConfigItem):
        self._items[item.key] = item

    def get(self, key: str) -> Any:
        """获取配置值"""
        item = self._items.get(key)
        if item is None:
            raise KeyError(f"未知配置项: {key}")
        return item.value

    def set(self, key: str, value: Any) -> Any:
        """设置配置值"""
        item = self._items.get(key)
        if item is None:
            raise KeyError(f"未知配置项: {key}")
        return item.set(value)

    def items(self) -> dict[str, ConfigItem]:
        return dict(self._items)

    def describe(self) -> list[dict[str, Any]]:
        """列出所有配置项（供 Agent 自省）"""
        return [
            {
                "key": k,
                "value": v.value,
                "default": v.default,
                "description": v.description,
                "env_var": v.env_var or f"DRACO_{k.upper()}",
            }
            for k, v in self._items.items()
        ]

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {k: v.value for k, v in self._items.items()}

    def save(self, path: Union[str, Path]):
        """保存到 JSON 文件"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        log.info(f"配置已保存到 {p}")

    def load(self, path: Union[str, Path]):
        """从 JSON 文件加载（覆盖现有值）"""
        p = Path(path)
        if not p.exists():
            return
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for k, v in data.items():
            if k in self._items:
                try:
                    self._items[k].set(v)
                except (ValueError, TypeError) as e:
                    log.warning(f"加载配置 {k}={v} 失败: {e}")


# 全局默认配置实例（惰性创建）
_global_config: Optional[DracoConfig] = None


def get_global_config() -> DracoConfig:
    """获取全局配置实例（单例）"""
    global _global_config
    if _global_config is None:
        _global_config = DracoConfig()
    return _global_config


__all__ = [
    "Validator",
    "RangeValidator",
    "ChoiceValidator",
    "IntValidator",
    "BoolValidator",
    "PathValidator",
    "ConfigItem",
    "DracoConfig",
    "get_global_config",
]
