"""身份验证 — PBKDF2-HMAC-SHA256 密码哈希

哈希文件格式: pbkdf2$<iterations>$<salt_hex>$<hash_hex>
文件权限 600（仅 owner 可读）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import stat
from pathlib import Path
from typing import Callable, Optional

from ..errors import ERR_AUTH_FAILED, make_error
from ..logger import get_logger

log = get_logger("security.authenticator")


def _hash_password(
    password: str, salt: bytes, iterations: int
) -> bytes:
    """PBKDF2-HMAC-SHA256"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )


class Authenticator:
    """身份验证器"""

    def __init__(
        self,
        hash_path: Path,
        iterations: int = 100000,
    ) -> None:
        self._hash_path = hash_path
        self._iterations = iterations
        self._test_callback: Optional[Callable[[], str]] = None

    def set_test_callback(self, cb: Callable[[], str]) -> None:
        """注入测试回调：cb() -> password_str"""
        self._test_callback = cb

    def clear_test_callback(self) -> None:
        self._test_callback = None

    def is_configured(self) -> bool:
        """是否已设置密码"""
        return self._hash_path.exists()

    def set_password(self, password: str) -> None:
        """设置密码（哈希存储）

        生成随机盐，PBKDF2 哈希，写入文件（权限 600）。
        """
        salt = os.urandom(16)
        digest = _hash_password(password, salt, self._iterations)
        line = f"pbkdf2${self._iterations}${salt.hex()}${digest.hex()}"

        self._hash_path.parent.mkdir(parents=True, exist_ok=True)
        # 以 600 权限创建
        fd = os.open(
            str(self._hash_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        # 确保权限（umask 可能影响）
        try:
            os.chmod(self._hash_path, 0o600)
        except OSError:
            pass
        log.info("password set: %s", self._hash_path)

    def _load_stored(self) -> Optional[tuple[int, bytes, bytes]]:
        """加载已存储的 (iterations, salt, digest)"""
        if not self._hash_path.exists():
            return None
        try:
            content = self._hash_path.read_text(encoding="utf-8").strip()
            parts = content.split("$")
            if len(parts) != 4 or parts[0] != "pbkdf2":
                log.error("invalid hash file format")
                return None
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            digest = bytes.fromhex(parts[3])
            return iterations, salt, digest
        except (OSError, ValueError) as e:
            log.error("failed to load hash: %s", e)
            return None

    def verify_password(self, password: str) -> bool:
        """验证密码（不交互，纯比对）"""
        stored = self._load_stored()
        if stored is None:
            return False
        iterations, salt, expected = stored
        actual = _hash_password(password, salt, iterations)
        return hmac.compare_digest(actual, expected)

    async def verify(self) -> bool:
        """交互式验证：prompt 用户输入密码，比对哈希

        Returns:
            True=验证通过, False=失败
        """
        if not self.is_configured():
            return False

        password = await self._prompt_password("请输入密码以确认高危操作: ")
        if password is None:
            return False
        return self.verify_password(password)

    async def _prompt_password(self, prompt: str) -> Optional[str]:
        """隐藏输入密码"""
        if self._test_callback is not None:
            return self._test_callback()

        try:
            from prompt_toolkit import prompt as pt_prompt
            from prompt_toolkit.completion import DummyCompleter

            # prompt_toolkit 的 is_password 参数隐藏输入
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: pt_prompt(prompt, is_password=True),
            )
            return result
        except (ImportError, EOFError, KeyboardInterrupt):
            return None
