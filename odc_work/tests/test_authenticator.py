"""authenticator 测试"""
import pytest

from opendracocli.security.authenticator import Authenticator


@pytest.fixture
def auth(tmp_path):
    return Authenticator(hash_path=tmp_path / "auth.hash", iterations=10000)


def test_not_configured_initially(auth):
    assert auth.is_configured() is False


def test_set_password_and_verify(auth):
    auth.set_password("mypassword")
    assert auth.is_configured() is True
    assert auth.verify_password("mypassword") is True


def test_wrong_password_fails(auth):
    auth.set_password("correct")
    assert auth.verify_password("wrong") is False


def test_empty_password(auth):
    auth.set_password("")
    assert auth.verify_password("") is True
    assert auth.verify_password("x") is False


def test_different_passwords_differ(auth):
    """不同密码产生不同哈希"""
    auth.set_password("pass1")
    # 读取哈希
    h1 = auth._hash_path.read_text()
    auth.set_password("pass2")
    h2 = auth._hash_path.read_text()
    assert h1 != h2


def test_hash_file_permissions(auth):
    """哈希文件权限应为 600"""
    auth.set_password("secret")
    import os
    mode = os.stat(auth._hash_path).st_mode & 0o777
    # Unix 600, Windows 可能不同（放宽断言）
    import sys
    if not sys.platform.startswith("win"):
        assert mode == 0o600


def test_hash_file_format(auth):
    """哈希文件格式: pbkdf2$<iter>$<salt_hex>$<hash_hex>"""
    auth.set_password("test")
    content = auth._hash_path.read_text()
    parts = content.split("$")
    assert len(parts) == 4
    assert parts[0] == "pbkdf2"
    assert int(parts[1]) == 10000
    # salt 和 hash 是有效 hex
    bytes.fromhex(parts[2])
    bytes.fromhex(parts[3])


@pytest.mark.asyncio
async def test_verify_interactive_with_callback(auth):
    """交互式验证（注入回调）"""
    auth.set_password("secret123")
    auth.set_test_callback(lambda: "secret123")
    assert await auth.verify() is True


@pytest.mark.asyncio
async def test_verify_interactive_wrong(auth):
    auth.set_password("right")
    auth.set_test_callback(lambda: "wrong")
    assert await auth.verify() is False


@pytest.mark.asyncio
async def test_verify_not_configured(auth):
    """未配置时 verify 返回 False"""
    assert await auth.verify() is False


def test_corrupted_hash_file(auth, tmp_path):
    """损坏的哈希文件 → verify 返回 False"""
    auth._hash_path.parent.mkdir(parents=True, exist_ok=True)
    auth._hash_path.write_text("garbage_not_valid")
    assert auth.verify_password("anything") is False
