"""
DracoDownloader 错误目录

集中化、可翻译、带错误码的异常体系，供 AI Agent 程序化处理。
每个错误类型对应一个稳定的字符串码，Agent 可据此选择重试/切换镜像/放弃等策略。
"""

from typing import Optional, Dict, Any
from dataclasses import dataclass


# === 错误码常量（稳定契约，不会随版本变更） ===
ERR_UNSUPPORTED_PROTOCOL = "draco.unsupported_protocol"
ERR_PROBE_FAILED = "draco.probe_failed"
ERR_HTTP_STATUS = "draco.http_status"
ERR_RANGE_NOT_SUPPORTED = "draco.range_not_supported"
ERR_DOWNLOAD_FAILED = "draco.download_failed"
ERR_MERGE_FAILED = "draco.merge_failed"
ERR_VERIFY_FAILED = "draco.verify_failed"
ERR_TIMEOUT = "draco.timeout"
ERR_CANCELLED = "draco.cancelled"
ERR_DISK_FULL = "draco.disk_full"
ERR_PERMISSION = "draco.permission_denied"
ERR_NETWORK = "draco.network"
ERR_BT_NO_PEERS = "draco.bt.no_peers"
ERR_BT_METADATA = "draco.bt.metadata"
ERR_BT_INVALID_TORRENT = "draco.bt.invalid_torrent"
ERR_M3U8_PARSE = "draco.m3u8.parse"
ERR_M3U8_DECRYPT = "draco.m3u8.decrypt"


# 可翻译消息表（key = 错误码, value = 默认中文消息）
# Agent 可通过 DracoError.message 获取，或通过 .tr() 钩子自行本地化
_DEFAULT_MESSAGES: Dict[str, str] = {
    ERR_UNSUPPORTED_PROTOCOL: "不支持的协议: {url}",
    ERR_PROBE_FAILED: "探测失败: {detail}",
    ERR_HTTP_STATUS: "HTTP 状态异常: {status} ({url})",
    ERR_RANGE_NOT_SUPPORTED: "服务器不支持 Range 请求，无法分片: {url}",
    ERR_DOWNLOAD_FAILED: "下载失败: {detail}",
    ERR_MERGE_FAILED: "分片合并失败: {detail}",
    ERR_VERIFY_FAILED: "文件校验失败: 期望 {expected}, 实际 {actual}",
    ERR_TIMEOUT: "操作超时（{seconds}s）",
    ERR_CANCELLED: "任务已取消",
    ERR_DISK_FULL: "磁盘空间不足",
    ERR_PERMISSION: "权限不足: {path}",
    ERR_NETWORK: "网络错误: {detail}",
    ERR_BT_NO_PEERS: "BT 下载未发现任何 peer",
    ERR_BT_METADATA: "BT 元数据获取失败: {detail}",
    ERR_BT_INVALID_TORRENT: "无效的 torrent 文件: {detail}",
    ERR_M3U8_PARSE: "M3U8 解析失败: {detail}",
    ERR_M3U8_DECRYPT: "M3U8 AES-128 解密失败: {detail}",
}


# 是否可重试的标记（Agent 据此决策）
_RETRYABLE_CODES = {
    ERR_NETWORK,
    ERR_TIMEOUT,
    ERR_HTTP_STATUS,
    ERR_DOWNLOAD_FAILED,
    ERR_BT_NO_PEERS,
    ERR_BT_METADATA,
}


@dataclass
class DracoError(Exception):
    """DracoDownloader 统一错误

    Attributes:
        code: 稳定错误码字符串（如 "draco.http_status"），Agent 可据此分支处理
        message: 人类可读消息（已格式化）
        retryable: 是否建议重试
        context: 任意附加上下文（供 Agent 诊断）
    """

    code: str
    message: str
    retryable: bool = False
    context: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        super().__init__(self.message)
        if self.context is None:
            self.context = {}

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典，方便 Agent 日志/JSON 上报"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "context": self.context,
        }


def make_error(code: str, **context) -> DracoError:
    """构造错误对象，自动填充默认消息和 retryable 标记

    Args:
        code: 错误码常量
        **context: 用于消息模板格式化和上下文记录

    Returns:
        DracoError 实例
    """
    template = _DEFAULT_MESSAGES.get(code, code)
    try:
        message = template.format(**context)
    except (KeyError, IndexError):
        message = template
    return DracoError(
        code=code,
        message=message,
        retryable=code in _RETRYABLE_CODES,
        context=dict(context),
    )


# === 便捷工厂函数（常用错误场景） ===

def unsupported_protocol(url: str) -> DracoError:
    return make_error(ERR_UNSUPPORTED_PROTOCOL, url=url)

def http_status_error(status: int, url: str) -> DracoError:
    return make_error(ERR_HTTP_STATUS, status=status, url=url)

def probe_failed(detail: str) -> DracoError:
    return make_error(ERR_PROBE_FAILED, detail=detail)


def network_error(detail: str) -> DracoError:
    return make_error(ERR_NETWORK, detail=detail)


def timeout_error(seconds: float) -> DracoError:
    return make_error(ERR_TIMEOUT, seconds=seconds)


def cancelled_error() -> DracoError:
    return make_error(ERR_CANCELLED)

def merge_failed(detail: str) -> DracoError:
    return make_error(ERR_MERGE_FAILED, detail=detail)


def verify_failed(expected: str, actual: str) -> DracoError:
    return make_error(ERR_VERIFY_FAILED, expected=expected, actual=actual)


def bt_no_peers() -> DracoError:
    return make_error(ERR_BT_NO_PEERS)


def bt_metadata_error(detail: str) -> DracoError:
    return make_error(ERR_BT_METADATA, detail=detail)


def bt_invalid_torrent(detail: str) -> DracoError:
    return make_error(ERR_BT_INVALID_TORRENT, detail=detail)


def m3u8_parse_error(detail: str) -> DracoError:
    return make_error(ERR_M3U8_PARSE, detail=detail)


def m3u8_decrypt_error(detail: str) -> DracoError:
    return make_error(ERR_M3U8_DECRYPT, detail=detail)


__all__ = [
    "DracoError",
    "make_error",
    # 错误码常量
    "ERR_UNSUPPORTED_PROTOCOL",
    "ERR_PROBE_FAILED",
    "ERR_HTTP_STATUS",
    "ERR_RANGE_NOT_SUPPORTED",
    "ERR_DOWNLOAD_FAILED",
    "ERR_MERGE_FAILED",
    "ERR_VERIFY_FAILED",
    "ERR_TIMEOUT",
    "ERR_CANCELLED",
    "ERR_DISK_FULL",
    "ERR_PERMISSION",
    "ERR_NETWORK",
    "ERR_BT_NO_PEERS",
    "ERR_BT_METADATA",
    "ERR_BT_INVALID_TORRENT",
    "ERR_M3U8_PARSE",
    "ERR_M3U8_DECRYPT",
    # 便捷工厂
    "unsupported_protocol",
    "http_status_error",
    "probe_failed",
    "network_error",
    "timeout_error",
    "cancelled_error",
    "merge_failed",
    "verify_failed",
    "bt_no_peers",
    "bt_metadata_error",
    "bt_invalid_torrent",
    "m3u8_parse_error",
    "m3u8_decrypt_error",
]
