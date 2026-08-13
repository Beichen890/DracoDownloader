"""agent/templates 测试"""
import pytest

from opendracocli.agent.code_runner import CodeRunner
from opendracocli.agent.templates import (
    TEMPLATES,
    FunctionTemplate,
    get_template,
    list_templates,
)


def test_list_templates_nonempty():
    templates = list_templates()
    assert len(templates) > 0


def test_list_templates_sorted_by_name():
    templates = list_templates()
    names = [t.name for t in templates]
    assert names == sorted(names)


def test_get_template_exists():
    tpl = get_template("deploy")
    assert isinstance(tpl, FunctionTemplate)
    assert tpl.name == "deploy"
    assert "def deploy" in tpl.code
    assert tpl.description


def test_get_template_not_found():
    with pytest.raises(KeyError):
        get_template("nonexistent_template")


def test_all_templates_have_description():
    for name, tpl in TEMPLATES.items():
        assert tpl.description, f"模板 {name} 缺少 description"


def test_all_templates_compile():
    """所有模板必须是合法 Python 语法"""
    runner = CodeRunner()
    for name, tpl in TEMPLATES.items():
        violations = runner.check_safety(tpl.code, strict=False)
        # 模板是用户函数示例，不应有 dunder 违规
        assert violations == [], f"模板 {name} 有违规: {violations}"


def test_all_templates_define_function():
    """每个模板必须定义至少一个函数"""
    runner = CodeRunner()
    for name, tpl in TEMPLATES.items():
        funcs = runner.extract_functions(tpl.code)
        assert len(funcs) >= 1, f"模板 {name} 未定义函数"


def test_deploy_template_uses_ctx_shell():
    """deploy 模板应使用 ctx.shell（代码即行动）"""
    tpl = get_template("deploy")
    assert "ctx.shell" in tpl.code or "await ctx.shell" in tpl.code


def test_clean_logs_template_has_days_param():
    tpl = get_template("clean_logs")
    assert "days" in tpl.code


def test_known_templates_exist():
    """验证关键模板存在"""
    expected = {"deploy", "clean_logs", "gitstat", "backup", "ports"}
    assert expected.issubset(set(TEMPLATES.keys()))
