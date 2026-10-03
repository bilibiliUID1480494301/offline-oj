"""导出的界面侧：对话框、PDF 渲染、以及"选中一份档案 → 导出"这条链。

``core/export.py`` 的测试（``test_export.py``）保证**产物是好的**；这里保证
**老师点得到、点得对、并且拿到的是好东西**。两件事分开测，是因为它们的失败
方式完全不同：前者是"文件打开是坏的"，后者是"按钮点不动 / 导错了场次"。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gc
import re
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.context import AppContext  # noqa: E402
from offline_oj.core import records  # noqa: E402
from offline_oj.core.export import ALL_SECTIONS, ExportData  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.export_dialog import EXPORT_CHOICES, ExportDialog  # noqa: E402
from offline_oj.ui.main_window import TAB_EXAM, MainWindow  # noqa: E402
from offline_oj.ui.panels.exam_panel import HOST_TAB_ARCHIVE  # noqa: E402


def sample_data() -> ExportData:
    return ExportData.from_mapping({
        "title": "期末模拟",
        "mode": "exam",
        "started_at": "2026-09-19T15:00:00",
        "problems": [{"id": "P0001", "title": "A+B", "points": 50}],
        "participants": [{"device_id": "AAAA", "username": "甲"}],
        "submissions": [{"serial": 1, "device_id": "AAAA", "username": "甲",
                         "problem_id": "P0001", "language": "cpp", "verdict": "AC",
                         "passed": 1, "total": 1, "score": 50, "possible": 50,
                         "code": "int main(){}",
                         "submitted_at": "2026-09-19T15:10:00"}],
        "leaderboard": {"max_total_score": 50, "overall": [
            {"rank": 1, "device_id": "AAAA", "username": "甲", "score": 50,
             "per_problem": {"P0001": 50}, "submit_count": 1}]},
        "max_total_score": 50,
        "keep_code": True,
    })


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _hold_gc_to_the_gui_thread():
    """导出用例期间**不让 GC 开火**，收尾在 GUI 线程上统一回收。

    这组用例的形态是"界面线程在 ``pump()`` 里转事件循环 + 一条 ``QThread``
    在写 xlsx / zip"。那条线程上的任何一次分配都可能触发一轮分代回收，而
    回收掉的会是**本轮刚建的 Qt 包装**（对话框、表格项……）—— 析构发生在
    错误的线程上就是 access violation。``conftest`` 的 ``_drain`` 已经在每个
    用例结束后把边界之前的对象冻结进永久代，护不住的只剩"用例进行中新建
    的"这一批：那就在用例期间直接关掉 GC，收尾时（工作线程早已收工、
    仍然在 GUI 线程上）再打开并收一次。多攒几百个对象无所谓，
    正确性是稀缺品。
    """
    gc.disable()
    yield
    gc.enable()
    gc.collect()


def pump(app, seconds: float, until) -> bool:
    """转到条件成立或超时。跟 ``smoke_gui`` 里那个是同一个路子。"""
    from time import monotonic
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        app.processEvents()
        if until():
            return True
        QApplication.processEvents()
    return until()


def wait_for_export(panel, app, path: Path, seconds: float = 30.0) -> bool:
    """等导出线程收工，条件里**必须包含"任务结束"**。

    只看 ``path.exists()`` 是不够的（原来就是这么写的，结果两条用例挂在这上面）：
    ``zipfile`` / ``QPdfWriter`` 都是**先建出文件再往里填**，文件一出现就断言，
    读到的必然是没有中央目录的半成品，报错是 ``BadZipFile: File is not a zip
    file`` —— 看起来像导出坏了，其实是断言抢跑了。

    ``_export_target`` 在开工前被设成目标、收工（成功或失败）时清回 ``None``，
    所以"它为 ``None``"就是任务结束的信号。产物本身是否完整由调用方再断言。
    """
    return pump(app, seconds,
                lambda: panel._export_target is None and Path(path).exists())


def valid_zip(path: Path) -> bool:
    """能当 zip 打开并且真有内容 —— 用来证明产物不是个半成品。"""
    import zipfile
    if not Path(path).exists():
        return False
    try:
        with zipfile.ZipFile(path) as bundle:
            return bool(bundle.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


# ======================================================================
# 对话框
# ======================================================================


class TestDialogDefaults:
    def test_the_default_name_is_already_filled_in(self, qapp, tmp_path):
        """默认名填好，老师改都不用改就能导 —— 少一次输入就少一次导错。"""
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        assert dialog.target().name == "20260919150000-期末模拟.txt"
        dialog.deleteLater()

    def test_both_sections_start_checked(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        assert dialog.selected_sections() == list(ALL_SECTIONS)
        dialog.deleteLater()

    def test_the_first_format_is_plain_text(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        assert dialog.selected_format() == "txt"
        dialog.deleteLater()

    def test_a_missing_source_is_disclosed_up_front(self, qapp, tmp_path):
        """档案没存源码时要**提前说**，别等导出完老师才发现包里没有代码。"""
        data = sample_data()
        data.keep_code = False
        dialog = ExportDialog(data, default_directory=tmp_path)
        texts = [child.text() for child in dialog.findChildren(type(dialog.hint))
                 if child.text()]
        assert any("没有保存源码" in text for text in texts), texts
        dialog.deleteLater()


class TestDialogFormatSwitching:
    def test_the_suffix_follows_the_chosen_format(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        for index, (_label, key, _hint) in enumerate(EXPORT_CHOICES):
            dialog.format_combo.setCurrentIndex(index)
            expected = ".zip" if key == "bundle" else f".{key}"
            assert dialog.target().suffix == expected, key

    def test_the_bundle_option_is_offered(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        keys = [dialog.format_combo.itemData(index)
                for index in range(dialog.format_combo.count())]
        assert keys == ["txt", "csv", "xlsx", "docx", "pdf", "bundle"]

    def test_choosing_the_bundle_disables_the_section_boxes(self, qapp, tmp_path):
        """档案包本身含两张表，再让人勾"要不要成绩单"只会让人以为单选。"""
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.format_combo.setCurrentIndex(len(EXPORT_CHOICES) - 1)
        assert dialog.selected_format() == "bundle"
        assert not dialog.scores_check.isEnabled()
        assert not dialog.submissions_check.isEnabled()

    def test_the_bundle_hint_mentions_the_source_folder(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.format_combo.setCurrentIndex(len(EXPORT_CHOICES) - 1)
        assert "sources/" in dialog.hint.text()

    def test_switching_back_reenables_the_section_boxes(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.format_combo.setCurrentIndex(len(EXPORT_CHOICES) - 1)
        dialog.format_combo.setCurrentIndex(0)
        assert dialog.scores_check.isEnabled()
        assert dialog.submissions_check.isEnabled()

    def test_a_hand_typed_name_keeps_its_stem(self, qapp, tmp_path):
        """只换后缀，不把老师自己起的名字冲掉。"""
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.path_edit.setText(str(tmp_path / "我要的名字.txt"))
        dialog.format_combo.setCurrentIndex(2)          # xlsx
        assert dialog.target().name == "我要的名字.xlsx"


class TestDialogValidation:
    def test_it_refuses_when_nothing_is_ticked(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.scores_check.setChecked(False)
        dialog.submissions_check.setChecked(False)
        dialog.accept()
        assert dialog.result() != QDialog.Accepted
        assert dialog.selected_sections() == []

    def test_the_bundle_from_an_empty_tick_still_passes(self, qapp, tmp_path):
        """档案包不走内容勾选 —— 空勾不该把它拦下。"""
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.format_combo.setCurrentIndex(len(EXPORT_CHOICES) - 1)
        dialog.scores_check.setChecked(False)
        dialog.submissions_check.setChecked(False)
        dialog.accept()
        assert dialog.result() == QDialog.Accepted

    def test_it_refuses_an_empty_path(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.path_edit.setText("   ")
        dialog.accept()
        assert dialog.result() != QDialog.Accepted
        assert "保存位置" in dialog.hint.text()

    def test_it_refuses_a_directory(self, qapp, tmp_path):
        """写到一个目录上会变成"导出成功但其实什么都没出来"。"""
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.path_edit.setText(str(tmp_path))
        dialog.accept()
        assert dialog.result() != QDialog.Accepted

    def test_the_problem_is_explained_in_the_hint(self, qapp, tmp_path):
        dialog = ExportDialog(sample_data(), default_directory=tmp_path)
        dialog.scores_check.setChecked(False)
        dialog.submissions_check.setChecked(False)
        dialog.accept()
        assert "勾一项" in dialog.hint.text()


# ======================================================================
# PDF
# ======================================================================


class TestPdf:
    def test_it_produces_a_real_pdf(self, qapp, tmp_path):
        from offline_oj.core import export as ex
        from offline_oj.ui.export_pdf import render_pdf

        target = render_pdf(ex.html_report(sample_data(), list(ALL_SECTIONS)),
                            tmp_path / "a.pdf", title="期末模拟")
        payload = target.read_bytes()
        assert payload.startswith(b"%PDF-")
        assert payload.rstrip().endswith(b"%%EOF")
        assert len(payload) > 1000

    def test_it_is_landscape_so_the_score_columns_fit(self, qapp, tmp_path):
        """成绩单的列数随题目数增长，纵向 A4 几道题就装不下。"""
        from offline_oj.core import export as ex
        from offline_oj.ui.export_pdf import render_pdf

        target = render_pdf(ex.html_report(sample_data(), list(ALL_SECTIONS)),
                            tmp_path / "a.pdf")
        # 注意是**带小数的**（A4 横向是 841.89 × 595.28 磅）——
        # 只匹配整数会一条都搜不到，然后误判成"PDF 里没有 MediaBox"
        box = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+"
                        rb"([\d.]+)\s+([\d.]+)\s*\]", target.read_bytes())
        assert box is not None, "PDF 里没有 /MediaBox"
        width, height = float(box.group(3)), float(box.group(4))
        assert width > height, (width, height)
        assert 800 < width < 900, width          # 确实是 A4 那一档，不是随便一张纸

    def test_chinese_text_gets_a_font_instead_of_boxes(self, qapp, tmp_path):
        """不嵌字体、也没有系统字体可用时中文会是一片方块。

        Qt 用系统字体渲染，所以这里只需要确认**真的有字体被写进 PDF** ——
        一个方块都画不出来的 PDF 里是不会有 Font 资源的。
        """
        from offline_oj.core import export as ex
        from offline_oj.ui.export_pdf import render_pdf

        target = render_pdf(ex.html_report(sample_data(), list(ALL_SECTIONS)),
                            tmp_path / "a.pdf")
        assert b"/Font" in target.read_bytes()

    def test_the_directory_is_created_on_demand(self, qapp, tmp_path):
        from offline_oj.core import export as ex
        from offline_oj.ui.export_pdf import render_pdf

        target = render_pdf(ex.html_report(sample_data(), list(ALL_SECTIONS)),
                            tmp_path / "deep" / "a.pdf")
        assert target.is_file()


# ======================================================================
# 面板接线
# ======================================================================


@pytest.fixture
def window(qapp, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("exportui")))
    win = MainWindow(AppContext.create(build_paths().ensure_layout()))
    yield win
    # 离屏没人点得到模态框，收尾前必须把这些状态清掉
    if win.exam_panel._server is not None:
        win.exam_panel.close_room(quiet=True)
    win.problems_panel._set_dirty(False)
    win.close()


def seed_archive(ctx, *, title: str = "期末模拟", keep_code: bool = True) -> None:
    """往档案目录里写一份真档案（不在测试里手搓假对象）。"""
    record = records.SessionRecord(
        title=title, mode="exam", started_at="2026-09-19T15:00:00",
        ended_at="2026-09-19T17:00:00",
        problems=[{"id": "P0001", "title": "A+B", "points": 50}],
        participants=[{"username": "甲", "device_id": "AAAA"}],
        submission_count=1, max_total_score=50, keep_code=keep_code)
    records.write_archive(
        ctx.paths.exams_dir, stamp="20260919-150000", record=record,
        submissions=[{"serial": 1, "device_id": "AAAA", "username": "甲",
                      "problem_id": "P0001", "language": "cpp", "attempt": 1,
                      "verdict": "AC", "passed": 1, "total": 1, "score": 50,
                      "possible": 50, "code": "int main(){}",
                      "submitted_at": "2026-09-19T15:10:00"}],
        leaderboard={"max_total_score": 50, "overall": [
            {"rank": 1, "device_id": "AAAA", "username": "甲", "score": 50,
             "per_problem": {"P0001": 50}, "submit_count": 1}],
            "per_problem": {}},
        keep_code=keep_code)


@pytest.fixture
def archive_panel(window):
    """切到考试页的「历史场次」，并备好一份可导出的档案。

    **必须先把 ``notify`` 的接收者拆掉。** ``MainWindow.notify`` 对 info 级也弹
    模态框（``QMessageBox.information``），而离屏环境里没人点得到它 ——
    ``exec()`` 会永久阻塞，整轮测试挂在那里、连一行输出都没有。
    这一条是拿一次 10 分钟挂死换来的。用例想收提示就自己接一个槽。
    """
    window.switch_tab(TAB_EXAM)
    panel = window.exam_panel
    panel.notify.disconnect()
    seed_archive(panel.ctx)
    panel.refresh_archives()
    panel.host_tabs.setCurrentIndex(HOST_TAB_ARCHIVE)
    return panel


class _StubDialog:
    """替掉真对话框：离屏环境里 ``QDialog.exec()`` 会永久阻塞。"""

    def __init__(self, target: Path, fmt: str = "txt", sections=None) -> None:
        self._target = Path(target)
        self._fmt = fmt
        self._sections = list(sections if sections is not None else ALL_SECTIONS)

    def exec(self) -> int:
        return QDialog.Accepted

    def selected_format(self) -> str:
        return self._fmt

    def selected_sections(self) -> list[str]:
        return self._sections

    def target(self) -> Path:
        return self._target


class TestArchivePage:
    def test_the_page_has_an_export_button(self, archive_panel):
        assert archive_panel.archive_export_button.text() == "导出…"

    def test_export_is_disabled_until_something_is_selected(self, archive_panel):
        """没选中就点，只会得到一句"请先选一份" —— 不如直接灰着。"""
        archive_panel.archive_table.clearSelection()
        archive_panel._on_archive_selected()
        assert not archive_panel.archive_export_button.isEnabled()
        archive_panel.archive_table.selectRow(0)
        assert archive_panel.archive_export_button.isEnabled()

    def test_exporting_without_a_selection_says_so(self, archive_panel, monkeypatch):
        archive_panel.archive_table.clearSelection()
        warnings: list[str] = []
        archive_panel.notify.connect(lambda level, text: warnings.append(text))
        archive_panel.export_selected_archive()
        assert any("先" in text for text in warnings), warnings

    def test_a_cancelled_dialog_writes_nothing(self, archive_panel, tmp_path, monkeypatch):
        class Cancelled(_StubDialog):
            def exec(self) -> int:
                return QDialog.Rejected

        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: Cancelled(tmp_path / "x.txt"))
        archive_panel.export_selected_archive()
        assert not (tmp_path / "x.txt").exists()

    def test_a_text_export_really_lands_on_disk(self, archive_panel, tmp_path,
                                                monkeypatch, qapp):
        target = tmp_path / "out.txt"
        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target))
        archive_panel.export_selected_archive()
        assert wait_for_export(archive_panel, qapp, target), "导出线程没有写出文件"
        text = target.read_text(encoding="utf-8-sig")
        assert "期末模拟" in text and "成绩单" in text and "提交明细" in text

    def test_an_xlsx_export_really_lands_on_disk(self, archive_panel, tmp_path,
                                                 monkeypatch, qapp):
        target = tmp_path / "out.xlsx"
        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target, "xlsx"))
        archive_panel.export_selected_archive()
        assert wait_for_export(archive_panel, qapp, target)
        assert valid_zip(target), "xlsx 不是一个完整的 zip"
        import zipfile
        with zipfile.ZipFile(target) as bundle:
            assert "xl/workbook.xml" in bundle.namelist()

    def test_the_pdf_path_renders_on_the_gui_thread(self, archive_panel, tmp_path,
                                                    monkeypatch, qapp):
        """PDF 走"工作线程出 HTML、界面线程渲染"，这条链最容易断在信号上。"""
        target = tmp_path / "out.pdf"
        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target, "pdf"))
        archive_panel.export_selected_archive()
        assert wait_for_export(archive_panel, qapp, target), "PDF 没有被渲染出来"
        payload = target.read_bytes()
        assert payload.startswith(b"%PDF-") and payload.rstrip().endswith(b"%%EOF")

    def test_the_bundle_carries_the_source_code(self, archive_panel, tmp_path,
                                                monkeypatch, qapp):
        target = tmp_path / "out.zip"
        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target, "bundle"))
        archive_panel.export_selected_archive()
        assert wait_for_export(archive_panel, qapp, target)
        assert valid_zip(target)
        import zipfile
        with zipfile.ZipFile(target) as bundle:
            sources = [n for n in bundle.namelist() if n.startswith("sources/")]
        assert sources and sources[0].endswith(".cpp")

    def test_the_run_is_reported_in_the_log(self, archive_panel, tmp_path,
                                            monkeypatch, qapp):
        """导完要有回执 —— 否则老师不知道文件到底出没出来、在哪儿。"""
        target = tmp_path / "out.txt"
        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target))
        archive_panel.export_selected_archive()
        assert wait_for_export(archive_panel, qapp, target)
        log_text = archive_panel.host_log.toPlainText()
        assert "已导出" in log_text and "out.txt" in log_text

    def test_a_failed_export_leaves_no_half_written_file(self, archive_panel,
                                                         tmp_path, monkeypatch, qapp):
        """失败时磁盘上不能留下一个"看着像成功"的半成品。

        ``zipfile`` 会先把文件建出来，所以直接往目标名字上写的话，中途失败
        会留下一个 Excel 打不开、但**文件名和位置都对**的东西 —— 老师会以为
        导出成功了，直到考前打开才发现是坏的。
        """
        target = tmp_path / "out.xlsx"
        archive_panel.archive_table.selectRow(0)
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target, "xlsx"))
        # 让真正的写盘那一步失败
        from offline_oj.core import export as ex
        monkeypatch.setattr(ex, "_sheet_xml",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满了")))
        archive_panel.export_selected_archive()
        from time import monotonic
        deadline = monotonic() + 20.0
        while monotonic() < deadline and archive_panel._export_target is not None:
            qapp.processEvents()
        assert not target.exists(), "失败之后留下了半成品"
        assert not list(tmp_path.glob("*.partial")), "失败之后留下了 .partial"

    def test_a_failure_is_reported_not_swallowed(self, archive_panel, tmp_path,
                                                 monkeypatch, qapp):
        """写不进去必须报出来。静默失败会让老师拿着一份不存在的文件去上课。"""
        errors: list[str] = []
        archive_panel.notify.connect(lambda level, text: errors.append(text))
        archive_panel.archive_table.selectRow(0)
        # 目标是一个**目录**：写文件时必定失败
        target = tmp_path / "adir"
        target.mkdir()
        monkeypatch.setattr(archive_panel, "_export_dialog",
                            lambda data: _StubDialog(target))
        archive_panel.export_selected_archive()
        assert pump(qapp, 20.0, lambda: bool(errors)), "导出失败却没有报出来"

    def test_exporting_the_wrong_session_is_impossible(self, window, monkeypatch,
                                                       tmp_path, qapp):
        """两份档案时，导出的必须是**选中的那一份**。

        "导错了场次"是这类功能最坏的失败方式：文件是好的、打开也正常，
        只是里面的名字和成绩全属于另一个班。
        """
        window.switch_tab(TAB_EXAM)
        panel = window.exam_panel
        seed_archive(panel.ctx, title="第一场")
        seed_archive(panel.ctx, title="第二场")
        # 两份档案的时间戳一样，用标题区分；records 会给出唯一目录名
        panel.refresh_archives()
        panel.host_tabs.setCurrentIndex(HOST_TAB_ARCHIVE)
        assert len(panel._archives) == 2

        titles = {item.record.title for item in panel._archives}
        assert titles == {"第一场", "第二场"}

        row = next(index for index, item in enumerate(panel._archives)
                   if item.record.title == "第二场")
        panel.archive_table.selectRow(row)
        target = tmp_path / "second.txt"
        monkeypatch.setattr(panel, "_export_dialog",
                            lambda data: _StubDialog(target))
        panel.export_selected_archive()
        assert pump(qapp, 20.0, target.exists)
        text = target.read_text(encoding="utf-8-sig")
        assert "第二场" in text and "第一场" not in text
