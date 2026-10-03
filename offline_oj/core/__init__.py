"""评测内核。

本层**不导入任何 UI 相关模块**（不使用 PySide6），因此可以在无图形界面的环境里
单独测试，也可以被命令行入口、批处理脚本复用。

模块职责::

    models.py       领域模型：Language / Verdict / TestCase / Problem
    validation.py   路径合法性校验（防止注入与误操作）
    compilers.py    编译器探测与版本排序
    sandbox.py      运行结果、编译器互斥锁、进程监控（时间/内存限制）
    runners.py      各语言"编译 + 运行"实现
    judge.py        评测流程编排（产出事件流，由 UI 渲染）
    checker.py      自定义校验器（SPJ）的编译与调用
    security.py     提交前危险调用静态检查
    repository.py   题库持久化（原子写、备份、ID 生成）
    archive.py      单题 / 批量题目的导入导出（ZIP、文件夹）
    records.py      整场测验的留档与读档（exams/<时间戳>-<标题>/）
    export.py       成绩单 / 提交明细导出（txt / csv / xlsx / docx / PDF 的 HTML）
    roster.py       选手名单（CSV 导入导出、口令补发）
    credentials.py  账号 / 口令 / 身份摘要的**单一真源**（core 与 net 必须逐字节一致）
    similarity.py   雷同检测（完全重复 + winnowing 高度相似），纯标准库
"""

from __future__ import annotations

from .models import Language, Problem, TestCase, Verdict

__all__ = ["Language", "Problem", "TestCase", "Verdict"]
