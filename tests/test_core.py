"""core 层单元测试。

不依赖 PySide6，因此可以在无图形环境的 CI 里跑：

    python -m pytest tests -q
    python -m unittest discover -s tests -v

覆盖重点是那些"出错会静默丢数据"的地方：输出比对、题库落盘与备份、
导入导出往返、目录穿越防护、设置损坏隔离。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from offline_oj.core.archive import ProblemArchive                    # noqa: E402
from offline_oj.core.judge import KIND_CHECKER, Judge                 # noqa: E402
from offline_oj.core.models import (                                  # noqa: E402
    CHECKER_LANGUAGES,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    DEFAULT_TESTCASE_POINTS,
    FALLBACK_SLUG,
    IOMode,
    JudgeConfig,
    Language,
    Problem,
    TASK_NAME_PLACEHOLDER,
    TestCase,
    Verdict,
    sanitize_slug,
)
from offline_oj.core.repository import (                              # noqa: E402
    RENAME,
    SKIP,
    ProblemRepository,
    SubmissionLog,
    SubmissionRecord,
)
from offline_oj.core.compilers import (                               # noqa: E402
    _EXTRA_DIR_TEMPLATES,
    _GLOB_TEMPLATES,
    CompilerDetector,
    extra_dirs,
    glob_dirs,
)
from offline_oj.win32.volumes import fixed_drives                     # noqa: E402
from offline_oj.core.runners import (                                 # noqa: E402
    CPP_STANDARD_LADDER,
    C_STANDARD_LADDER,
    JavaRunner,
    MsvcProfile,
    _STANDARD_CACHE,
    _cache_key,
    _diagnose,
    _launch_failure,
    _standard_rejected,
    compare_output,
    diff_summary,
    executable_name,
    make_runner,
    profile_for,
)
from offline_oj.core.security import scan                             # noqa: E402
from offline_oj.core.validation import PathValidator                  # noqa: E402
from offline_oj.settings import AppSettings                           # noqa: E402


def make_problem(problem_id: str = "P0001", *, cases: int = 2) -> Problem:
    return Problem(
        id=problem_id,
        title="两数求和",
        description="## 描述\n读入两个整数，输出它们的和。",
        time_limit=1000,
        memory_limit=128,
        testcases=[TestCase(input=f"{i} {i + 1}\n", output=f"{2 * i + 1}\n")
                   for i in range(cases)],
    )


class OutputComparisonTest(unittest.TestCase):
    """输出比对必须宽容无关差异，但严格对待真实差异。"""

    def test_ignores_trailing_whitespace_and_newlines(self) -> None:
        self.assertTrue(compare_output("1 2\r\n3  \n\n", "1 2\n3"))

    def test_detects_real_difference(self) -> None:
        self.assertFalse(compare_output("1 2\n4", "1 2\n3"))

    def test_leading_blank_lines_tolerated(self) -> None:
        self.assertTrue(compare_output("\n\n42\n", "42"))

    def test_diff_summary_points_at_first_mismatch(self) -> None:
        summary = diff_summary("1\n9\n3", "1\n2\n3")
        self.assertIn("第 2 行", summary)


class ModelTest(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        original = make_problem()
        restored = Problem.from_dict(original.to_dict())
        self.assertEqual(restored.id, original.id)
        self.assertEqual(len(restored.testcases), len(original.testcases))
        self.assertEqual(restored.testcases[0].output, original.testcases[0].output)

    def test_legacy_payload_without_optional_fields(self) -> None:
        legacy = {
            "id": "P1",
            "title": "旧题",
            "description": "desc",
            "time_limit": 500,
            "memory_limit": 64,
            "testcases": [{"input": "1", "output": "1"}],
        }
        problem = Problem.from_dict(legacy)
        self.assertEqual(problem.time_limit, 500)
        self.assertEqual(problem.image_refs(), [])

    def test_validate_reports_missing_pieces(self) -> None:
        problem = Problem(id="", title="", description="", testcases=[])
        issues = problem.validate()
        self.assertGreaterEqual(len(issues), 3)

    def test_image_refs_skip_remote_urls(self) -> None:
        problem = make_problem()
        problem.description = "![a](local.png) ![b](https://x/y.png)"
        self.assertEqual(problem.image_refs(), ["local.png"])

    def test_language_metadata(self) -> None:
        self.assertEqual(Language.JAVA.key_paths, ("javac", "java"))
        self.assertEqual(Language.CPP.suffix, ".cpp")


class PointValueTest(unittest.TestCase):
    """测试点分值：NOI 是按测试点给分的，于是每个点自带分值、满分是它们的和。

    "满分不再是恒定的 100"这件事会一路影响到榜单上的每一个分数，所以
    分值本身必须能原样存下来、也必须能容忍写坏的数据。
    """

    def test_default_is_ten_points(self) -> None:
        self.assertEqual(DEFAULT_TESTCASE_POINTS, 10)
        self.assertEqual(TestCase(input="1", output="1").points, 10)

    def test_total_points_is_the_sum(self) -> None:
        problem = Problem(id="P1", title="题", description="描述", testcases=[
            TestCase(input="", output="1", points=50),
            TestCase(input="", output="1", points=30),
            TestCase(input="", output="1", points=20),
        ])
        self.assertEqual(problem.total_points, 100)

    def test_a_testcaseless_problem_has_no_points_at_all(self) -> None:
        """没有测试点的题满分是 0，而不是"默认 100" —— 那份配置根本不合法。"""
        self.assertEqual(Problem(id="P1", title="题", description="描述").total_points, 0)

    def test_default_points_are_not_written_to_disk(self) -> None:
        """默认分值不落盘，于是旧题库的文件逐字节不变。"""
        self.assertNotIn("points", TestCase(input="1", output="1").to_dict())

    def test_custom_points_survive_a_round_trip(self) -> None:
        original = TestCase(input="1", output="1", points=25)
        self.assertEqual(TestCase.from_dict(original.to_dict()).points, 25)

    def test_legacy_payload_reads_as_ten_points(self) -> None:
        case = TestCase.from_dict({"input": "1", "output": "1"})
        self.assertEqual(case.points, 10)

    def test_broken_points_fall_back_instead_of_zeroing_the_problem(self) -> None:
        """写坏的分值当"没配过"处理。

        让它变成 0 分的话，这道题**所有人**都是 0 分，现场看起来像判题器坏了，
        比"分值不对"难查得多。
        """
        for broken in (-5, "abc", None, {"x": 1}):
            with self.subTest(value=broken):
                case = TestCase.from_dict(
                    {"input": "1", "output": "1", "points": broken})
                self.assertEqual(case.points, DEFAULT_TESTCASE_POINTS)

    def test_zero_points_is_a_legal_configuration(self) -> None:
        """0 分是合法的（比如只想看看会不会 RE 的练习点），只有负数才算写坏。"""
        case = TestCase(input="1", output="1", points=0)
        self.assertEqual(case.points, 0)
        self.assertEqual(case.to_dict()["points"], 0)


class ValidationTest(unittest.TestCase):
    def test_rejects_shell_metacharacters(self) -> None:
        self.assertFalse(PathValidator.is_safe("a; rm -rf /"))
        self.assertFalse(PathValidator.is_safe("name`whoami`.exe"))

    def test_blocks_directory_traversal(self) -> None:
        with self.assertRaises(ValueError):
            PathValidator.safe_join(tempfile.gettempdir(), "..", "evil.bat")

    def test_blocks_absolute_path(self) -> None:
        with self.assertRaises(ValueError):
            PathValidator.safe_join(tempfile.gettempdir(), "C:\\Windows\\evil.dll")

    def test_safe_join_builds_nested_path(self) -> None:
        base = tempfile.gettempdir()
        result = PathValidator.safe_join(base, "images", "a.png")
        self.assertTrue(result.replace("\\", "/").endswith("images/a.png"))

    def test_safe_filename_strips_reserved_characters(self) -> None:
        self.assertNotIn("/", PathValidator.safe_filename('a/b:c*d?e"f'))


class RepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = ProblemRepository(root / "problems.json", root / "resources").load()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_and_reload(self) -> None:
        self.repo.put(make_problem())
        self.repo.save()

        reloaded = ProblemRepository(self.repo.problems_file, self.repo.resources_dir).load()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded.get("P0001").title, "两数求和")

    def test_save_keeps_backup(self) -> None:
        self.repo.put(make_problem())
        self.repo.save()
        self.repo.put(make_problem("P0002"))
        self.repo.save()
        self.assertTrue(self.repo.problems_file.with_name("problems.json.bak").is_file())

    def test_no_temp_file_left_behind(self) -> None:
        self.repo.put(make_problem())
        self.repo.save()
        leftovers = list(self.repo.problems_file.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_conflict_strategies(self) -> None:
        self.repo.put(make_problem())
        self.assertEqual(self.repo.put(make_problem(), strategy=SKIP), "P0001")
        renamed = self.repo.put(make_problem(), strategy=RENAME)
        self.assertEqual(renamed, "P0001_1")
        self.assertEqual(len(self.repo), 2)

    def test_remove_and_rename(self) -> None:
        self.repo.put(make_problem())
        ok, message = self.repo.rename("P0001", "P2001")
        self.assertTrue(ok, message)
        self.assertIsNotNone(self.repo.get("P2001"))
        self.repo.remove("P2001")
        self.assertEqual(len(self.repo), 0)

    def test_next_id_avoids_existing(self) -> None:
        self.repo.put(make_problem("P0001"))
        self.assertEqual(self.repo.next_id(), "P1001")

    def test_search_matches_title(self) -> None:
        self.repo.put(make_problem())
        self.assertEqual(len(self.repo.search("两数求和")), 1)
        self.assertEqual(len(self.repo.search("不存在的关键词")), 0)

    def test_corrupted_file_is_quarantined(self) -> None:
        self.repo.problems_file.write_text("{ this is not json", encoding="utf-8")
        reloaded = ProblemRepository(self.repo.problems_file, self.repo.resources_dir).load()
        self.assertEqual(len(reloaded), 0)
        self.assertTrue(any(reloaded.problems_file.parent.glob("*.broken-*")))

    def test_orphan_resource_detection(self) -> None:
        self.repo.resources_dir.mkdir(parents=True, exist_ok=True)
        (self.repo.resources_dir / "used.png").write_bytes(b"x")
        (self.repo.resources_dir / "orphan.png").write_bytes(b"x")
        problem = make_problem()
        problem.description = "![a](used.png)"
        self.repo.put(problem)
        self.assertEqual(self.repo.list_orphan_resources(), ["orphan.png"])

    def test_import_resource_renames_with_uuid(self) -> None:
        source = Path(self._tmp.name) / "pic.png"
        source.write_bytes(b"png")
        name = self.repo.import_resource(source)
        self.assertNotEqual(name, "pic.png")
        self.assertTrue((self.repo.resources_dir / name).is_file())


class ArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = ProblemRepository(self.root / "data" / "problems.json",
                                     self.root / "data" / "resources").load()
        self.archive = ProblemArchive(self.repo)
        self.repo.put(make_problem("P0001"))
        self.repo.put(make_problem("P0002"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zip_round_trip(self) -> None:
        target = self.root / "export.zip"
        count = self.archive.export_zip(target, self.repo.all())
        self.assertEqual(count, 2)

        fresh = ProblemRepository(self.root / "fresh" / "problems.json",
                                 self.root / "fresh" / "resources").load()
        report = ProblemArchive(fresh).import_zip(target, strategy=RENAME)
        self.assertEqual(report.imported, 2)
        self.assertEqual(len(fresh), 2)

    def test_folder_round_trip(self) -> None:
        target = self.root / "export"
        self.archive.export_folder(target, self.repo.all())
        self.assertTrue((target / "problems" / "P0001.json").is_file())

        fresh = ProblemRepository(self.root / "fresh2" / "problems.json",
                                 self.root / "fresh2" / "resources").load()
        report = ProblemArchive(fresh).import_folder(target, strategy=SKIP)
        self.assertEqual(report.imported, 2)

    def test_skip_strategy_counts_skipped(self) -> None:
        target = self.root / "export.zip"
        self.archive.export_zip(target, self.repo.all())
        report = self.archive.import_zip(target, strategy=SKIP)
        self.assertEqual(report.skipped, 2)
        self.assertEqual(report.imported, 0)

    def test_zip_slip_is_rejected(self) -> None:
        """构造一个带 ../ 资源名的题目包，确认不会写到资源目录之外。"""
        import zipfile

        payload = make_problem("EVIL")
        payload.description = "![x](../escaped.txt)"

        archive_path = self.root / "evil.zip"
        with zipfile.ZipFile(archive_path, "w") as handle:
            handle.writestr("problems/EVIL.json",
                            json.dumps(payload.to_dict(), ensure_ascii=False))
            handle.writestr("resources/../escaped.txt", "pwned")

        report = self.archive.import_zip(archive_path, strategy=RENAME)
        self.assertEqual(report.imported, 1)
        self.assertFalse((self.root / "data" / "escaped.txt").exists())
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_judge_config_survives_both_export_formats(self) -> None:
        """判题方式（文件模式 + 校验器源码）必须随题目一起导出、导入。

        校验器是**题目自身的一部分**，源码就存在题目 JSON 里，所以归档层
        一行都不用改 —— 但这条链路得真跑一遍才算数。
        """
        judged = make_problem("SPJ1")
        judged.judge = JudgeConfig(io_mode=IOMode.FILE, input_file="data.in",
                                   output_file="data.out", checker=ORDER_FREE_CHECKER,
                                   checker_language=Language.PYTHON)
        self.repo.put(judged, strategy="overwrite")

        for kind, target in (("zip", self.root / "judge.zip"),
                             ("folder", self.root / "judge_dir")):
            with self.subTest(kind=kind):
                if kind == "zip":
                    self.archive.export_zip(target, [judged])
                    fresh = ProblemRepository(self.root / f"fresh_{kind}" / "problems.json",
                                              self.root / f"fresh_{kind}" / "resources").load()
                    ProblemArchive(fresh).import_zip(target, strategy=RENAME)
                else:
                    self.archive.export_folder(target, [judged])
                    fresh = ProblemRepository(self.root / f"fresh_{kind}" / "problems.json",
                                              self.root / f"fresh_{kind}" / "resources").load()
                    ProblemArchive(fresh).import_folder(target, strategy=RENAME)

                restored = fresh.get("SPJ1")
                self.assertIsNotNone(restored)
                self.assertEqual(restored.judge.to_dict(), judged.judge.to_dict())
                self.assertEqual(restored.judge.input_file, "data.in")
                self.assertIn("token 集合", restored.judge.checker)

    def test_import_rejects_payload_without_testcases(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"id": "X", "title": "t", "description": "d",
                                   "time_limit": 100, "memory_limit": 64,
                                   "testcases": []}), encoding="utf-8")
        ok, message = self.archive.import_single(bad)
        self.assertFalse(ok)
        self.assertTrue(message)


class SecurityScanTest(unittest.TestCase):
    def test_detects_process_spawn(self) -> None:
        findings = scan("import subprocess\nsubprocess.run(['calc'])", Language.PYTHON)
        self.assertTrue(any("subprocess" in item for item in findings))

    def test_detects_system_call_in_cpp(self) -> None:
        findings = scan("#include <cstdlib>\nint main(){ system(\"pause\"); }", Language.CPP)
        self.assertTrue(any("system()" in item for item in findings))

    def test_cpp_rule_not_applied_to_python(self) -> None:
        self.assertEqual(scan("print(1)", Language.PYTHON), [])

    def test_clean_code_has_no_findings(self) -> None:
        code = "a, b = map(int, input().split())\nprint(a + b)\n"
        self.assertEqual(scan(code, Language.PYTHON), [])


class JavaNamingTest(unittest.TestCase):
    def test_public_class_is_detected(self) -> None:
        code = "public class Solution { public static void main(String[] a){} }"
        self.assertEqual(JavaRunner.extract_main_class(code), "Solution")

    def test_ignores_class_name_inside_comment(self) -> None:
        code = "// public class Nope\npublic class Real { }"
        self.assertEqual(JavaRunner.extract_main_class(code), "Real")

    def test_falls_back_to_main(self) -> None:
        self.assertEqual(JavaRunner.extract_main_class("int x = 1;"), "Main")


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_when_missing(self) -> None:
        settings = AppSettings(self.path).load()
        self.assertTrue(settings.bool("security_mode"))
        self.assertEqual(settings.get("theme"), "light")

    def test_save_then_load_keeps_values(self) -> None:
        settings = AppSettings(self.path).load()
        settings.set("theme", "dark")
        settings.set_compiler_path("cpp", "C:/mingw/bin/g++.exe")
        settings.save()

        reloaded = AppSettings(self.path).load()
        self.assertEqual(reloaded.get("theme"), "dark")
        self.assertEqual(reloaded.compiler_path("cpp"), "C:/mingw/bin/g++.exe")

    def test_missing_keys_get_defaults(self) -> None:
        self.path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        settings = AppSettings(self.path).load()
        self.assertEqual(settings.get("theme"), "dark")
        self.assertTrue("o2_optimization" in settings.as_dict())

    def test_broken_file_is_quarantined(self) -> None:
        self.path.write_text("{{{", encoding="utf-8")
        settings = AppSettings(self.path).load()
        self.assertEqual(settings.get("theme"), "light")
        self.assertTrue(self.path.with_suffix(".json.broken").is_file())


class SubmissionLogTest(unittest.TestCase):
    def test_append_and_read_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = SubmissionLog(Path(tmp) / "history.jsonl")
            for index in range(3):
                log.append(SubmissionRecord(
                    problem_id=f"P{index}", title="t", language="cpp",
                    verdict="AC", passed=2, total=2,
                ))
            recent = log.recent(2)
            self.assertEqual(len(recent), 2)
            self.assertEqual(recent[0]["problem_id"], "P2")


class JudgeIntegrationTest(unittest.TestCase):
    """真实跑一遍评测流程（Python 总是可用，所以只测 Python）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.problem = Problem(
            id="SUM",
            title="求和",
            description="读入两个整数输出和",
            time_limit=3000,
            memory_limit=256,
            testcases=[TestCase(input="1 2\n", output="3\n"),
                       TestCase(input="10 20\n", output="30\n")],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _judge(self, code: str, language: Language = Language.PYTHON):
        from offline_oj.core.runners import make_runner

        paths = {"python": sys.executable}
        runner = make_runner(language, paths, self.root, optimize=False)
        return Judge(runner).judge(self.problem, code)

    def test_correct_solution_is_accepted(self) -> None:
        report = self._judge("a, b = map(int, input().split())\nprint(a + b)\n")
        self.assertTrue(report.accepted, report.text_report())
        self.assertEqual(report.passed, 2)

    def test_wrong_answer_is_detected(self) -> None:
        # 第一组 (1,2) 输出 3 正确，第二组 (10,20) 输出 0 错误 → 通过 1 个
        report = self._judge(
            "a, b = map(int, input().split())\nprint(a + b if a == 1 else 0)\n")
        self.assertEqual(report.verdict, Verdict.WA)
        self.assertEqual(report.passed, 1)
        self.assertEqual(report.total, 2)

    def test_runtime_error_is_detected(self) -> None:
        report = self._judge("raise SystemExit(1)\n")
        self.assertEqual(report.verdict, Verdict.RE)

    def test_syntax_error_reports_compile_error(self) -> None:
        report = self._judge("def broken(:\n")
        self.assertFalse(report.compile_ok)
        self.assertEqual(report.verdict, Verdict.CE)

    def test_time_limit_is_enforced(self) -> None:
        report = self._judge("while True:\n    pass\n")
        self.assertEqual(report.verdict, Verdict.TLE)

    def test_output_limit_is_enforced(self) -> None:
        # 刻意让输出**有限但超限**（6 MB > MAX_OUTPUT_BYTES 的 4 MB）。
        #
        # 原来的写法是 `while True: print('x' * 1000)` —— 无限输出。它能过，
        # 但代价是判题器自己的读取线程被拖到内存耗尽（``communicate()`` 会先
        # **全量**读进本进程内存，超限截断发生在那之后，实测抛 MemoryError），
        # 于是整套测试里留下一两百 MB 的内存尖峰，后面的用例在内存压力下跑，
        # 全量偶发段错误就出在这之后（实测崩点：test_exam_code_view）。
        # 有限超限一样能验证 OLE 判定，而且不靠 TLE 竞态，反而更稳。
        report = self._judge("print('x' * (6 * 1024 * 1024))\n")
        self.assertIn(report.verdict, (Verdict.TLE, Verdict.OLE))

    def test_events_are_emitted_in_order(self) -> None:
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        kinds = [event.kind for event in
                 Judge(runner).iter_events(self.problem, "a, b = map(int, input().split())\nprint(a + b)\n")]
        self.assertEqual(kinds[0], "start")
        self.assertIn("compiled", kinds)
        self.assertEqual(kinds[-1], "finish")

    def test_work_dir_is_cleaned_up(self) -> None:
        self._judge("print(1)\n")
        leftovers = [item for item in self.root.iterdir() if item.is_dir()]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# 判题方式（文件输入输出 + 自定义校验器）
# ---------------------------------------------------------------------------

#: 顺序无关的校验器：两边 token 排序后相同就算对
ORDER_FREE_CHECKER = """\
import sys


def tokens(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read().split()


if sorted(tokens(sys.argv[2])) == sorted(tokens(sys.argv[3])):
    sys.exit(0)
print("token 集合与标准答案不一致")
sys.exit(1)
"""

#: 永远返回"格式错误"
PE_CHECKER = "import sys\nsys.exit(2)\n"

#: 协议之外的退出码：属于校验器自己坏了
CRASH_CHECKER = "import sys\nprint('checker blew up', file=sys.stderr)\nsys.exit(3)\n"

#: 死循环，用来确认校验器超时不会把评测拖死
HANG_CHECKER = "while True:\n    pass\n"

#: 编译不过的校验器
BROKEN_CHECKER = "def broken(:\n"


class JudgeConfigTest(unittest.TestCase):
    """判题配置的序列化与校验。

    最要紧的一条是**向后兼容**：没配过判题方式的题目，落盘结果必须与老版本
    逐字节一致，否则老题库会被静默改写。
    """

    def _problem(self, judge: JudgeConfig, *, problem_id: str = "P",
                 slug: str = "") -> Problem:
        return Problem(id=problem_id, title="t", slug=slug, judge=judge,
                       testcases=[TestCase(input="1\n", output="1\n")])

    def test_default_problem_json_has_no_judge_key(self) -> None:
        data = self._problem(JudgeConfig()).to_dict()
        self.assertNotIn("judge", data)
        self.assertEqual(JudgeConfig().to_dict(), {})

    def test_default_config_matches_the_old_field_set(self) -> None:
        """字段集合就是加判题方式之前的那一套，一个不多。"""
        data = self._problem(JudgeConfig()).to_dict()
        self.assertEqual(
            set(data),
            {"id", "title", "description", "time_limit", "memory_limit",
             "testcases", "source", "created_at", "updated_at"},
        )

    def test_round_trip_keeps_every_field(self) -> None:
        judge = JudgeConfig(io_mode=IOMode.FILE, input_file="a.in", output_file="a.out",
                            checker_language=Language.CPP, checker="int main(){}")
        restored = Problem.from_dict(self._problem(judge).to_dict()).judge
        self.assertEqual(restored.to_dict(), judge.to_dict())
        self.assertTrue(restored.uses_files)
        self.assertTrue(restored.uses_checker)

    def test_from_dict_tolerates_junk(self) -> None:
        for value in (None, {}, [], "nonsense", 42):
            with self.subTest(value=value):
                config = JudgeConfig.from_dict(value)
                self.assertIs(config.io_mode, IOMode.STDIO)
                self.assertFalse(config.uses_checker)

    def test_unknown_io_mode_falls_back_to_stdio(self) -> None:
        self.assertIs(JudgeConfig.from_dict({"io_mode": "telepathy"}).io_mode, IOMode.STDIO)

    def test_languages_that_cannot_be_checkers_are_rejected(self) -> None:
        # C 与 Java 不在 CHECKER_LANGUAGES 里：读回来时应当当作"没有校验器"，
        # 而不是留一个跑不起来的配置
        self.assertEqual(CHECKER_LANGUAGES, (Language.CPP, Language.PYTHON))
        for language in (Language.C, Language.JAVA):
            with self.subTest(language=language):
                config = JudgeConfig.from_dict(
                    {"checker_language": language.value, "checker": "x"})
                self.assertIsNone(config.checker_language)
                self.assertFalse(config.uses_checker)

    def test_blank_filenames_fall_back_to_defaults(self) -> None:
        config = JudgeConfig.from_dict({"io_mode": "file", "input_file": "   ",
                                        "output_file": ""})
        self.assertEqual(config.input_file, DEFAULT_INPUT_FILE)
        self.assertEqual(config.output_file, DEFAULT_OUTPUT_FILE)

    def test_validation_catches_bad_filenames(self) -> None:
        cases = [
            (JudgeConfig(io_mode=IOMode.FILE, input_file="", output_file="out.txt"),
             "输入文件名不能为空"),
            (JudgeConfig(io_mode=IOMode.FILE, input_file="in.txt", output_file=""),
             "输出文件名不能为空"),
            (JudgeConfig(io_mode=IOMode.FILE, input_file="../in.txt", output_file="o.txt"),
             "不能包含路径分隔符"),
            (JudgeConfig(io_mode=IOMode.FILE, input_file="a.txt", output_file="a.txt"),
             "不能相同"),
        ]
        for config, expected in cases:
            with self.subTest(expected=expected):
                joined = "；".join(config.problems())
                self.assertIn(expected, joined)

    def test_validation_catches_half_configured_checker(self) -> None:
        no_language = JudgeConfig(checker="print(1)", checker_language=None)
        self.assertIn("没有选择校验器语言", "；".join(no_language.problems()))

        no_source = JudgeConfig(checker="   ", checker_language=Language.PYTHON)
        self.assertIn("校验器源码是空的", "；".join(no_source.problems()))
        self.assertFalse(no_source.uses_checker)

    def test_problem_validate_includes_judge_problems(self) -> None:
        problem = self._problem(
            JudgeConfig(io_mode=IOMode.FILE, input_file="a.txt", output_file="a.txt"))
        self.assertTrue(any("不能相同" in item for item in problem.validate()))

    def test_judge_note_only_mentions_what_is_configured(self) -> None:
        self.assertEqual(self._problem(JudgeConfig()).judge_note(), "")
        files = self._problem(JudgeConfig(io_mode=IOMode.FILE), slug="poker")
        self.assertEqual(files.judge_note(), " · 文件 poker.in/poker.out")
        both = self._problem(JudgeConfig(io_mode=IOMode.FILE,
                                         checker_language=Language.CPP, checker="x"),
                             slug="poker")
        self.assertEqual(both.judge_note(), " · 文件 poker.in/poker.out · 特殊判题")

    def test_judge_note_shows_the_names_literally_written_by_the_user(self) -> None:
        """手写文件名不受题目英文名影响，提示里就该显示手写的那个。"""
        problem = self._problem(
            JudgeConfig(io_mode=IOMode.FILE, input_file="a.in", output_file="a.out"),
            slug="poker")
        self.assertEqual(problem.judge_note(), " · 文件 a.in/a.out")

    # ---- 文件名模板（CCF 规约：数据文件与题目英文名同名） ----

    def test_default_filenames_are_templates_not_literal_names(self) -> None:
        self.assertEqual(DEFAULT_INPUT_FILE, f"{TASK_NAME_PLACEHOLDER}.in")
        self.assertEqual(DEFAULT_OUTPUT_FILE, f"{TASK_NAME_PLACEHOLDER}.out")
        config = JudgeConfig(io_mode=IOMode.FILE)
        self.assertEqual((config.input_file, config.output_file),
                         (DEFAULT_INPUT_FILE, DEFAULT_OUTPUT_FILE))

    def test_resolved_expands_the_template_with_the_problem_name(self) -> None:
        config = JudgeConfig(io_mode=IOMode.FILE)
        resolved = config.resolved("poker")
        self.assertEqual((resolved.input_file, resolved.output_file),
                         ("poker.in", "poker.out"))
        # 原对象不能被就地改掉：同一份 JudgeConfig 会被复用给所有测试点
        self.assertEqual(config.input_file, DEFAULT_INPUT_FILE)

    def test_resolved_is_a_noop_for_literal_names_and_stdio_mode(self) -> None:
        literal = JudgeConfig(io_mode=IOMode.FILE, input_file="a.in", output_file="a.out")
        self.assertIs(literal.resolved("poker"), literal)
        stdio = JudgeConfig()
        self.assertIs(stdio.resolved("poker"), stdio)

    def test_resolved_keeps_the_checker_intact(self) -> None:
        config = JudgeConfig(io_mode=IOMode.FILE, checker="print(1)",
                             checker_language=Language.PYTHON)
        resolved = config.resolved("poker")
        self.assertEqual(resolved.checker, "print(1)")
        self.assertTrue(resolved.uses_checker)

    def test_the_default_template_is_not_reported_as_a_bad_filename(self) -> None:
        # 模板里带花括号，但既没有路径分隔符也不是同一个名字，不该被拦下来
        self.assertEqual(JudgeConfig(io_mode=IOMode.FILE).problems(), [])


class CcfNamingTest(unittest.TestCase):
    """CCF CSP-J/S 第二轮认证的命名与目录规约。

    规约（NOI 官网）：源程序 ``<题目英文名>.cpp``，输入输出文件 ``<题目英文名>.in`` /
    ``<题目英文名>.out``，文件夹名与题目英文名完全一致且只能是小写英文字母，
    程序在**当前路径下**用不带绝对路径的名字访问数据文件。
    """

    def test_valid_names_pass_through_untouched(self) -> None:
        for name in ("poker", "task1", "a_b_c", "p1001", "1", "a"):
            with self.subTest(name=name):
                self.assertEqual(sanitize_slug(name), name)

    def test_invalid_characters_are_collapsed_to_underscores(self) -> None:
        cases = {
            "Poker": "poker",
            "  Poker  ": "poker",
            "Poker Game": "poker_game",
            "a-b.c": "a_b_c",
            "a///b": "a_b",
            "__x__": "x",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_slug(raw), expected)

    def test_names_that_wash_away_completely_fall_back(self) -> None:
        for raw in ("", "   ", "第一题", "***"):
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_slug(raw), FALLBACK_SLUG)

    def test_sanitize_is_idempotent(self) -> None:
        for raw in ("Poker Game", "第一题", "a-b.c", "poker"):
            with self.subTest(raw=raw):
                once = sanitize_slug(raw)
                self.assertEqual(sanitize_slug(once), once)

    def test_english_name_prefers_the_explicit_slug(self) -> None:
        self.assertEqual(Problem(id="P0001", slug="poker").english_name, "poker")
        self.assertFalse(Problem(id="P0001", slug="poker").slug_is_derived)

    def test_english_name_is_derived_from_the_id_when_blank(self) -> None:
        problem = Problem(id="P0001")
        self.assertEqual(problem.english_name, "p0001")
        self.assertTrue(problem.slug_is_derived)

    def test_english_name_of_a_chinese_id_is_the_fallback(self) -> None:
        problem = Problem(id="第一题")
        self.assertEqual(problem.english_name, FALLBACK_SLUG)
        # 兜底名也是合法目录名，判题照样能跑
        self.assertTrue(problem.english_name.isascii())

    def test_illegal_slug_is_rejected_on_save(self) -> None:
        for bad in ("Poker", "poker game", "poker.cpp", "扑克"):
            with self.subTest(slug=bad):
                issues = Problem(id="P", slug=bad).validate()
                self.assertTrue(any("英文名" in item for item in issues), issues)

    def test_legal_slug_passes_validation(self) -> None:
        for good in ("poker", "poker_1", "p1_2_3", ""):
            with self.subTest(slug=good):
                issues = Problem(id="P", slug=good).validate()
                self.assertFalse(any("英文名" in item for item in issues), issues)

    def test_io_files_expand_to_the_problem_name(self) -> None:
        problem = Problem(id="P", slug="poker",
                          judge=JudgeConfig(io_mode=IOMode.FILE))
        self.assertEqual(problem.io_files(), ("poker.in", "poker.out"))

    def test_io_files_ignore_literal_names(self) -> None:
        problem = Problem(id="P", slug="poker",
                          judge=JudgeConfig(io_mode=IOMode.FILE, input_file="a.in",
                                            output_file="a.out"))
        self.assertEqual(problem.io_files(), ("a.in", "a.out"))

    def test_compiled_artifact_has_no_extension_on_posix(self) -> None:
        expected = "poker.exe" if sys.platform == "win32" else "poker"
        self.assertEqual(executable_name("poker"), expected)

    def test_blank_slug_is_not_serialized(self) -> None:
        data = Problem(id="P").to_dict()
        self.assertNotIn("slug", data)

    def test_slug_survives_a_dict_round_trip(self) -> None:
        problem = Problem(id="P0001", slug="poker")
        self.assertEqual(problem.to_dict()["slug"], "poker")
        self.assertEqual(Problem.from_dict(problem.to_dict()).slug, "poker")

    def test_junk_slug_in_json_does_not_crash(self) -> None:
        # 数字会被转成字符串（"42" 本身是合法英文名），其余一律清成空
        cases = [(None, ""), (42, "42"), ([], ""), ("  ", "")]
        for value, expected in cases:
            with self.subTest(value=value):
                data = Problem(id="P").to_dict()
                data["slug"] = value
                self.assertEqual(Problem.from_dict(data).slug, expected)


