"""提交前静态检查。

定位说明：这是**提示性护栏，不是沙箱**。真正的隔离要靠进程权限与系统机制。
本模块的作用是在学生提交"明显在做危险事情"的代码时先给一次确认机会，
防止误操作（例如把评测环境里的文件删了）。

每条规则都带中文说明，学生看得懂"为什么被拦"，比只报一个正则表达式友好得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Language


@dataclass(frozen=True)
class SecurityRule:
    pattern: re.Pattern[str]
    label: str
    languages: tuple[Language, ...] = ()


def _rule(regex: str, label: str, *languages: Language) -> SecurityRule:
    return SecurityRule(re.compile(regex), label, tuple(languages))


RULES: tuple[SecurityRule, ...] = (
    # ---- 进程与命令执行 ----
    _rule(r"\bsystem\s*\(", "调用 system() 执行外部命令", Language.CPP, Language.C),
    _rule(r"\bpopen\s*\(", "调用 popen() 执行外部命令", Language.CPP, Language.C),
    _rule(r"\bexec[lv]p?e?\s*\(", "调用 exec* 系列函数替换进程映像", Language.CPP, Language.C),
    _rule(r"\bfork\s*\(", "调用 fork() 创建子进程", Language.CPP, Language.C),
    _rule(r"\bWinExec\b|\bShellExecute\b|\bCreateProcess\b", "调用 Win32 API 启动进程",
          Language.CPP, Language.C),
    _rule(r"\bsubprocess\b", "使用 subprocess 模块启动进程", Language.PYTHON),
    _rule(r"\bos\.(system|popen|spawn\w*|exec\w*)\b", "使用 os 模块执行命令", Language.PYTHON),
    _rule(r"\bProcessBuilder\b|\bRuntime\s*\.\s*getRuntime\b", "启动外部进程", Language.JAVA),
    # ---- 网络 ----
    _rule(r"\bsocket\s*\.", "使用 socket 建立网络连接"),
    _rule(r"\bURLConnection\b|\bHttpURLConnection\b", "发起 HTTP 请求"),
    _rule(r"\brequests\s*\.|\burllib\b|\bhttp\.client\b", "发起网络请求", Language.PYTHON),
    # ---- 动态求值 / 反射 ----
    _rule(r"__import__\s*\(", "使用 __import__ 动态导入模块", Language.PYTHON),
    _rule(r"\beval\s*\(", "使用 eval 动态求值", Language.PYTHON),
    _rule(r"\bexec\s*\(", "使用 exec 动态执行代码", Language.PYTHON),
    # ---- 文件系统破坏性操作 ----
    _rule(r"\b(shutil\.rmtree|os\.remove|os\.unlink|os\.rmdir)\b",
          "删除文件或目录", Language.PYTHON),
    _rule(r"\bremove\s*\(|\bunlink\s*\(|\brmdir\s*\(", "删除文件或目录",
          Language.CPP, Language.C),
    _rule(r"File\s*\.\s*delete\b|\.deleteOnExit\s*\(", "删除文件", Language.JAVA),
    # ---- Windows 注册表 ----
    _rule(r"\bReg(Open|Set|Delete)\w*Key\b", "读写注册表"),
)


def scan(code: str, language: Language) -> list[str]:
    """返回命中的风险说明列表（去重、保序）。语言无关的规则始终生效。"""
    if not code or not code.strip():
        return []

    findings: list[str] = []
    for rule in RULES:
        if rule.languages and language not in rule.languages:
            continue
        if rule.pattern.search(code):
            findings.append(rule.label)

    # 去重保序：不同规则可能命中同一段代码，学生不需要看重复提示
    seen: set[str] = set()
    unique: list[str] = []
    for item in findings:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def format_findings(findings: list[str]) -> str:
    """把检查结果整理成对话框正文。"""
    if not findings:
        return ""
    lines = ["检测到以下可能具有风险的操作：", ""]
    lines.extend(f"  · {item}" for item in findings)
    lines.extend([
        "",
        "继续执行意味着在本机直接运行这段代码。",
        "如果这是你自己写的练习代码，可以继续；否则建议先看清楚再决定。",
    ])
    return "\n".join(lines)
