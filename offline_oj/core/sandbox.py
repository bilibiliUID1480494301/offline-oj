"""运行结果、编译器互斥锁与进程监控。

本地评测无法使用 cgroup / job object 之外的强隔离手段，可行的资源限制方式是
"边跑边看，超了就杀"：

* **时间限制** —— 主线程 ``communicate(timeout=...)`` 兜底，监控线程按 10ms 粒度
  实测墙钟时间；
* **内存限制** —— 需要 psutil 轮询进程 RSS。未安装 psutil 时优雅降级：只做时间限制，
  并在结果里明确说明"内存限制未生效"，而不是假装通过了。

与被测程序交互时统一走 UTF-8、``errors="replace"``，避免中文输出导致解码异常。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from ..win32.process import kill_tree
from .models import Verdict

#: psutil 是可选依赖，只在真的要测内存时才导入 —— 见 :func:`psutil_module`
_PSUTIL: Any = None
#: 探测过没有？``None`` 表示还没试过，这样"没装"也只会付一次代价
_PSUTIL_STATE: bool | None = None


def psutil_module() -> Any:
    """惰性导入 psutil，返回模块对象；不可用时返回 ``None``。

    不在模块顶层 ``import``，有两个理由：

    1. **导入它是有副作用的。** psutil 会加载一个 C 扩展并初始化 Winsock。
       实测在某台机器上，只要在 Qt 之前 ``import psutil``，之后 PySide6 的
       ``QStyledItemDelegate`` 在被绘制时就会抛 access violation —— 崩溃点与 psutil
       没有任何逻辑关联（是代码补全的候选列表自绘），把这一次导入去掉就恢复正常。
       判题本身并不需要这个副作用。
    2. 内存限制本来就是**可选能力**：用户没装 psutil、或者题目没有内存限制时，
       没有理由为它付出启动开销。未装时按"只做时间限制"优雅降级（见下方注释）。

    探测结果会缓存在模块变量里，所以"没装 psutil"这条路径也只会付一次导入代价。

    **调用方注意**：调用它就意味着"可能要导入一个 C 扩展"，导入会分配内存并触发
    分代回收，因此必须**在监控线程之外**先调用一次（:class:`ProcessMonitor`
    就是在构造时定下来的）。否则首次导入会随机落在 `oj-monitor` 那条短命线程上，
    那一轮 GC 会去回收进程里任何已不可达的对象 —— 若其中夹着 PySide 控件，
    PySide 的析构会发生在监控线程而不是 GUI 线程，Qt 不允许，于是访问违例。
    """
    global _PSUTIL, _PSUTIL_STATE
    if _PSUTIL_STATE is None:
        try:
            import psutil as module
        except ImportError:  # pragma: no cover - 取决于运行环境
            _PSUTIL, _PSUTIL_STATE = None, False
        else:
            _PSUTIL, _PSUTIL_STATE = module, True
    return _PSUTIL


def has_psutil() -> bool:
    """内存限制能力是否可用。"""
    return psutil_module() is not None

log = logging.getLogger(__name__)

#: 输出体积上限（字节），防止死循环打印撑爆内存
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
#: 内存采样间隔（秒）
POLL_INTERVAL = 0.01
#: 每 N 次采样统计一次子进程内存（遍历进程树开销较高）
CHILD_SAMPLE_EVERY = 10
#: 判题时给超时判定预留的宽限（秒）
TIMEOUT_GRACE = 0.5


@dataclass
class RunResult:
    """一次运行的结果。"""

    verdict: Verdict
    stdout: str = ""
    stderr: str = ""
    time_ms: float = 0.0
    memory_mb: float = 0.0
    message: str = ""
    exit_code: int | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.AC

    @property
    def error_text(self) -> str:
        """优先返回判题机说明，其次返回被测程序的 stderr。"""
        return (self.message or self.stderr or "").strip()

    def format_detail(self, *, indent: str = "    ") -> str:
        """多行详情，供结果面板展示。"""
        lines: list[str] = []
        if self.time_ms:
            lines.append(f"耗时 {self.time_ms:.1f} ms")
        if self.memory_mb:
            lines.append(f"内存 {self.memory_mb:.1f} MB")
        if self.exit_code is not None and self.exit_code != 0:
            lines.append(f"退出码 {self.exit_code}")
        if self.truncated:
            lines.append(f"输出超过 {MAX_OUTPUT_BYTES // 1024 // 1024} MB，已截断")
        detail = " · ".join(lines)
        if self.error_text:
            detail = f"{detail}\n{indent}{self.error_text}" if detail else self.error_text
        return detail

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "time_ms": self.time_ms,
            "memory_mb": self.memory_mb,
            "message": self.message,
            "exit_code": self.exit_code,
            "truncated": self.truncated,
        }


class CompilerLock:
    """按编译器路径加锁，串行化编译动作。

    多个判题线程共用同一个 ``g++`` 时，同时启动大量编译进程会让磁盘 I/O 打满、
    反而整体变慢。按可执行文件路径加锁能让并发判题保持稳定。
    """

    _locks: dict[str, threading.Lock] = {}
    _registry_lock = threading.Lock()

    @classmethod
    def _lock_for(cls, key: str) -> threading.Lock:
        with cls._registry_lock:
            if key not in cls._locks:
                cls._locks[key] = threading.Lock()
            return cls._locks[key]

    @classmethod
    @contextmanager
    def hold(cls, compiler_path: str, timeout: float = 30.0) -> Iterator[None]:
        lock = cls._lock_for(compiler_path or "<default>")
        acquired = lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(f"等待编译器锁超时（>{timeout:.0f}s）: {compiler_path}")
        try:
            yield
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass


class ProcessMonitor:
    """边运行边监控时间与内存，越界即终止进程树。"""

    def __init__(
        self,
        process: subprocess.Popen,
        time_limit_ms: int,
        memory_limit_mb: int,
        *,
        include_children: bool = False,
    ) -> None:
        self.process = process
        self.time_limit = max(1, int(time_limit_ms)) / 1000.0
        self.memory_limit = max(1, int(memory_limit_mb)) * 1024 * 1024
        self.include_children = include_children

        # 这里就把 psutil 定下来，**不要留给监控线程去惰性导入**。
        # 原因见 sandbox 模块顶部与 psutil_module 的说明：导入会分配内存、
        # 触发分代回收，而监控线程是每跑一组测试数据就新建一条的短命线程，
        # 让"第一次导入"落在这条线程上，等于把一次全量 GC 交给它 ——
        # 那轮 GC 会顺手回收进程中任何已经不可达的对象，包括别处留下的
        # PySide 控件；而 PySide 的 tp_dealloc 会在**当前线程**上析构 QWidget，
        # Qt 只允许在 GUI 线程析构控件，于是访问违例（实测崩溃点落在
        # importlib 的 _compile_bytecode 里，跟判题逻辑毫无关系）。
        self._psutil = psutil_module()

        self._start = time.perf_counter()
        self._peak_memory = 0
        self._killed_reason: Verdict | None = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()

    # ---- 对外接口 ---------------------------------------------------------

    def wait(self, input_data: str | None = None) -> RunResult:
        """喂入输入并等待结束，返回本次运行结果。"""
        monitor = threading.Thread(target=self._monitor_loop, daemon=True,
                                   name="oj-monitor")
        monitor.start()

        stdout = stderr = ""
        returncode: int | None = None
        try:
            stdout, stderr = self.process.communicate(
                input=input_data, timeout=self.time_limit + TIMEOUT_GRACE
            )
            returncode = self.process.returncode
        except subprocess.TimeoutExpired:
            self._kill(Verdict.TLE)
            try:
                stdout, stderr = self.process.communicate(timeout=2.0)
            except Exception:
                pass
            returncode = -1
        except Exception as exc:  # 罕见的管道异常
            self._kill(Verdict.IE)
            log.exception("等待子进程失败")
            return RunResult(Verdict.IE, message=f"等待子进程失败: {exc}")
        finally:
            self._stop.set()
            monitor.join(timeout=1.0)

        return self._build_result(stdout or "", stderr or "", returncode)

    # ---- 内部实现 ---------------------------------------------------------

    def _build_result(self, stdout: str, stderr: str, returncode: int | None) -> RunResult:
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        memory_mb = self._peak_memory / (1024 * 1024)

        truncated = False
        if len(stdout) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES]
            truncated = True

        with self._state_lock:
            reason = self._killed_reason

        message = ""
        if reason is Verdict.TLE:
            verdict = Verdict.TLE
            elapsed_ms = max(elapsed_ms, self.time_limit * 1000.0)
            message = f"超过时间限制 {self.time_limit * 1000:.0f} ms"
        elif reason is Verdict.MLE:
            verdict = Verdict.MLE
            message = f"超过内存限制 {self.memory_limit / 1024 / 1024:.0f} MB"
        elif reason is Verdict.OLE:
            verdict = Verdict.OLE
            message = "输出体积过大"
        elif reason is Verdict.IE:
            verdict = Verdict.IE
            message = "评测过程异常，已终止进程"
        elif returncode not in (0, None):
            verdict = Verdict.RE
            message = f"程序以非零状态退出（{returncode}）"
        elif truncated:
            verdict = Verdict.OLE
            message = "输出体积过大"
        else:
            verdict = Verdict.AC

        if self._psutil is None and verdict is not Verdict.AC:
            message = f"{message}（未安装 psutil，内存限制未生效）" if message else ""

        return RunResult(
            verdict=verdict,
            stdout=stdout,
            stderr=stderr,
            time_ms=elapsed_ms,
            memory_mb=memory_mb,
            message=message,
            exit_code=returncode,
            truncated=truncated,
        )

    def _kill(self, reason: Verdict) -> None:
        with self._state_lock:
            if self._killed_reason is None:
                self._killed_reason = reason
        try:
            kill_tree(self.process.pid)
        except Exception:
            log.debug("终止进程失败", exc_info=True)

    def _monitor_loop(self) -> None:
        """在独立线程里采样时间与内存。"""
        if self._psutil is None:
            # 没有 psutil 就没有可采样的内存数据；时间限制由主线程的
            # communicate(timeout=...) 兜底，见 wait()
            return

        tick = 0
        while not self._stop.is_set():
            try:
                if self.process.poll() is not None:
                    break

                elapsed = time.perf_counter() - self._start
                if elapsed > self.time_limit:
                    self._kill(Verdict.TLE)
                    break

                tick += 1
                sample_children = self.include_children or tick % CHILD_SAMPLE_EVERY == 0
                usage = self._memory_usage(sample_children)
                if usage > self._peak_memory:
                    self._peak_memory = usage

                if usage > self.memory_limit:
                    self._kill(Verdict.MLE)
                    break

                time.sleep(POLL_INTERVAL)
            except Exception:
                break

    def _memory_usage(self, include_children: bool) -> int:
        """当前进程（含子进程）的常驻内存占用，单位字节。"""
        psutil = self._psutil
        if psutil is None:
            return 0
        try:
            parent = psutil.Process(self.process.pid)
            total = parent.memory_info().rss
            if include_children:
                for child in parent.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            return total
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0
        except Exception:
            return 0
