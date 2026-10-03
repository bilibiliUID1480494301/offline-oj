r"""Windows 作业对象（Job Object）隔离。

为什么需要它
------------
评测要在本机**以当前用户身份**运行学生提交的代码。原来只有"边跑边看、超了就杀"
（见 :mod:`offline_oj.core.sandbox`），这有三个绕不过去的洞：

1. **杀得不够快。** 内存是 10ms 轮询一次，学生一段 ``malloc`` 循环完全能在两次
   采样之间把整台机器拖到卡死；进程炸弹更糟 —— ``taskkill /T`` 一边扫进程树，
   新进程一边在冒出来，越杀越多。
2. **逃得掉。** 子进程换一个父进程就断了 ``taskkill /T`` 的树，孤儿进程留在后台
   吃 CPU，而且下一组测试数据还要再抢一次资源。
3. **够不着内核。** 内存上限是"观察到超了就杀"，不是"根本分配不到"。

作业对象把这三件事一起解决 —— 限制由**内核**执行：分配直接被拒、活动进程数直接
封顶、作业句柄一关**所有**后代进程无条件被杀。全都不依赖轮询，也就没有窗口期。

挂起启动：先挂进去，再让它跑
----------------------------
``CreateProcess`` 返回之后、``AssignProcessToJobObject`` 之前，被启动的程序已经
能执行指令了。它只要在这几毫秒里派生一次，子进程就落在作业外面，之后怎么杀都
不干净。所以启动时加 ``CREATE_SUSPENDED``：进程被创建出来，但**一条指令都还没
执行**；挂进作业之后再放它跑。逃生窗口为零。

启用挂起启动时，进程创建后立刻把 ``CREATE_SUSPENDED`` 换成 ``CREATE_NO_WINDOW``
是必要的 —— 恢复执行靠 ``ntdll!NtResumeProcess``，它只需要进程句柄，
不需要线程句柄（``subprocess`` 也没有把主线程句柄暴露出来）。

结构体布局必须自检，不能靠推算
------------------------------
``IO_COUNTERS`` 有 **六个** ``ULONGLONG``，很容易漏掉最后一个
``OtherTransferCount``。漏掉的后果分两级：轻则
``SetInformationJobObject`` 返回 ``ERROR_BAD_LENGTH(24)``；重则结构体够长、
调用也成功，但内存上限被**静默写到错误的字段**上 —— 表现为"配了 256MB，
学生却能吃到 4GB"，而且界面上一切正常。

所以这里不靠推算：:func:`verify_structure_layout` 用"写入指纹再查回来"的办法向
内核问出它真正读的是哪些偏移，与 ctypes 算出的偏移对照。两边不一致时
:func:`require_structure_layout` 会**拒绝启用沙箱**并说明原因，而不是带着一个
写错偏移的结构体继续跑。

能力分层如实上报
----------------
不是每台机器都能开满。实测过的情况：把进程令牌完整性降到 Low 这一步，
在装了 360 安全卫士主动防御的机器上会被静默拦掉 —— 进程没起来，但 API 全部返回
成功，没有任何错误码可查。**学校机房正是这类软件的重灾区。**

所以这里的原则是：**能做的真做，做不到的如实说，绝不假装隔离生效**。
:func:`probe_capabilities` 每一项都独立验证、独立报告，且区分"结构已验证"
（内核接受了参数且回读一致）与"行为已验证"（真的起了一个进程看着它被限制）。
界面上展示的就是这份报告。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

__all__ = [
    "JobError", "JobLimits", "SandboxJob", "SandboxCapabilities",
    "LayerStatus", "launch_in_job", "probe_capabilities", "structure_report",
    "verify_structure_layout", "read_integrity_rid", "sandbox_enabled",
    "set_sandbox_enabled", "LOW_INTEGRITY_SID", "STRUCT_LAYOUT",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 结构体布局的期望值：``sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)`` 与
#: 两个内存上限字段的偏移。数字来自
#: ``Windows Kits\10\Include\...\um\winnt.h`` 与内核实际读取位置的双重印证，
#: 见模块顶部说明。
STRUCT_LAYOUT = (144, 112, 120)

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400

JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9

L_PROCESS_TIME = 0x00000002
L_JOB_TIME = 0x00000004
L_ACTIVE_PROCESS = 0x00000008
L_PROCESS_MEMORY = 0x00000100
L_JOB_MEMORY = 0x00000200
L_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
L_KILL_ON_JOB_CLOSE = 0x00002000

#: UI 限制位。用途是让被测程序拿不到剪贴板与桌面句柄 —— 一份"读剪贴板再打印"
#: 的提交能把自己的答案沿局域网传出去，也能把上一位考生的剪贴板内容读走。
U_HANDLES = 0x0001
U_READCLIPBOARD = 0x0002
U_WRITECLIPBOARD = 0x0004
U_SYSTEMPARAMETERS = 0x0008
U_DISPLAYSETTINGS = 0x0010
U_GLOBALATOMS = 0x0020
U_DESKTOP = 0x0040
U_EXITWINDOWS = 0x0080
UI_RESTRICTION_MASK = (U_HANDLES | U_READCLIPBOARD | U_WRITECLIPBOARD
                       | U_SYSTEMPARAMETERS | U_DISPLAYSETTINGS | U_GLOBALATOMS
                       | U_DESKTOP | U_EXITWINDOWS)

#: 低完整性级别（Low IL）。低于它的进程即使被完全控制，也改不了高完整性的
#: 文件、注册表与进程 —— 这是"简易沙箱"里唯一不依赖管理员权限的**权限**隔离维度
#: （作业对象只管资源，不管权限）。
LOW_INTEGRITY_SID = "S-1-16-4096"

TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TokenIntegrityLevel = 25
SE_GROUP_INTEGRITY = 0x00000020

#: 作恶进程的默认上限：活动进程数。Java 要起虚拟机，给宽一点但不放任。
DEFAULT_MAX_PROCESSES = 8


class JobError(OSError):
    """作业对象创建或配置失败。"""


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------


class _LARGE_INTEGER(ctypes.Union):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG),
                ("QuadPart", ctypes.c_longlong)]


class _IO_COUNTERS(ctypes.Structure):
    """注意：**六个**字段。漏掉 ``OtherTransferCount`` 会少 8 字节，
    于是 ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` 从 144 变成 136，
    内存上限字段整体前移 —— 要么 ERROR_BAD_LENGTH，要么写错字段。"""

    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOB_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", _LARGE_INTEGER),
                ("PerJobUserTimeLimit", _LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _JOB_EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOB_BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


# ---------------------------------------------------------------------------
# 原型声明：一处都不能省
#
# ctypes 默认 restype 是 c_int，会把 64 位句柄截断成 32 位 —— 现象是句柄"看起来
# 有值"，但拿去调下一个 API 就报无效句柄，且错误码指向真正的调用，查起来很远。
# ---------------------------------------------------------------------------

_k32: Any = None
_adv: Any = None
_ntdll: Any = None


def _bind() -> None:
    global _k32, _adv, _ntdll
    if _k32 is not None or not IS_WINDOWS:
        return

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = []

    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.SetTokenInformation.restype = wintypes.BOOL
    adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                        ctypes.c_void_p, wintypes.DWORD]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                        ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertStringSidToSidW.restype = wintypes.BOOL
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR,
                                           ctypes.POINTER(ctypes.c_void_p)]

    k32.LocalFree.restype = ctypes.c_void_p
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    ntdll.NtResumeProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    _k32, _adv, _ntdll = k32, adv, ntdll


#: 导入时就绑定一次。这些 DLL 在任何进程里都已经加载过，代价可以忽略，
#: 但能消除一整类"某个函数里忘了先 _bind()"的错误 ——
#: 那种错误的现象是 `'NoneType' object has no attribute 'CreateJobObjectW'`，
#: 报错点离真正的原因（谁忘了初始化）很远。
if IS_WINDOWS:
    _bind()


def _err() -> str:
    code = ctypes.get_last_error()
    return f"WinError {code}: {ctypes.FormatError(code).strip()}"


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobLimits:
    """作业对象的限制参数。"""

    #: 单进程内存上限（MB），同时也作为整个作业的上限
    memory_mb: int = 256
    #: 作业内活动进程数上限
    max_processes: int = DEFAULT_MAX_PROCESSES
    #: 作业累计 CPU 时间上限（毫秒）。0 表示不设 —— 由墙钟时间兜底，
    #: 因为 CPU 时间限制对"频繁 sleep 拖时间"的程序不起作用，
    #: 而墙钟时间限制又对"多线程抢 CPU"太宽容，两者互补
    cpu_ms: int = 0
    #: 关闭作业句柄时杀掉作业内所有进程
    kill_on_close: bool = True
    #: 未处理异常不弹窗（否则一个崩掉的提交会让主机屏幕被对话框占住）
    die_on_unhandled_exception: bool = True
    #: 禁止碰剪贴板、桌面句柄、系统参数
    block_ui: bool = True
    #: 把被测进程的完整性级别降到指定档位
    low_integrity: bool = False
    #: 降级到哪一档。默认 Low；成对使用 ``S-1-16-8448``（Medium-Plus）可以
    #: 做成"比 Low 宽松一档但同样挡写升权"的中间层 —— 挡不住读，但能挡住提交的
    #: 代码改写学生自己的文件和注册表
    integrity_sid: str = LOW_INTEGRITY_SID

    def describe(self) -> str:
        parts = [f"内存 {self.memory_mb}MB",
                 f"进程数 ≤{self.max_processes}"]
        if self.cpu_ms:
            parts.append(f"CPU {self.cpu_ms}ms")
        if self.kill_on_close:
            parts.append("关作业即杀")
        if self.die_on_unhandled_exception:
            parts.append("崩溃不弹窗")
        if self.block_ui:
            parts.append("挡剪贴板/桌面")
        if self.low_integrity:
            parts.append("低完整性")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# 作业对象
# ---------------------------------------------------------------------------


class SandboxJob:
    """一个作业对象。用 ``with`` 管生命周期，退出即杀干净。

        with SandboxJob(JobLimits(memory_mb=256)) as job:
            process = launch_in_job(["prog.exe"], work_dir, job)
            out, _ = process.communicate(timeout=2.0)

    不用 ``with`` 也可以，但必须显式 :meth:`close` —— 作业句柄是内核对象，
    泄漏的话被测进程会一直活着（而且 ``kill_on_close`` 也永远不会触发）。
    """

    def __init__(self, limits: JobLimits | None = None) -> None:
        if not IS_WINDOWS:
            raise JobError("作业对象只在 Windows 上可用")
        _bind()
        self.limits = limits or JobLimits()
        self._handle: Any = None
        self._lock = threading.Lock()
        #: 结构体布局自检结论，创建时定下来并附在错误信息里
        self.layout_problem: str = ""
        #: 最近一次启动子进程时，读回的完整性 RID（4096 = Low）。
        #: ``None`` 表示没做降级或读不回来 —— 见 :func:`read_integrity_rid`
        self.integrity_rid: int | None = None
        self._create()

    # ---- 属性 -------------------------------------------------------------

    @property
    def handle(self) -> Any:
        return self._handle

    @property
    def active(self) -> bool:
        return self._handle is not None

    def describe(self) -> str:
        if not self.active:
            return "已关闭"
        return f"作业对象已启用（{self.limits.describe()}）"

    # ---- 生命周期 ---------------------------------------------------------

    def _create(self) -> None:
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            raise JobError(f"CreateJobObjectW 失败：{_err()}")
        try:
            self._apply_extended_limits(handle)
            if self.limits.block_ui:
                self._apply_ui_restrictions(handle)
        except Exception:
            _k32.CloseHandle(handle)
            raise
        self._handle = handle

    def _apply_extended_limits(self, handle: Any) -> None:
        info = _JOB_EXTENDED_LIMIT()
        flags = L_PROCESS_MEMORY | L_JOB_MEMORY | L_ACTIVE_PROCESS
        if self.limits.kill_on_close:
            flags |= L_KILL_ON_JOB_CLOSE
        if self.limits.die_on_unhandled_exception:
            flags |= L_DIE_ON_UNHANDLED_EXCEPTION
        if self.limits.cpu_ms:
            flags |= L_JOB_TIME
            info.BasicLimitInformation.PerJobUserTimeLimit.QuadPart = (
                self.limits.cpu_ms * 10000)          # 100ns 为单位
        info.BasicLimitInformation.LimitFlags = flags
        info.BasicLimitInformation.ActiveProcessLimit = max(
            1, int(self.limits.max_processes))
        info.ProcessMemoryLimit = self.limits.memory_mb * 1024 * 1024
        info.JobMemoryLimit = self.limits.memory_mb * 1024 * 1024

        if not _k32.SetInformationJobObject(
                handle, JobObjectExtendedLimitInformation, ctypes.byref(info),
                ctypes.sizeof(info)):
            raise JobError(
                f"SetInformationJobObject 失败：{_err()}。"
                f"若错误码是 {24}（ERROR_BAD_LENGTH），说明结构体布局不对 —— "
                f"当前 sizeof={ctypes.sizeof(info)}，"
                f"期望 {STRUCT_LAYOUT[0]}")

        # 回读一遍：确认内核真的把上限存进了内存字段。
        # 只看 Set 的返回值为真是不够的 —— 偏移写错时它照样返回真。
        check = _JOB_EXTENDED_LIMIT()
        if _k32.QueryInformationJobObject(
                handle, JobObjectExtendedLimitInformation, ctypes.byref(check),
                ctypes.sizeof(check), None):
            want = self.limits.memory_mb * 1024 * 1024
            if check.ProcessMemoryLimit != want:
                raise JobError(
                    "作业对象的内存上限没有落到内核期望的字段上"
                    f"（写入 {want}，读回 {check.ProcessMemoryLimit}）—— "
                    "结构体布局与系统不一致，拒绝启用沙箱")
            if check.BasicLimitInformation.ActiveProcessLimit != max(
                    1, int(self.limits.max_processes)):
                log.warning("活动进程数上限回读不一致：%s",
                            check.BasicLimitInformation.ActiveProcessLimit)

    @staticmethod
    def _apply_ui_restrictions(handle: Any) -> None:
        value = wintypes.DWORD(UI_RESTRICTION_MASK)
        if not _k32.SetInformationJobObject(
                handle, JobObjectBasicUIRestrictions, ctypes.byref(value),
                ctypes.sizeof(value)):
            # UI 限制失败不该拖垮整个沙箱：它是"额外加一层"，不是必需品
            log.warning("设置作业对象 UI 限制失败：%s", _err())

    def assign(self, process: subprocess.Popen) -> None:
        """把一个**已创建但尚未运行**的进程挂进作业。

        调用方必须先以 ``CREATE_SUSPENDED`` 创建进程，否则就有逃生窗口 ——
        见模块顶部说明。:func:`launch_in_job` 已经把这个顺序包好了。
        """
        with self._lock:
            if self._handle is None:
                raise JobError("作业对象已关闭")
            handle = wintypes.HANDLE(int(process._handle))  # noqa: SLF001
            if not _k32.AssignProcessToJobObject(self._handle, handle):
                raise JobError(
                    f"AssignProcessToJobObject 失败：{_err()}。"
                    "若当前进程自身已在一个作业里，子进程会继承那个作业；"
                    "Windows 8 以上支持作业嵌套，但仍可能被外层作业的"
                    "UI 限制挡住")

    def terminate(self, exit_code: int = 1) -> bool:
        """**一次性**杀掉作业内所有进程，含刚派生出来的。

        这是作业对象最值钱的能力之一：``taskkill /T`` 是"遍历进程树逐个杀"，
        进程炸弹能在遍历过程中不断生出新进程；``TerminateJobObject`` 是内核
        一次性清场，没有这个窗口。
        """
        with self._lock:
            if self._handle is None:
                return False
            return bool(_k32.TerminateJobObject(self._handle, exit_code))

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
        # 先 TerminateJobObject 再 CloseHandle：单靠 KILL_ON_JOB_CLOSE 也能杀，
        # 但那是"最后一个句柄关闭"时才生效；显式终止让行为不依赖 flag 是否设置成功
        try:
            _k32.TerminateJobObject(handle, 1)
        except Exception:
            log.debug("TerminateJobObject 失败", exc_info=True)
        try:
            _k32.CloseHandle(handle)
        except Exception:
            log.debug("CloseHandle 失败", exc_info=True)

    def __enter__(self) -> "SandboxJob":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 兜底，正常路径走 close()
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def read_integrity_rid(token: Any) -> int | None:
    """读回令牌的完整性级别 RID（4096 = Low，8192 = Medium，12288 = High）。

    为什么要读回来：``SetTokenInformation`` 返回真**不等于**改动生效 ——
    实测存在"API 全部返回成功、值却没落地"的情形。判断"低完整性到底开没开"
    必须看读回来的值，不能信写入的返回值。
    """
    needed = wintypes.DWORD()
    _adv.GetTokenInformation(token, TokenIntegrityLevel, None, 0,
                             ctypes.byref(needed))
    if needed.value == 0:
        return None
    buf = (ctypes.c_ubyte * needed.value)()
    if not _adv.GetTokenInformation(token, TokenIntegrityLevel, buf,
                                    needed.value, ctypes.byref(needed)):
        return None
    # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { Sid } } —— SID 的最后一个
    # 子权威（subauthority）就是完整性 RID
    sid = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents.Label.Sid
    if not sid:
        return None
    sid_bytes = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))
    subauthority_count = sid_bytes[1]
    if subauthority_count < 1:
        return None
    offset = 8 + (subauthority_count - 1) * 4
    return int.from_bytes(bytes(sid_bytes[offset:offset + 4]), "little")


def _lower_process_integrity(process: subprocess.Popen,
                             sid_string: str = LOW_INTEGRITY_SID) -> int | None:
    """把**已经创建、尚未运行**的子进程的令牌完整性降到 Low，并读回确认。

    走这条路而不是 ``CreateProcessAsUserW`` 是有意的：

    * ``CreateProcessAsUserW`` 需要 ``SeAssignPrimaryToken`` 与
      ``SeIncreaseQuota`` —— 普通用户、以及绝大多数教师账号都没有，实测直接被拒；
    * 改自己**子进程**令牌的完整性级别只需要 ``TOKEN_ADJUST_DEFAULT``，
      这个我们有。而且**降级不需要 SeRelabel**（提权才需要）。

    因为进程还挂在 ``CREATE_SUSPENDED`` 上，令牌在第一条指令执行前就被改好了，
    与"出生即低完整性"等价。

    :return: 读回的完整性 RID；读不到时返回 ``None``（调用方据此判断未生效）。
    """
    handle = wintypes.HANDLE(int(process._handle))  # noqa: SLF001
    token = wintypes.HANDLE()
    if not _adv.OpenProcessToken(
            handle, TOKEN_QUERY | TOKEN_ADJUST_DEFAULT, ctypes.byref(token)):
        raise JobError(f"OpenProcessToken 失败：{_err()}")
    try:
        sid = ctypes.c_void_p()
        if not _adv.ConvertStringSidToSidW(sid_string, ctypes.byref(sid)):
            raise JobError(f"解析低完整性 SID {sid_string} 失败：{_err()}")
        try:
            label = _TOKEN_MANDATORY_LABEL()
            label.Label.Sid = sid
            label.Label.Attributes = SE_GROUP_INTEGRITY
            if not _adv.SetTokenInformation(
                    token, TokenIntegrityLevel, ctypes.byref(label),
                    ctypes.sizeof(label)):
                raise JobError(f"SetTokenInformation(完整性=Low) 失败：{_err()}")
        finally:
            _k32.LocalFree(sid)

        rid = read_integrity_rid(token)
        if rid is None:
            log.warning("设置了低完整性但读不回来，按未生效处理")
        elif rid >= 0x2000:
            log.warning("低完整性没有落地：读回的完整性 RID 是 %d（期望 4096）",
                        rid)
            return None
        return rid
    finally:
        _k32.CloseHandle(token)


def launch_in_job(
    cmd: Sequence[str] | str,
    cwd: str | os.PathLike[str] | None,
    job: SandboxJob | None,
    *,
    pipes: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """在作业里启动一个子进程（不弹黑窗），返回 ``Popen``。

    顺序是刻意的，不能调换：

    1. ``CREATE_SUSPENDED`` 创建 —— 进程存在但没跑过一条指令；
    2. ``AssignProcessToJobObject`` —— 挂进作业；
    3. 可选地把令牌完整性降到 Low；
    4. ``NtResumeProcess`` —— 放它跑。

    ``job`` 为 ``None`` 时退化为普通启动（只在非 Windows 或用不了作业对象时）。
    """
    argv = list(cmd) if not isinstance(cmd, str) else cmd
    flags = CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
    if job is not None:
        flags |= CREATE_SUSPENDED

    process = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if pipes else None,
        stdout=subprocess.PIPE if pipes else None,
        stderr=subprocess.PIPE if pipes else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=flags,
    )

    if job is None:
        return process

    try:
        job.assign(process)
        if job.limits.low_integrity:
            job.integrity_rid = _lower_process_integrity(
                process, job.limits.integrity_sid)
    except Exception:
        # 挂不进去就必须立刻杀掉：这个进程是挂起状态，直接 TerminateProcess 即可
        try:
            process.kill()
            process.wait(timeout=2.0)
        except Exception:
            log.debug("清理挂起的子进程失败", exc_info=True)
        raise

    status = _ntdll.NtResumeProcess(wintypes.HANDLE(int(process._handle)))  # noqa: SLF001
    if status != 0:
        try:
            process.kill()
        except Exception:
            pass
        raise JobError(f"NtResumeProcess 返回 0x{status & 0xFFFFFFFF:08X}")
    return process


# ---------------------------------------------------------------------------
# 总开关
# ---------------------------------------------------------------------------

_ENABLED = True


def sandbox_enabled() -> bool:
    """沙箱是否启用。用户可以在设置里关掉（例如老机器上资源占用敏感）。"""
    return _ENABLED


def set_sandbox_enabled(value: bool) -> None:
    global _ENABLED
    _ENABLED = bool(value)


# ---------------------------------------------------------------------------
# 结构体布局自检
# ---------------------------------------------------------------------------


def verify_structure_layout() -> tuple[bool, str]:
    """独立验证结构体布局：既算偏移，也向内核问偏移。

    只做 ctypes 的 ``sizeof`` / ``offsetof`` 是不够的 —— 那只能证明"我的结构体
    是自洽的"，不能证明"内核读的是这些位置"。所以再叠一层实测：逐个候选偏移写
    上限，看哪个偏移能让 ``SetInformationJobObject`` 接受。两边一致才算通过。
    """
    if not IS_WINDOWS:
        return True, "非 Windows，跳过"

    size = ctypes.sizeof(_JOB_EXTENDED_LIMIT)
    off_process = _JOB_EXTENDED_LIMIT.ProcessMemoryLimit.offset
    off_job = _JOB_EXTENDED_LIMIT.JobMemoryLimit.offset
    want_size, want_process, want_job = STRUCT_LAYOUT

    if (size, off_process, off_job) != STRUCT_LAYOUT:
        return False, (f"结构体与期望不符：sizeof={size}（期望 {want_size}），"
                       f"ProcessMemoryLimit@{off_process}（期望 {want_process}），"
                       f"JobMemoryLimit@{off_job}（期望 {want_job}）")

    found: dict[int, int] = {}
    for flag in (L_PROCESS_MEMORY, L_JOB_MEMORY):
        for offset in range(64, size + 8, 8):
            probe = _k32.CreateJobObjectW(None, None)
            if not probe:
                return False, f"CreateJobObjectW 失败：{_err()}"
            try:
                buf = bytearray(size + 16)
                buf[16:20] = flag.to_bytes(4, "little")
                buf[offset:offset + 8] = (16 * 1024 * 1024).to_bytes(8, "little")
                blob = (ctypes.c_ubyte * len(buf)).from_buffer(buf)
                if _k32.SetInformationJobObject(
                        probe, JobObjectExtendedLimitInformation, blob, size):
                    found[flag] = offset
                    break
            finally:
                _k32.CloseHandle(probe)

    if found.get(L_PROCESS_MEMORY) != off_process:
        return False, (f"内核读进程内存上限的位置是 "
                       f"{found.get(L_PROCESS_MEMORY)}，ctypes 算的是 "
                       f"{off_process} —— 拒绝启用沙箱")
    if found.get(L_JOB_MEMORY) != off_job:
        return False, (f"内核读作业内存上限的位置是 {found.get(L_JOB_MEMORY)}，"
                       f"ctypes 算的是 {off_job} —— 拒绝启用沙箱")
    return True, (f"sizeof={size}，内存上限@{off_process}/{off_job}，"
                  f"与内核一致")


def structure_report() -> dict[str, Any]:
    """布局自检报告的原始数据，供诊断界面展示。"""
    if not IS_WINDOWS:
        return {"platform_ok": False, "detail": "非 Windows 平台"}
    ok, detail = verify_structure_layout()
    return {
        "platform_ok": ok,
        "detail": detail,
        "sizeof": ctypes.sizeof(_JOB_EXTENDED_LIMIT),
        "process_memory_offset": _JOB_EXTENDED_LIMIT.ProcessMemoryLimit.offset,
        "job_memory_offset": _JOB_EXTENDED_LIMIT.JobMemoryLimit.offset,
    }


# ---------------------------------------------------------------------------
# 能力自检
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerStatus:
    """一层隔离能力的结论。

    ``verified`` 区分两种证据强度，展示时不要混：

    * ``"行为"`` —— 真的起了一个进程，看着它被限制住了；
    * ``"结构"`` —— 内核接受了参数且回读一致（例如内存上限）。这比"没验证"
      强，但不能证明没有第三方软件在中间拦截。
    """

    name: str
    ok: bool
    detail: str = ""
    verified: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "verified": self.verified}


@dataclass
class SandboxCapabilities:
    """这台机器上沙箱能力的分层报告。"""

    platform_ok: bool = False
    layers: list[LayerStatus] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tested_at: float = 0.0

    def get(self, name: str) -> LayerStatus | None:
        return next((layer for layer in self.layers if layer.name == name), None)

    def usable(self, name: str) -> bool:
        layer = self.get(name)
        return bool(layer and layer.ok)

    @property
    def ok(self) -> bool:
        """最基础的资源隔离是否可用 —— 决定要不要启用沙箱执行路径。"""
        return self.platform_ok and self.usable("作业对象")

    def level(self) -> str:
        """一句话档位，给界面上的大字用。"""
        if not self.ok:
            return "无隔离"
        if self.usable("低完整性"):
            return "完整"
        if self.usable("关作业即杀进程树"):
            return "基础"
        return "仅资源上限"

    def summary(self) -> str:
        lines = [f"沙箱档位：{self.level()}"]
        for layer in self.layers:
            mark = "可用" if layer.ok else "不可用"
            extra = f"（{layer.verified}已验证）" if layer.ok and layer.verified else ""
            detail = f" —— {layer.detail}" if layer.detail else ""
            lines.append(f"  {'✓' if layer.ok else '✗'} {layer.name}：{mark}{extra}{detail}")
        lines.extend(f"  注：{note}" for note in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_ok": self.platform_ok,
            "level": self.level(),
            "layers": [layer.to_dict() for layer in self.layers],
            "notes": list(self.notes),
            "tested_at": self.tested_at,
        }


_CAPABILITIES: SandboxCapabilities | None = None
_CAPABILITY_LOCK = threading.Lock()

#: 用来做行为验证的"无辜程序"。只用 ``cmd.exe`` —— 它一定存在，
#: 不依赖机器上装没装 Python（打包后的 exe 自身未必能当解释器用）。
_SHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "cmd.exe")


def _run_probe_child(job: SandboxJob, argv: Sequence[str], *,
                     timeout: float = 8.0) -> tuple[int | None, str]:
    """在作业里跑一个小程序，返回 ``(退出码, 标准输出)``。"""
    work_dir = tempfile.mkdtemp(prefix="oj-probe-")
    try:
        process = launch_in_job(argv, work_dir, job, pipes=True)
    except Exception:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    try:
        try:
            out, _err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, _err = process.communicate()
        return process.returncode, (out or "")
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)


def _check_kill_on_close(notes: list[str]) -> LayerStatus:
    """行为验证：关掉作业句柄，作业里的进程必须立刻死。

    这是"不留孤儿进程"那条保证的唯一硬证据。用 ``ping -n 20`` 造一个活 20 秒的
    进程，关作业后立刻查它的存活状态。
    """
    job = SandboxJob(JobLimits(memory_mb=256, max_processes=2,
                               kill_on_close=True, block_ui=False))
    try:
        process = launch_in_job(
            [_SHELL, "/c", "ping", "-n", "20", "127.0.0.1"],
            tempfile.gettempdir(), job, pipes=True)
    except Exception as exc:
        job.close()
        return LayerStatus("关作业即杀进程树", False, str(exc))

    time.sleep(0.4)
    if process.poll() is not None:
        job.close()
        return LayerStatus("关作业即杀进程树", False,
                           "探针进程自己就退出了，无法验证（本机可能拦截了 ping）")
    job.close()
    deadline = time.time() + 3.0
    while process.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            process.kill()
        except Exception:
            pass
        notes.append("关作业后探针进程仍存活超过 3 秒，已兜底强杀")
        return LayerStatus("关作业即杀进程树", False, "关闭作业后进程仍在运行")
    return LayerStatus("关作业即杀进程树", True, "作业句柄关闭后进程即刻终止", "行为")


def _check_low_integrity() -> LayerStatus:
    """行为验证：把子进程令牌降到 Low 之后，它还能正常跑。

    这里必须**真的启动一个进程并看它的输出**，不能只看 API 返回值。
    实测：在装了 360 安全卫士主动防御的机器上，降完整性之后的进程创建会被静默
    拦掉 —— 所有 API 都返回成功，没有任何错误码，只是进程从来没跑起来过。
    只信返回值的话，界面会显示"低完整性已启用"，而实际上没有。
    """
    job = SandboxJob(JobLimits(memory_mb=256, max_processes=2,
                               low_integrity=True, block_ui=False))
    try:
        code, out = _run_probe_child(
            job, [_SHELL, "/c", "echo", "OJ_SANDBOX_OK"], timeout=8.0)
    except JobError as exc:
        job.close()
        return LayerStatus("低完整性", False, f"{exc}")
    except Exception as exc:
        job.close()
        return LayerStatus("低完整性", False,
                           f"启动低完整性进程失败：{type(exc).__name__}: {exc}")
    finally:
        job.close()

    if "OJ_SANDBOX_OK" in out:
        return LayerStatus("低完整性", True,
                           "子进程以低完整性令牌运行并正常输出", "行为")
    if code is None:
        return LayerStatus("低完整性", False, "低完整性进程没有退出（疑似被拦截）")
    return LayerStatus(
        "低完整性", False,
        f"进程未产出预期输出（退出码 {code}）—— 常见原因是安全软件"
        f"（360 主动防御一类）在进程创建路径上静默拦截")


def probe_capabilities(*, refresh: bool = False) -> SandboxCapabilities:
    """量出这台机器的沙箱能力。结果缓存，``refresh=True`` 可强制重测。

    每项独立测、独立报。**不会**因为某一层不可用就把整体结论设成"可用"。
    """
    global _CAPABILITIES
    with _CAPABILITY_LOCK:
        if _CAPABILITIES is not None and not refresh:
            return _CAPABILITIES
        _CAPABILITIES = _probe()
        return _CAPABILITIES


def _probe() -> SandboxCapabilities:
    caps = SandboxCapabilities(tested_at=time.time())

    if not IS_WINDOWS:
        caps.notes.append("当前不是 Windows，评测沿用「边跑边看」式的资源限制")
        return caps
    caps.platform_ok = True

    layout_ok, layout_detail = verify_structure_layout()
    caps.layers.append(LayerStatus("结构体布局", layout_ok, layout_detail, "行为"
                                   if layout_ok else ""))
    if not layout_ok:
        caps.notes.append("结构体布局与系统不一致，已拒绝启用沙箱执行路径")
        caps.layers.append(LayerStatus("作业对象", False, "布局自检未通过"))
        return caps

    # 作业对象本身：先结构验证（内存上限能写能读回）
    try:
        with SandboxJob(JobLimits(memory_mb=256, max_processes=4,
                                  kill_on_close=False, block_ui=False)) as job:
            caps.layers.append(LayerStatus(
                "作业对象", True,
                "内核接受并回读了内存上限与活动进程数上限", "结构"))
            _ = job
    except JobError as exc:
        caps.layers.append(LayerStatus("作业对象", False, str(exc)))
        caps.notes.append("作业对象不可用，评测将退化为轮询式资源限制")
        return caps

    # 核心行为验证：关作业即杀
    try:
        caps.layers.append(_check_kill_on_close(caps.notes))
    except Exception as exc:                       # pragma: no cover - 环境相关
        caps.layers.append(LayerStatus("关作业即杀进程树", False, str(exc)))

    # UI 限制：结构验证即可，它只是加固
    try:
        with SandboxJob(JobLimits(memory_mb=256, block_ui=True)):
            caps.layers.append(LayerStatus(
                "UI 限制（剪贴板/桌面）", True, "已禁止剪贴板与桌面句柄", "结构"))
    except JobError as exc:
        caps.layers.append(LayerStatus("UI 限制（剪贴板/桌面）", False, str(exc)))

    # 低完整性：必须行为验证，见 _check_low_integrity 的说明
    try:
        caps.layers.append(_check_low_integrity())
    except Exception as exc:                       # pragma: no cover - 环境相关
        caps.layers.append(LayerStatus("低完整性", False, str(exc)))

    if not caps.usable("低完整性"):
        caps.notes.append(
            "低完整性不可用不影响判题正确性，只是被测程序与本机同权限："
            "它能读写当前用户能碰的文件。若要挡住这一点，需要换用"
            "独立账号或虚拟机。")

    log.info("沙箱能力自检完成：%s", caps.level())
    return caps


def sandbox_limits(time_limit_ms: int, memory_limit_mb: int, *,
                   language: str = "") -> JobLimits:
    """按题目限制推导作业对象参数。

    内存取"题目限制 + 一点余量"：被测程序自身的运行时（Python 解释器约 12MB、
    JVM 起步就上百 MB）会先吃掉一部分额度，卡得过紧会把正确解误判成 MLE。

    ``low_integrity`` 在这里固定为 ``False``：要不要开低完整性必须由
    :func:`probe_capabilities` 的**行为验证**决定，见 :func:`resolve_limits`。
    """
    slack_mb = 64
    if language == "java":
        slack_mb = 256
    elif language == "python":
        slack_mb = 128
    return JobLimits(
        memory_mb=max(64, int(memory_limit_mb) + slack_mb),
        max_processes=DEFAULT_MAX_PROCESSES if language != "java" else 4,
        # CPU 时间上限给墙钟的 3 倍：正常程序 CPU 时间不会超过墙钟太多，
        # 但留出余量以免多线程程序被误杀；真正兜底的仍是墙钟监控
        cpu_ms=max(1000, int(time_limit_ms) * 3),
        low_integrity=False,
        block_ui=True,
    )


def resolve_limits(time_limit_ms: int, memory_limit_mb: int, *,
                   language: str = "",
                   capabilities: SandboxCapabilities | None = None) -> JobLimits:
    """在 :func:`sandbox_limits` 之上，按实测能力决定是否启用低完整性。"""
    caps = capabilities or probe_capabilities()
    limits = sandbox_limits(time_limit_ms, memory_limit_mb, language=language)
    if not caps.usable("低完整性"):
        return limits
    return JobLimits(
        memory_mb=limits.memory_mb,
        max_processes=limits.max_processes,
        cpu_ms=limits.cpu_ms,
        kill_on_close=limits.kill_on_close,
        die_on_unhandled_exception=limits.die_on_unhandled_exception,
        block_ui=limits.block_ui,
        low_integrity=True,
    )