class FileModeTest(unittest.TestCase):
    """文件输入输出模式：程序读写题目指定的文件。

    这里刻意用**题目英文名**（``poker``）当数据文件名，走的就是 CCF 那一套：
    题目 poker 对应 ``poker.in`` / ``poker.out``，源程序与产物也都叫 ``poker``。
    """

    SLUG = "poker"

    #: 读 poker.in 的两个数、把和写进 poker.out
    SOLUTION = (
        "a, b = map(int, open('poker.in').read().split())\n"
        "open('poker.out', 'w').write(str(a + b) + '\\n')\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.judge_config = JudgeConfig(io_mode=IOMode.FILE)
        self.problem = Problem(
            id="FILE", title="文件输入输出", slug=self.SLUG,
            time_limit=3000, memory_limit=256,
            judge=self.judge_config,
            testcases=[TestCase(input="1 2\n", output="3\n"),
                       TestCase(input="10 20\n", output="30\n")],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _judge(self, code: str):
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        return Judge(runner).judge(self.problem, code)

    def test_reading_and_writing_files_is_accepted(self) -> None:
        report = self._judge(self.SOLUTION)
        self.assertTrue(report.accepted, report.text_report())
        self.assertEqual(report.passed, 2)

    def test_data_files_are_named_after_the_problem(self) -> None:
        """确认判题时数据文件真的叫 poker.in / poker.out，而不是模板原样。"""
        report = self._judge(self.SOLUTION)
        self.assertTrue(report.accepted, report.text_report())
        self.assertEqual(self.problem.io_files(), ("poker.in", "poker.out"))

    def test_program_runs_inside_a_directory_named_after_the_problem(self) -> None:
        """CCF 的目录结构：程序在 <题目英文名> 这一层里，以裸名访问数据文件。

        让程序把当前目录列出来写进输出文件 —— 目录里应当有源程序与输入文件，
        且没有路径前缀，证明 cwd 就是那一层题目目录。
        """
        program = (
            "import os\n"
            "names = sorted(n for n in os.listdir('.') if os.path.isfile(n))\n"
            "open('poker.out', 'w').write('\\n'.join(names) + '\\n')\n"
        )
        problem = Problem(id="FILE", title="目录", slug=self.SLUG,
                          judge=self.judge_config,
                          testcases=[TestCase(input="1 2\n",
                                              output="poker.in\npoker.py\n")])
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        report = Judge(runner).judge(problem, program)
        self.assertTrue(report.accepted, report.text_report())

    def test_writing_only_to_stdout_is_wrong_answer(self) -> None:
        # 输出内容本身是对的，只是写错了地方；必须给 WA 并把原因说清楚
        report = self._judge("a, b = map(int, open('poker.in').read().split())\n"
                             "print(a + b)\n")
        self.assertEqual(report.verdict, Verdict.WA)
        self.assertIn("没有生成输出文件 poker.out", report.outcomes[0].message)
        self.assertIn("标准输出", report.outcomes[0].message)

    def test_missing_output_file_is_never_accepted(self) -> None:
        # 期望输出非空时本就该 WA，这里额外确认没有把空输出当成"相等"
        report = self._judge("pass\n")
        self.assertEqual(report.verdict, Verdict.WA)
        self.assertEqual(report.passed, 0)

    def test_empty_expected_output_is_not_matched_by_a_missing_file(self) -> None:
        """最容易误判的一种：期望输出为空 + 程序什么都没写。

        两边都是空字符串，精确比对会判 AC —— 而"没写输出文件"其实是错的。
        """
        problem = Problem(id="EMPTY", title="空答案", judge=self.judge_config,
                          testcases=[TestCase(input="\n", output="")])
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        report = Judge(runner).judge(problem, "pass\n")
        self.assertEqual(report.verdict, Verdict.WA)

    def test_stale_output_file_from_the_previous_case_is_not_reused(self) -> None:
        """同一工作目录连着跑所有测试点，上一轮的 poker.out 必须先清掉。

        程序只在第一组数据下写文件；第二组如果读到上一轮的残留，就会被判成 AC。
        """
        program = (
            "a, b = map(int, open('poker.in').read().split())\n"
            "if a == 1:\n"
            "    open('poker.out', 'w').write(str(a + b) + '\\n')\n"
        )
        report = self._judge(program)
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 1, report.text_report())
        self.assertEqual(report.outcomes[1].verdict, Verdict.WA)

    def test_stdio_mode_is_unaffected(self) -> None:
        problem = Problem(id="S", title="标准流", testcases=[TestCase(input="1 2\n",
                                                                     output="3\n")])
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        report = Judge(runner).judge(
            problem, "a, b = map(int, input().split())\nprint(a + b)\n")
        self.assertTrue(report.accepted, report.text_report())


class SpecialJudgeTest(unittest.TestCase):
    """自定义校验器：判定协议的五条出口，以及"校验器坏了"不算选手头上。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _problem(self, judge: JudgeConfig) -> Problem:
        return Problem(id="SPJ", title="多解题目", time_limit=3000, memory_limit=256,
                       judge=judge, testcases=[TestCase(input="3 5\n", output="3 5\n")])

    def _judge(self, judge: JudgeConfig, code: str):
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        return Judge(runner).judge(self._problem(judge), code)

    def _events(self, judge: JudgeConfig, code: str):
        from offline_oj.core.runners import make_runner

        runner = make_runner(Language.PYTHON, {"python": sys.executable},
                             self.root, optimize=False)
        return list(Judge(runner).iter_events(self._problem(judge), code))

    @staticmethod
    def _checker(source: str) -> JudgeConfig:
        return JudgeConfig(checker=source, checker_language=Language.PYTHON)

    # ---- 没有校验器：与从前一致 ----

    def test_without_a_checker_the_old_comparison_still_applies(self) -> None:
        self.assertEqual(self._judge(JudgeConfig(), "print('3 5')\n").verdict, Verdict.AC)
        self.assertEqual(self._judge(JudgeConfig(), "print('5 3')\n").verdict, Verdict.WA)

    # ---- 协议出口 ----

    def test_valid_alternative_answer_is_accepted(self) -> None:
        report = self._judge(self._checker(ORDER_FREE_CHECKER), "print('5 3')\n")
        self.assertTrue(report.accepted, report.text_report())

    def test_checker_rejection_is_wrong_answer(self) -> None:
        report = self._judge(self._checker(ORDER_FREE_CHECKER), "print('5 4')\n")
        self.assertEqual(report.verdict, Verdict.WA)

    def test_the_checker_own_explanation_reaches_the_user(self) -> None:
        report = self._judge(self._checker(ORDER_FREE_CHECKER), "print('5 4')\n")
        self.assertIn("token 集合", report.outcomes[0].message)

    def test_wrong_answer_from_a_checker_has_no_line_diff(self) -> None:
        # 答案可能本来就是合法的另一种形式，逐行 diff 只会误导
        report = self._judge(self._checker(ORDER_FREE_CHECKER), "print('5 4')\n")
        self.assertEqual(report.outcomes[0].diff, "")

    def test_exit_code_two_is_presentation_error(self) -> None:
        report = self._judge(self._checker(PE_CHECKER), "print('3 5')\n")
        self.assertEqual(report.verdict, Verdict.PE)

    # ---- 校验器自身出问题 ----

    def test_unknown_exit_code_is_internal_error(self) -> None:
        report = self._judge(self._checker(CRASH_CHECKER), "print('3 5')\n")
        self.assertEqual(report.verdict, Verdict.IE)
        self.assertIn("退出码 3", report.outcomes[0].message)
        self.assertIn("checker blew up", report.outcomes[0].message)   # stderr 带回来了
        self.assertTrue(report.compile_ok, "选手代码并没有编译失败")

    def test_checker_timeout_is_internal_error(self) -> None:
        from offline_oj.core import checker as checker_module
        from unittest import mock

        # 把上限压到 1 秒，否则这条用例要等 30 秒
        with mock.patch.object(checker_module, "CHECKER_TIMEOUT_MS", 1000):
            report = self._judge(self._checker(HANG_CHECKER), "print('3 5')\n")
        self.assertEqual(report.verdict, Verdict.IE)
        self.assertIn("超时", report.outcomes[0].message)

    def test_broken_checker_aborts_before_running_any_case(self) -> None:
        judge = self._checker(BROKEN_CHECKER)
        events = self._events(judge, "print('3 5')\n")
        kinds = [event.kind for event in events]

        self.assertIn(KIND_CHECKER, kinds)
        report = events[-1].report
        self.assertEqual(report.verdict, Verdict.IE)
        self.assertFalse(report.checker_ok)
        self.assertTrue(report.compile_ok, "不要把校验器的问题算成选手的编译错误")
        self.assertEqual(report.total, 0, "校验器编译不过时不该白跑选手程序")
        self.assertIn("自定义校验器不可用", report.summary())

    def test_checker_toolchain_failure_is_reported_before_running(self) -> None:
        """题目配了 C++ 校验器，但这台机器没配 C++ 编译器。

        校验器和选手程序共用工具链路径表；选手跑 Python 不受影响，
        所以整题应当因为**校验器**判不了而记 IE。
        """
        judge = JudgeConfig(checker="int main(){}", checker_language=Language.CPP)
        report = self._judge(judge, "print('3 5')\n")
        self.assertEqual(report.verdict, Verdict.IE)
        self.assertFalse(report.checker_ok)
        self.assertTrue(report.compile_ok, "选手代码是好的，不该被算成 CE")
        self.assertIn("校验器", report.checker_message)

    def test_checker_workspace_is_cleaned_up(self) -> None:
        self._judge(self._checker(ORDER_FREE_CHECKER), "print('5 3')\n")
        leftovers = [item for item in self.root.iterdir() if item.is_dir()]
        self.assertEqual(leftovers, [], "校验器的工作目录也要清干净")

    def test_checker_and_file_mode_can_be_combined(self) -> None:
        judge = JudgeConfig(io_mode=IOMode.FILE, input_file="in.txt", output_file="ans.txt",
                            checker=ORDER_FREE_CHECKER,
                            checker_language=Language.PYTHON)
        program = ("v = open('in.txt').read().split()\n"
                   "open('ans.txt', 'w').write(v[1] + ' ' + v[0] + '\\n')\n")
        report = self._judge(judge, program)
        self.assertTrue(report.accepted, report.text_report())

        # 没写输出文件时，运行阶段就该给出结论，不劳校验器
        report = self._judge(judge, "print('5 3')\n")
        self.assertEqual(report.verdict, Verdict.WA)
        self.assertIn("没有生成输出文件", report.outcomes[0].message)


class ToolchainScanTest(unittest.TestCase):
    """工具链扫描与编译标准降级。

    这两块踩过的坑都很隐蔽 —— 前者让"装了编译器却检测不到"，后者让
    "Python/Java 能判而 C++ 一律编译失败"，都必须在无图形环境下可回归。
    """

    def test_ladder_runs_from_new_to_old(self) -> None:
        self.assertEqual(CPP_STANDARD_LADDER[0], "-std=c++17")
        self.assertIn("-std=c++14", CPP_STANDARD_LADDER)   # GCC 4.9 的上限
        self.assertEqual(CPP_STANDARD_LADDER[-1], "")
        self.assertEqual(C_STANDARD_LADDER[0], "-std=c11")

    def test_runners_use_a_ladder_not_a_single_flag(self) -> None:
        for language, expected in ((Language.CPP, CPP_STANDARD_LADDER),
                                   (Language.C, C_STANDARD_LADDER)):
            runner = make_runner(language, {}, "/tmp")
            self.assertFalse(hasattr(runner, "std_flag"),
                             "不应该再存在写死的 std_flag")
            # 阶梯交给 profile 提供：同一个 runner 既可能配 GCC 也可能配 cl.exe
            self.assertEqual(
                profile_for("C:/mingw/bin/g++.exe").ladder(language, "C:/mingw/bin/g++.exe"),
                expected,
            )

    def test_msvc_ladder_comes_from_toolset_version(self) -> None:
        """MSVC 认不出 /std: 时只发 D9002 不报错，所以必须按版本号选而不是试错。"""
        from offline_oj.win32 import msvc

        profile = MsvcProfile()
        # 19.44（VS2022）支持 c++17；19.36 是 VS2019，c11 可用
        self.assertEqual(msvc.cpp_standard((19, 44, 35228)), "/std:c++17")
        self.assertEqual(msvc.cpp_standard((19, 20, 0)), "/std:c++17")
        self.assertEqual(msvc.cpp_standard((19, 0, 24215)), "/std:c++14")
        self.assertEqual(msvc.cpp_standard((18, 0, 0)), "")          # VS2013 没有 /std:
        self.assertEqual(msvc.c_standard((19, 44, 35228)), "/std:c17")
        self.assertEqual(msvc.c_standard((19, 28, 0)), "/std:c11")
        self.assertEqual(msvc.c_standard((19, 20, 0)), "")          # VS2019 16.7 之前没有

        # 落成阶梯时永远留一个"不加参数"的兜底，供 D9002 时降级
        ladder = profile.ladder(Language.CPP, __file__)
        self.assertEqual(ladder[-1], "")

    def test_msvc_version_normalizes_toolset_directory(self) -> None:
        """工具集目录写的是 14.44，编译器自称 19.44 —— 不换算会得出反的结论。"""
        from offline_oj.win32 import msvc

        self.assertEqual(msvc._normalize_version((14, 44, 35207)), (19, 44))
        self.assertEqual(msvc._normalize_version((19, 44, 35228)), (19, 44))
        self.assertEqual(msvc._normalize_version(()), ())

    def test_msvc_is_detected_by_filename_only(self) -> None:
        from offline_oj.win32 import msvc

        # 只看文件名：cl.exe 可能来自 VS / Build Tools / 被复制到别处
        self.assertTrue(msvc.is_msvc(r"C:\Program Files\VS\VC\Tools\MSVC\14.44\bin\Hostx64\x64\cl.exe"))
        self.assertTrue(msvc.is_msvc("cl.exe"))
        self.assertTrue(msvc.is_msvc("CL.EXE"))
        self.assertFalse(msvc.is_msvc("C:/mingw/bin/g++.exe"))
        self.assertFalse(msvc.is_msvc("/usr/bin/clang++"))
        self.assertFalse(msvc.is_msvc(""))
        self.assertFalse(msvc.is_msvc(None))

    def test_msvc_architecture_read_from_path_not_banner(self) -> None:
        """横幅里那句 ``for x64`` 会随系统语言变成「用于 x64 的」，不能靠它。"""
        from offline_oj.win32 import msvc

        self.assertEqual(
            msvc.architecture(r"C:\VS\VC\Tools\MSVC\14.44\bin\Hostx64\x64\cl.exe"), "x64")
        self.assertEqual(
            msvc.architecture(r"C:\VS\VC\Tools\MSVC\14.44\bin\Hostx86\x86\cl.exe"), "x86")
        self.assertEqual(msvc.architecture("cl.exe"), "")

    def test_msvc_profile_ignores_unknown_std_flag(self) -> None:
        """D9002 是"参数被忽略"，不是"编译失败" —— 必须能识别出来。"""
        from subprocess import CompletedProcess

        from offline_oj.win32 import msvc

        profile = MsvcProfile()
        completed = CompletedProcess(
            [], 0, "feat.cpp\r\n",
            "cl: 命令行 warning D9002 :忽略未知选项“/std:c++23”\r\n")
        self.assertTrue(profile.ignored(completed, "/std:c++23"))
        # 忽略的是别人（比如老版本不认 /utf-8），不算我这个标准参数的问题
        self.assertFalse(profile.ignored(completed, "/std:c++17"))
        # 空参数不可能被忽略
        self.assertFalse(profile.ignored(completed, ""))
        # GCC 系没有"忽略"这种半途状态
        self.assertFalse(profile_for("/mingw/g++.exe").ignored(completed, "-std=c++17"))
        self.assertEqual(msvc.ignored_options(completed), ("/std:c++23",))

    def test_msvc_command_line_uses_relative_names(self) -> None:
        """参数里不能出现工作目录 —— 用户名带空格时 /Fe: 会被 cl 的解析绊住。"""
        profile = MsvcProfile()
        command = profile.command(
            compiler=r"C:\VS\cl.exe", work_dir=r"C:\Users\John Doe\ws\oj_1",
            source_name="main.cpp", target_name="main.exe",
            flag="/std:c++17", optimize=True, language=Language.CPP)
        self.assertEqual(command[0], r"C:\VS\cl.exe")
        self.assertIn("/nologo", command)
        self.assertIn("/std:c++17", command)
        self.assertIn("/utf-8", command)     # 否则源码会被按 ANSI 代码页读
        self.assertIn("/MT", command)        # 静态 CRT，产物不依赖可再发行组件包
        self.assertIn("/EHsc", command)
        for item in command:
            self.assertNotIn("John Doe", item, "命令行参数里不应出现工作目录")
        # 源文件与产物都用相对名，链接器与调试信息也就不会带上空格路径
        self.assertIn("main.cpp", command)
        self.assertIn("/Fe:main.exe", command)
        self.assertIn("/Fo:main.obj", command)

    def test_msvc_c_mode_skips_cxx_only_options(self) -> None:
        profile = MsvcProfile()
        command = profile.command(
            compiler="cl.exe", work_dir="C:/ws", source_name="main.c",
            target_name="main.exe", flag="/std:c17", optimize=False,
            language=Language.C)
        self.assertNotIn("/EHsc", command, "/EHsc 只对 C++ 有意义")
        self.assertNotIn("/O2", command, "optimize=False 时不应该加优化")
        self.assertIn("/std:c17", command)

    def test_msvc_without_environment_reports_actionable_error(self) -> None:
        """找不到 INCLUDE / LIB 时要说清该装什么，而不是丢一句"编译失败"。"""
        from unittest import mock

        with tempfile.TemporaryDirectory() as folder:
            # 造一个真的存在、名字就叫 cl.exe 的文件，好让前面的可用性检查通过，
            # 这样测到的才是"环境组装失败"这条分支
            fake = Path(folder) / "cl.exe"
            fake.write_bytes(b"MZ")
            runner = make_runner(Language.CPP, {"cpp": str(fake)}, folder)
            with mock.patch.object(MsvcProfile, "environment", return_value=None):
                result = runner.compile("int main(){return 0;}", folder)
        self.assertFalse(result.ok)
        self.assertIn("INCLUDE", result.message)
        self.assertIn("使用 C++ 的桌面开发", result.message)

    def test_compile_error_message_drops_work_dir_prefix(self) -> None:
        """报错里的临时目录路径要摘掉，否则用户看到一长串 %TEMP% 路径。"""
        from subprocess import CompletedProcess

        work = r"C:\Users\ws\AppData\Local\Temp\oj_cpp_ab12"
        completed = CompletedProcess(
            [], 1, "", f"{work}\\main.cpp(3): error: 'x' was not declared\n")
        text = _diagnose(completed, work, "main.cpp")
        self.assertIn("main.cpp(3): error", text)
        self.assertNotIn("AppData", text)
        # cl.exe 会在 stdout 回显源文件名，那是噪音
        echoed = CompletedProcess([], 0, "main.cpp\r\n", "")
        self.assertEqual(_diagnose(echoed, work, "main.cpp"), "")

    def test_launch_denied_is_explained_as_security_software(self) -> None:
        """产物起不来时要说清"多半是杀毒软件"，并给出唯一有效的处置办法。

        踩过的坑：判题机装了 360 / 火绒时，刚编译出的程序会被拦下执行、随后连文件
        一起删掉。此时报错只剩一句「拒绝访问」，用户完全无从下手 ——
        而这跟代码没有任何关系。
        """
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "main.exe")           # 模拟"已被删掉"
            denied = PermissionError(13, "拒绝访问。", None, 5, None)
            text = _launch_failure(denied, missing, folder)

        self.assertIn("拒绝访问", text)
        self.assertIn("杀毒软件", text)
        self.assertIn(folder, text, "要给出可照做的目录路径")
        self.assertIn("信任区", text)

    def test_launch_failure_keeps_it_short_when_not_blocked(self) -> None:
        """不是"被拦下"的情况不要吓唬用户，直接给原因和路径就好。"""
        with tempfile.TemporaryDirectory() as folder:
            artifact = str(Path(folder) / "main.exe")
            Path(artifact).write_bytes(b"MZ")                  # 产物确实在
            exc = PermissionError(13, "参数错误。", None, 87, None)
            text = _launch_failure(exc, artifact, folder)

        self.assertIn("无法启动编译产物", text)
        self.assertIn(artifact, text)
        self.assertNotIn("杀毒软件", text)

    def test_flag_related_failure_is_recognized(self) -> None:
        text = "g++.exe: error: unrecognized command line option '-std=c++17'"
        self.assertTrue(_standard_rejected(text, "-std=c++17"))
        # clang 的说法不同，但同样带上了参数本身
        self.assertTrue(_standard_rejected(
            "error: invalid value 'c++17' in '-std=c++17'", "-std=c++17"))
        # 只有通用措辞、没提到参数，也算"可能只是参数问题"
        self.assertTrue(_standard_rejected(
            "cl: Command line warning D9002 : ignoring unknown option '-std=c++17'", ""))

    def test_real_compile_error_is_not_treated_as_flag_problem(self) -> None:
        """语法错误必须立刻失败，不能白试三种标准。"""
        text = ("main.cpp: In function 'int main()':\n"
                "main.cpp:3:15: error: 'this' was not declared in this scope")
        self.assertFalse(_standard_rejected(text, "-std=c++17"))
        self.assertFalse(_standard_rejected("", "-std=c++17"))

    def test_cached_flag_is_tried_first(self) -> None:
        runner = make_runner(Language.CPP, {"cpp": "/fake/g++"}, "/tmp")
        profile = profile_for("/fake/g++")
        key = ("cpp", _cache_key("/fake/g++"))
        _STANDARD_CACHE[key] = "-std=c++14"
        try:
            attempts = runner._standard_attempts("/fake/g++", profile)
            self.assertEqual(attempts[0], "-std=c++14")
            # 其余候选仍然保留作为兜底
            self.assertEqual(set(attempts), set(CPP_STANDARD_LADDER))
            self.assertEqual(len(attempts), len(CPP_STANDARD_LADDER))
        finally:
            _STANDARD_CACHE.pop(key, None)

    def test_scan_templates_expand_over_every_fixed_drive(self) -> None:
        drives = fixed_drives()
        if sys.platform != "win32" or not drives:
            self.skipTest("仅在 Windows 上验证")
        self.assertTrue(all(drive.endswith(":\\") for drive in drives))
        for key in ("cpp", "c"):
            dirs = extra_dirs(key)
            # 每个盘符都要有一份 Dev-Cpp 候选，否则"装在 D 盘"就等于没装
            for drive in drives:
                self.assertIn(f"{drive}Dev-Cpp\\MinGW64\\bin", dirs)
            self.assertIn(r"%LOCALAPPDATA%\Programs\mingw64\bin", dirs)

    def test_templates_have_no_hardcoded_drive(self) -> None:
        """扫描模板里不许写死盘符 —— 那正是"D 盘上的工具链探测不到"的根因。

        断言的是**模板**而不是展开结果：展开后出现 ``C:\\...`` 是正常的，
        因为 C 盘本来就在固定盘列表里。
        """
        pattern = re.compile(r"^[A-Za-z]:[\\/]")
        for table_name, table in (("_EXTRA_DIR_TEMPLATES", _EXTRA_DIR_TEMPLATES),
                                  ("_GLOB_TEMPLATES", _GLOB_TEMPLATES)):
            for key, templates in table.items():
                for template in templates:
                    self.assertIsNone(
                        pattern.match(template),
                        f"{table_name}[{key}] 里写死了盘符: {template}")

    def test_env_var_templates_survive_expansion_check(self) -> None:
        """展开后仍带 % 的模板要在 glob 前被跳过，不能拿着 %VAR% 去 glob。"""
        import os

        survived = [p for p in glob_dirs("python")
                    if "%" in os.path.expandvars(p)]
        self.assertEqual(survived, [])

    def test_falls_back_to_msvc_when_no_gcc_is_installed(self) -> None:
        """没装 GCC 系编译器时必须能退到 cl.exe。

        MSVC 支持的实际价值就在这条路径上 —— 只装了 Visual Studio 的机器
        （机房、只装 VS 的笔记本）也应该能判 C/C++。这里只验证**探测**，
        不做真实编译：编译要在 :func:`offline_oj.core.runners.self_test` 里单独跑。
        """
        from offline_oj.win32 import msvc

        cl = msvc.find_compiler()
        if not cl:
            self.skipTest("本机没有安装带 C++ 工具的 Visual Studio")

        from unittest import mock

        original = CompilerDetector._candidate_paths.__func__

        def without_gcc(cls, key):
            # 只保留 cl.exe，模拟"这台机器只有 VS、没有 MinGW"
            return [p for p in original(cls, key) if msvc.is_msvc(p)]

        with mock.patch.object(CompilerDetector, "_candidate_paths",
                               classmethod(without_gcc)):
            info = CompilerDetector.detect("cpp")

        self.assertIsNotNone(info, "排除 GCC 后应该退到 cl.exe，而不是「检测不到」")
        self.assertTrue(msvc.is_msvc(info.path))
        self.assertIn("Visual Studio", info.version)
        # 环境组装不出来时 probe() 会返回 None，能走到这里说明 INCLUDE/LIB 是齐的
        self.assertIsNotNone(msvc.build_environment(info.path))


class OptionalDependencyTest(unittest.TestCase):
    """可选依赖的导入时机。

    psutil 只用来做内存限制，却曾经在模块顶层被 ``import``。后果不只是启动慢：
    实测在导入 psutil 之后再让 Qt 绘制自绘委托，会抛 access violation，
    而且崩溃点（代码补全候选列表的 paint）跟 psutil 毫无逻辑关联 —— 极难排查。

    改成惰性导入之后又踩到第二个坑：首次导入会随机落在 ``oj-monitor``
    监控线程上，那一轮 GC 于是发生在错误线程里，可能去析构 PySide 控件。

    所以这两条一起钉：**模块顶层不导入**，**首次导入也不能在监控线程里发生**。
    """

    def test_sandbox_does_not_import_psutil_at_module_level(self) -> None:
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r);"
            "import offline_oj.core.sandbox as s;"
            "print('psutil' in sys.modules)"
            % str(Path(__file__).resolve().parent.parent)
        )
        # 必须在干净的子进程里查：本进程可能已经被别的用例导入过 psutil
        completed = subprocess.run([sys.executable, "-c", code],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "False",
                         "导入 offline_oj.core.sandbox 不应该顺带导入 psutil")

    def test_psutil_is_reachable_on_demand(self) -> None:
        from offline_oj.core import sandbox

        # 惰性不等于不可用：要内存数据时仍然拿得到
        self.assertEqual(sandbox.has_psutil(), sandbox.psutil_module() is not None)

    def test_monitor_thread_never_performs_the_first_import(self) -> None:
        """首次导入必须发生在调用线程上，不能落到 ``oj-monitor`` 那条线程上。

        监控线程是"每跑一组测试数据就新建一条"的短命线程。让它承担一次 C 扩展
        导入，就等于把一轮**全量 GC** 交给它：那轮回收会清掉进程里任何已经不可达
        的对象，其中若夹着 PySide 控件，析构就发生在监控线程而不是 GUI 线程 ——
        Qt 只允许在 GUI 线程析构控件，于是访问违例（崩溃点落在 importlib 里，
        跟判题逻辑毫无关系）。

        这条用例把"能力在构造时就定下来、线程只读缓存"钉死：只要有人把
        ``psutil_module()`` 挪回 ``_monitor_loop`` 里，这里就会报出来。
        """
        import subprocess
        import threading
        from unittest import mock

        from offline_oj.core import sandbox

        threads: list[str] = []

        def spy():
            threads.append(threading.current_thread().name)
            return None  # 假装没装：监控线程立刻收工，用例跑得也快

        process = subprocess.Popen([sys.executable, "-c", "pass"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.kill)
        self.addCleanup(process.communicate)
        with mock.patch.object(sandbox, "psutil_module", spy):
            monitor = sandbox.ProcessMonitor(
                process, time_limit_ms=60_000, memory_limit_mb=256)
            self.assertEqual(threads, [threading.current_thread().name],
                             "构造 ProcessMonitor 时就应该把内存限制能力定下来")
            monitor.wait()

        self.assertNotIn("oj-monitor", threads,
                         f"监控线程也去导入了 psutil：{threads}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
