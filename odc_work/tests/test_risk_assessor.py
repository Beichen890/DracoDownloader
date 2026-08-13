"""risk_assessor 测试"""
import json

import pytest

from opendracocli.shell.ir import CommandIR, CommandNode
from opendracocli.shell.parser import parse
from opendracocli.security.risk_assessor import (
    RiskAssessment,
    RiskAssessor,
    RiskLevel,
)


@pytest.fixture
def assessor():
    """用包内默认规则表"""
    from opendracocli.config import DracoConfig

    cfg = DracoConfig()
    return RiskAssessor(builtin_rules_path=cfg.builtin_rules_path)


def test_safe_command(assessor):
    ir = parse("echo hello")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.SAFE
    assert result.matched_rules == []


def test_caution_rm_single(assessor):
    ir = parse("rm file.txt")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CAUTION
    assert len(result.matched_rules) == 1


def test_danger_rm_rf(assessor):
    ir = parse("rm -rf /tmp/test")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.DANGER


def test_critical_rm_rf_root(assessor):
    ir = parse("rm -rf /")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CRITICAL


def test_critical_mkfs(assessor):
    ir = parse("mkfs /dev/sda1")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CRITICAL


def test_critical_dd_to_device(assessor):
    ir = parse("dd if=image.iso of=/dev/sdb")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CRITICAL


def test_danger_kill_9(assessor):
    ir = parse("kill -9 1234")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.DANGER


def test_caution_kill_default(assessor):
    ir = parse("kill 1234")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CAUTION


def test_multiple_nodes_takes_highest(assessor):
    """多节点取最高等级：echo && rm -rf /"""
    ir = parse("echo hi && rm -rf /")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CRITICAL
    assert len(result.matched_rules) == 1


def test_case_insensitive_command(assessor):
    ir = parse("RM file.txt")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.CAUTION


def test_is_high_risk_property(assessor):
    assert RiskAssessment(level=RiskLevel.SAFE).is_high_risk is False
    assert RiskAssessment(level=RiskLevel.CAUTION).is_high_risk is False
    assert RiskAssessment(level=RiskLevel.DANGER).is_high_risk is True
    assert RiskAssessment(level=RiskLevel.CRITICAL).is_high_risk is True


def test_user_rules_merge(tmp_path):
    """用户规则文件扩展覆盖"""
    from opendracocli.config import DracoConfig

    cfg = DracoConfig()
    # 用户追加一条 danger 规则：自定义命令 purge
    user_rules = {
        "danger": {
            "patterns": [
                {"cmd": "purge", "args_contain": ["--all"], "desc": "清空所有数据"}
            ]
        }
    }
    user_path = tmp_path / "user_rules.json"
    user_path.write_text(json.dumps(user_rules), encoding="utf-8")

    assessor = RiskAssessor(
        builtin_rules_path=cfg.builtin_rules_path,
        user_rules_path=user_path,
    )
    ir = parse("purge --all")
    result = assessor.assess(ir)
    assert result.level == RiskLevel.DANGER


def test_reason_contains_desc(assessor):
    ir = parse("rm -rf /")
    result = assessor.assess(ir)
    assert "递归强删根目录" in result.reason or "删除根目录" in result.reason


def test_risk_level_max():
    assert RiskLevel.max(RiskLevel.SAFE, RiskLevel.CAUTION) == RiskLevel.CAUTION
    assert RiskLevel.max(RiskLevel.DANGER, RiskLevel.CAUTION) == RiskLevel.DANGER
    assert RiskLevel.max(RiskLevel.CRITICAL, RiskLevel.DANGER) == RiskLevel.CRITICAL
    assert RiskLevel.max() == RiskLevel.SAFE


def test_risk_level_severity():
    assert RiskLevel.SAFE.severity < RiskLevel.CAUTION.severity
    assert RiskLevel.CAUTION.severity < RiskLevel.DANGER.severity
    assert RiskLevel.DANGER.severity < RiskLevel.CRITICAL.severity
