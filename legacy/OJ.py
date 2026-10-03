"""
离线OJ系统 - 测试版 1.2 (修复版)
===========================================
版本号: 1.2 (修复运行问题)
年份: 2025

修复内容：
1. 修复 ttk.PanedWindow weight 参数兼容性问题
2. 修复测试点控件越界访问问题
3. 修复 Tkinter 线程安全问题
4. 修复 status_var 初始化顺序
5. 修复 ttk.Spinbox 兼容性
6. 修复 compare_output 空行处理
7. 修复进程通信死锁风险
8. 修复编译器锁超时处理
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import json
import os
import subprocess
import threading
import time
import tempfile
import re
import shutil
import sys
import platform
import signal
import traceback
import zipfile
from datetime import datetime
import glob
from io import StringIO
from functools import wraps
import uuid

# 尝试导入 psutil，如果失败给出提示
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("警告: 未安装 psutil，内存监控功能将不可用。请运行: pip install psutil")


# ==================== 全局工具函数 ====================

def get_base_path():
    """获取资源文件的基础路径"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.abspath(".")


def resource_path(relative_path):
    """获取资源的绝对路径"""
    try:
        base_path = get_base_path()
        if hasattr(sys, '_MEIPASS'):
            base_path = sys._MEIPASS
        return os.path.join(base_path, relative_path)
    except Exception:
        return relative_path


def safe_remove(filepath):
    """安全删除文件"""
    try:
        if filepath and os.path.exists(filepath):
            os.unlink(filepath)
    except Exception:
        pass


def force_kill_process(pid):
    """强制终止进程及其子进程"""
    try:
        if platform.system() == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           startupinfo=startupinfo,
                           capture_output=True, timeout=2)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def silent_subprocess_run(cmd, **kwargs):
    """静默运行子进程，避免黑色窗口闪烁"""
    if platform.system() == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs['startupinfo'] = startupinfo

    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    kwargs.setdefault('timeout', 5)

    return subprocess.run(cmd, **kwargs)


# ==================== 路径验证器 ====================

class PathValidator:
    """路径安全验证器"""

    @staticmethod
    def is_safe_path(path):
        """检查路径是否安全"""
        if not path:
            return False

        dangerous_chars = [';', '|', '&', '$', '`', '>', '<', '\n', '\r']
        for char in dangerous_chars:
            if char in path:
                return False

        if '..' in path and not os.path.isabs(path):
            normalized = os.path.normpath(path)
            if normalized.startswith('..') or normalized == '..':
                return False

        return True

    @staticmethod
    def is_valid_compiler_path(path):
        """验证编译器路径"""
        if not path:
            return False, "路径为空"

        if not PathValidator.is_safe_path(path):
            return False, "路径包含危险字符"

        if not os.path.exists(path):
            return False, "路径不存在"

        if platform.system() == "Windows":
            if not path.lower().endswith(('.exe', '.bat', '.cmd')):
                return False, "不是有效的可执行文件"
        else:
            if not os.access(path, os.X_OK):
                return False, "文件不可执行"

        return True, "路径有效"

    @staticmethod
    def is_removable_drive(path):
        """检查路径是否在可移动设备上"""
        try:
            if platform.system() == "Windows":
                drive = os.path.splitdrive(path)[0]
                if drive:
                    import ctypes
                    drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
                    return drive_type == 2
        except Exception:
            pass
        return False


# ==================== 编译器锁管理器 ====================

class CompilerLock:
    """编译器锁"""
    _locks = {}
    _lock = threading.Lock()

    @classmethod
    def acquire(cls, compiler_path):
        with cls._lock:
            if compiler_path not in cls._locks:
                cls._locks[compiler_path] = threading.Lock()
            lock = cls._locks[compiler_path]

        acquired = lock.acquire(blocking=True, timeout=10)
        if not acquired:
            raise TimeoutError(f"获取编译器锁超时: {compiler_path}")
        return acquired

    @classmethod
    def release(cls, compiler_path):
        with cls._lock:
            if compiler_path in cls._locks:
                try:
                    cls._locks[compiler_path].release()
                except RuntimeError:
                    pass


# ==================== 进程监控器 ====================

class ProcessMonitor:
    """进程监控器"""

    def __init__(self, process, time_limit, memory_limit, language):
        self.process = process
        self.time_limit = time_limit / 1000.0
        self.memory_limit = memory_limit * 1024 * 1024
        self.language = language
        self.start_time = time.time()
        self.max_memory = 0
        self.timeout = False
        self.memory_exceeded = False
        self._stop_event = threading.Event()

    def monitor_and_wait(self, input_data=None):
        try:
            monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            monitor_thread.start()

            try:
                stdout, stderr = self.process.communicate(
                    input=input_data,
                    timeout=self.time_limit + 0.5
                )
                returncode = self.process.returncode
            except subprocess.TimeoutExpired:
                self.timeout = True
                force_kill_process(self.process.pid)
                try:
                    stdout, stderr = self.process.communicate(timeout=1)
                except Exception:
                    stdout, stderr = "", ""
                returncode = -1

            self._stop_event.set()
            monitor_thread.join(timeout=1)

            elapsed = time.time() - self.start_time
            memory_mb = self.max_memory / (1024 * 1024) if self.max_memory > 0 else 0

            if self.timeout:
                return {"status": "TLE", "output": stdout or "", "error": stderr or "",
                        "time": elapsed * 1000, "memory": memory_mb}
            elif self.memory_exceeded:
                return {"status": "MLE", "output": stdout or "", "error": stderr or "",
                        "time": elapsed * 1000, "memory": memory_mb}
            elif returncode != 0:
                return {"status": "RE", "output": stdout or "", "error": stderr or "",
                        "time": elapsed * 1000, "memory": memory_mb}
            else:
                return {"status": "AC", "output": stdout or "", "error": stderr or "",
                        "time": elapsed * 1000, "memory": memory_mb}

        except Exception as e:
            try:
                force_kill_process(self.process.pid)
            except Exception:
                pass
            return {"status": "RE", "output": "", "error": f"监控异常: {str(e)}",
                    "time": 0, "memory": 0}

    def _monitor(self):
        if not HAS_PSUTIL:
            return
        while not self._stop_event.is_set():
            try:
                if self.process.poll() is not None:
                    break

                elapsed = time.time() - self.start_time
                if elapsed > self.time_limit:
                    self.timeout = True
                    force_kill_process(self.process.pid)
                    break

                memory_usage = self._get_memory_usage()
                if memory_usage > self.max_memory:
                    self.max_memory = memory_usage

                if memory_usage > self.memory_limit:
                    self.memory_exceeded = True
                    force_kill_process(self.process.pid)
                    break

                time.sleep(0.01)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            except Exception:
                break

    def _get_memory_usage(self):
        try:
            if self.language == "java":
                return self._get_java_memory_usage()
            else:
                proc = psutil.Process(self.process.pid)
                return proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    def _get_java_memory_usage(self):
        try:
            total_memory = 0
            parent = psutil.Process(self.process.pid)
            children = parent.children(recursive=True)
            all_processes = [parent] + children
            for proc in all_processes:
                try:
                    total_memory += proc.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return total_memory
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0


# ==================== Java运行器 ====================

class JavaRunner:
    """Java代码运行器"""

    @staticmethod
    def extract_main_class(code):
        # 先移除注释
        code_no_block = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
        code_no_comment = re.sub(r'//[^\n]*', '', code_no_block)

        patterns = [
            r'public\s+class\s+(\w+)',
            r'class\s+(\w+)\s*\{[^}]*\bpublic\s+static\s+void\s+main\s*\(',
            r'class\s+(\w+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, code_no_comment, re.DOTALL)
            if match:
                return match.group(1)
        return "Main"

    @staticmethod
    def run_java_code(code, input_data, time_limit, memory_limit, javac_path, java_path):
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix="oj_java_")
            main_class = JavaRunner.extract_main_class(code)
            source_file = os.path.join(temp_dir, f"{main_class}.java")

            with open(source_file, 'w', encoding='utf-8') as f:
                f.write(code)

            CompilerLock.acquire(javac_path)
            try:
                compile_cmd = [javac_path, "-encoding", "UTF-8", source_file]
                compile_proc = silent_subprocess_run(compile_cmd, cwd=temp_dir, timeout=15)
                if compile_proc.returncode != 0:
                    return {
                        "status": "CE",
                        "output": "",
                        "error": f"编译错误:\n{(compile_proc.stderr or '')[:500]}",
                        "time": 0, "memory": 0
                    }
            finally:
                CompilerLock.release(javac_path)

            run_cmd = [java_path, f"-Xmx{memory_limit}m",
                       "-Dfile.encoding=UTF-8", "-cp", temp_dir, main_class]

            process = subprocess.Popen(
                run_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=temp_dir
            )

            monitor = ProcessMonitor(process, time_limit, memory_limit, "java")
            return monitor.monitor_and_wait(input_data)

        except subprocess.TimeoutExpired:
            return {"status": "TLE", "output": "", "error": "编译超时", "time": 0, "memory": 0}
        except Exception as e:
            return {"status": "RE", "output": "", "error": f"运行异常: {str(e)}", "time": 0, "memory": 0}
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass


# ==================== 编译器检测器 ====================

class CompilerDetector:

    @staticmethod
    def find_best_compiler(compiler_names, compiler_type):
        candidates = []

        if platform.system() == "Windows":
            candidates.extend(CompilerDetector._search_windows_paths(compiler_names, compiler_type))

        for name in compiler_names:
            try:
                if platform.system() == "Windows":
                    result = silent_subprocess_run(["where", name], timeout=3)
                else:
                    result = silent_subprocess_run(["which", name], timeout=3)

                if result.returncode == 0:
                    paths = [p.strip() for p in (result.stdout or "").strip().split('\n') if p.strip()]
                    for path in paths:
                        version_str = CompilerDetector._get_version_string(path, compiler_type)
                        version_num = CompilerDetector._parse_version(version_str, compiler_type)
                        if version_num:
                            candidates.append((path, version_str, version_num))
            except Exception:
                continue

        if not candidates:
            return None, None

        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates[0][0], candidates[0][1]

    @staticmethod
    def _search_windows_paths(compiler_names, compiler_type):
        candidates = []
        devcpp_paths = [
            r"C:\Program Files\Dev-Cpp\MinGW64\bin",
            r"C:\Program Files (x86)\Dev-Cpp\MinGW64\bin",
            r"C:\Dev-Cpp\MinGW64\bin",
        ]
        mingw_paths = [
            r"C:\mingw64\bin",
            r"C:\MinGW\bin",
            r"C:\msys64\mingw64\bin",
        ]
        search_paths = []
        if compiler_type in ["cpp", "c"]:
            search_paths.extend(devcpp_paths)
            search_paths.extend(mingw_paths)

        # Python 常见安装路径
        if compiler_type == "python":
            python_paths = [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python"),
                r"C:\Python312", r"C:\Python311", r"C:\Python310", r"C:\Python39",
                r"C:\Program Files\Python312", r"C:\Program Files\Python311",
            ]
            search_paths.extend(python_paths)

        for search_path in search_paths:
            if not os.path.exists(search_path):
                continue

            # 递归搜索一层子目录（比如 Python 版本号目录）
            dirs_to_search = [search_path]
            try:
                for sub in os.listdir(search_path):
                    sub_path = os.path.join(search_path, sub)
                    if os.path.isdir(sub_path):
                        dirs_to_search.append(sub_path)
            except Exception:
                pass

            for sp in dirs_to_search:
                for name in compiler_names:
                    if compiler_type == "cpp":
                        exe_names = ["g++.exe", "clang++.exe"]
                    elif compiler_type == "c":
                        exe_names = ["gcc.exe", "clang.exe"]
                    elif compiler_type == "python":
                        exe_names = ["python.exe"]
                    else:
                        exe_names = [f"{name}.exe"]

                    for exe_name in exe_names:
                        exe_path = os.path.join(sp, exe_name)
                        if os.path.exists(exe_path):
                            try:
                                version_str = CompilerDetector._get_version_string(exe_path, compiler_type)
                                version_num = CompilerDetector._parse_version(version_str, compiler_type)
                                if version_num:
                                    candidates.append((exe_path, version_str, version_num))
                            except Exception:
                                continue

        return candidates

    @staticmethod
    def _get_version_string(compiler_path, compiler_type):
        try:
            if compiler_type in ["cpp", "c"]:
                result = silent_subprocess_run([compiler_path, "--version"], timeout=3)
                out = (result.stdout or "")
                return out.split('\n')[0].strip() if out else ""
            elif compiler_type == "python":
                result = silent_subprocess_run([compiler_path, "--version"], timeout=3)
                return (result.stdout or "").strip() or (result.stderr or "").strip()
            elif compiler_type in ["javac", "java"]:
                result = silent_subprocess_run([compiler_path, "-version"], timeout=3)
                err = (result.stderr or "")
                return err.strip().split('\n')[0] if err else ""
        except Exception:
            return ""
        return ""

    @staticmethod
    def _parse_version(version_str, compiler_type):
        if not version_str:
            return None
        try:
            if compiler_type in ["cpp", "c"]:
                match = re.search(r'(\d+)\.(\d+)\.(\d+)', version_str)
                if match:
                    return [int(match.group(1)), int(match.group(2)), int(match.group(3))]
                match = re.search(r'(\d+)\.(\d+)', version_str)
                if match:
                    return [int(match.group(1)), int(match.group(2)), 0]
            elif compiler_type == "python":
                match = re.search(r'Python (\d+)\.(\d+)\.(\d+)', version_str)
                if match:
                    major = int(match.group(1))
                    if major == 3:
                        return [3, int(match.group(2)), int(match.group(3)), 1]
                    else:
                        return [major, int(match.group(2)), int(match.group(3)), 0]
                match = re.search(r'(\d+)\.(\d+)\.(\d+)', version_str)
                if match:
                    return [int(match.group(1)), int(match.group(2)), int(match.group(3)), 0]
            elif compiler_type in ["javac", "java"]:
                match = re.search(r'version "?(\d+)(?:\.(\d+))?(?:\.(\d+))?', version_str)
                if match:
                    return [int(match.group(1) or 0),
                            int(match.group(2) or 0),
                            int(match.group(3) or 0)]
        except Exception:
            pass
        return None


# ==================== 批量导入管理器 ====================

class BatchImportManager:

    def __init__(self, parent_app):
        self.parent = parent_app
        self.report_buffer = StringIO()
        self.imported_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.resources_copied = 0

    def import_from_zip(self, zip_filename, conflict_strategy="skip"):
        self._reset_counters()
        self._log("开始从ZIP文件导入...")
        try:
            with zipfile.ZipFile(zip_filename, 'r') as zipf:
                problem_files = [f for f in zipf.namelist()
                                 if f.startswith("problems/") and f.endswith(".json")]
                if not problem_files:
                    self._log("错误: ZIP文件中未找到题目文件")
                    return False

                self._log(f"找到 {len(problem_files)} 个题目文件")
                for i, problem_file in enumerate(problem_files):
                    self.parent.root.after(0, lambda n=i: self.parent.status_var.set(
                        f"导入进度: {n+1}/{len(problem_files)}"))
                    try:
                        with zipf.open(problem_file, 'r') as f:
                            problem_data = json.loads(f.read().decode('utf-8'))
                        if not self._validate_problem_data(problem_data, problem_file):
                            self.failed_count += 1
                            continue
                        problem_id = problem_data["id"]
                        new_problem_id = self._handle_id_conflict(problem_id, conflict_strategy)
                        if new_problem_id is None:
                            self.skipped_count += 1
                            continue
                        if new_problem_id != problem_id:
                            problem_data["id"] = new_problem_id
                            self._log(f"  重命名: {problem_id} -> {new_problem_id}")
                        self._import_resources_from_zip(zipf, problem_data)
                        self.parent.problems[new_problem_id] = problem_data
                        self.imported_count += 1
                        self._log(f"  成功导入: {new_problem_id}")
                    except Exception as e:
                        self.failed_count += 1
                        self._log(f"  导入失败 [{problem_file}]: {str(e)}")
                self._finalize_import()
                return True
        except Exception as e:
            self._log(f"ZIP文件处理失败: {str(e)}")
            return False

    def import_from_folder(self, folder_path, conflict_strategy="skip"):
        self._reset_counters()
        self._log("开始从文件夹导入...")
        try:
            problems_dir = os.path.join(folder_path, "problems")
            if os.path.exists(problems_dir):
                problem_files = glob.glob(os.path.join(problems_dir, "*.json"))
            else:
                problem_files = glob.glob(os.path.join(folder_path, "*.json"))

            if not problem_files:
                self._log("错误: 文件夹中未找到题目文件")
                return False

            self._log(f"找到 {len(problem_files)} 个题目文件")
            for i, problem_file in enumerate(problem_files):
                self.parent.root.after(0, lambda n=i: self.parent.status_var.set(
                    f"导入进度: {n+1}/{len(problem_files)}"))
                try:
                    with open(problem_file, 'r', encoding='utf-8') as f:
                        problem_data = json.load(f)
                    if not self._validate_problem_data(problem_data, os.path.basename(problem_file)):
                        self.failed_count += 1
                        continue
                    problem_id = problem_data["id"]
                    new_problem_id = self._handle_id_conflict(problem_id, conflict_strategy)
                    if new_problem_id is None:
                        self.skipped_count += 1
                        continue
                    if new_problem_id != problem_id:
                        problem_data["id"] = new_problem_id
                        self._log(f"  重命名: {problem_id} -> {new_problem_id}")
                    self._import_resources_from_folder(folder_path, problem_data)
                    self.parent.problems[new_problem_id] = problem_data
                    self.imported_count += 1
                    self._log(f"  成功导入: {new_problem_id}")
                except Exception as e:
                    self.failed_count += 1
                    self._log(f"  导入失败 [{os.path.basename(problem_file)}]: {str(e)}")
            self._finalize_import()
            return True
        except Exception as e:
            self._log(f"文件夹处理失败: {str(e)}")
            return False

    def _validate_problem_data(self, problem_data, source_name):
        try:
            required_fields = ["id", "title", "description", "time_limit", "memory_limit", "testcases"]
            for field in required_fields:
                if field not in problem_data:
                    self._log(f"  验证失败 [{source_name}]: 缺少必需字段 '{field}'")
                    return False
            problem_id = problem_data["id"]
            if not problem_id or not isinstance(problem_id, str):
                self._log(f"  验证失败 [{source_name}]: 题目ID无效")
                return False
            testcases = problem_data.get("testcases", [])
            if not isinstance(testcases, list):
                self._log(f"  验证失败 [{source_name}]: 测试用例格式错误")
                return False
            for i, testcase in enumerate(testcases):
                if not isinstance(testcase, dict) or "input" not in testcase or "output" not in testcase:
                    self._log(f"  验证失败 [{source_name}]: 测试点 {i+1} 格式错误")
                    return False
            if not isinstance(problem_data["time_limit"], (int, float)) or problem_data["time_limit"] <= 0:
                self._log(f"  验证失败 [{source_name}]: 时间限制必须为正数")
                return False
            if not isinstance(problem_data["memory_limit"], (int, float)) or problem_data["memory_limit"] <= 0:
                self._log(f"  验证失败 [{source_name}]: 内存限制必须为正数")
                return False
            return True
        except Exception as e:
            self._log(f"  验证失败 [{source_name}]: 异常 {str(e)}")
            return False

    def _handle_id_conflict(self, problem_id, conflict_strategy):
        if problem_id not in self.parent.problems:
            return problem_id
        if conflict_strategy == "skip":
            self._log(f"  跳过: {problem_id} (已存在)")
            return None
        elif conflict_strategy == "overwrite":
            self._log(f"  覆盖: {problem_id}")
            return problem_id
        elif conflict_strategy == "rename":
            base_id = problem_id
            counter = 1
            while f"{base_id}_{counter}" in self.parent.problems:
                counter += 1
            new_id = f"{base_id}_{counter}"
            self._log(f"  重命名为: {new_id}")
            return new_id
        return problem_id

    def _import_resources_from_zip(self, zipf, problem_data):
        description = problem_data.get("description", "")
        image_matches = re.findall(r'!\[.*?\]\((.*?)\)', description)
        for image_ref in image_matches:
            if not image_ref.startswith('http'):
                possible_paths = [
                    f"resources/{image_ref}",
                    f"resource/{image_ref}",
                    f"images/{image_ref}",
                    image_ref
                ]
                for zip_path in possible_paths:
                    if zip_path in zipf.namelist():
                        self._copy_zip_resource(zipf, zip_path, image_ref)
                        break

    def _import_resources_from_folder(self, folder_path, problem_data):
        description = problem_data.get("description", "")
        image_matches = re.findall(r'!\[.*?\]\((.*?)\)', description)
        for image_ref in image_matches:
            if not image_ref.startswith('http'):
                possible_paths = [
                    os.path.join(folder_path, "resources", image_ref),
                    os.path.join(folder_path, "resource", image_ref),
                    os.path.join(folder_path, "images", image_ref),
                    os.path.join(folder_path, image_ref)
                ]
                for source_path in possible_paths:
                    if os.path.exists(source_path):
                        self._copy_file_resource(source_path, image_ref)
                        break

    def _copy_zip_resource(self, zipf, zip_path, image_ref):
        try:
            resources_dir = resource_path("problem_resources")
            os.makedirs(resources_dir, exist_ok=True)
            dest_path = os.path.join(resources_dir, image_ref)
            os.makedirs(os.path.dirname(dest_path) or resources_dir, exist_ok=True)
            with zipf.open(zip_path, 'r') as src, open(dest_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            self.resources_copied += 1
            self._log(f"    复制资源: {image_ref}")
        except Exception as e:
            self._log(f"    资源复制失败 [{image_ref}]: {str(e)}")

    def _copy_file_resource(self, source_path, image_ref):
        try:
            resources_dir = resource_path("problem_resources")
            os.makedirs(resources_dir, exist_ok=True)
            dest_path = os.path.join(resources_dir, image_ref)
            os.makedirs(os.path.dirname(dest_path) or resources_dir, exist_ok=True)
            shutil.copy2(source_path, dest_path)
            self.resources_copied += 1
            self._log(f"    复制资源: {image_ref}")
        except Exception as e:
            self._log(f"    资源复制失败 [{image_ref}]: {str(e)}")

    def _reset_counters(self):
        self.report_buffer = StringIO()
        self.imported_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.resources_copied = 0

    def _log(self, message):
        self.report_buffer.write(message + "\n")
        print(message)

    def _finalize_import(self):
        self._log("\n" + "=" * 50)
        self._log("批量导入完成")
        self._log(f"成功导入: {self.imported_count} 个题目")
        self._log(f"跳过: {self.skipped_count} 个（已存在）")
        self._log(f"失败: {self.failed_count} 个")
        self._log(f"复制资源文件: {self.resources_copied} 个")
        self._log("=" * 50)

    def get_report(self):
        return self.report_buffer.getvalue()


# ==================== 异步任务装饰器 ====================

def async_task(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
        thread.start()
        return thread
    return wrapper


# ==================== 主应用程序 ====================

class OfflineOJSystem:

    def __init__(self, root):
        self.root = root
        self.root.title("离线OJ系统 - 测试版 1.2 (2025)")
        self.root.geometry("1400x800")

        # 【修复】提前创建 status_var，避免后面使用时未定义
        self.status_var = tk.StringVar(value="就绪 - 离线OJ系统 测试版 1.2 (2025)")

        # 题目存储
        self.problems = {}
        self.current_problem_id = None

        # 编译器路径
        self.compiler_paths = {
            "cpp": {"path": "", "works": False, "version": ""},
            "c": {"path": "", "works": False, "version": ""},
            "python": {"path": "", "works": False, "version": ""},
            "javac": {"path": "", "works": False, "version": ""},
            "java": {"path": "", "works": False, "version": ""},
        }

        # 设置
        self.security_mode = tk.BooleanVar(value=True)
        self.file_mode = tk.BooleanVar(value=False)
        self.enable_markdown_render = tk.BooleanVar(value=True)
        self.o2_optimization = tk.BooleanVar(value=True)

        self.judging = False
        self.import_manager = BatchImportManager(self)

        # 【修复】保存测试点控件引用，避免用 winfo_children 索引出错
        self.testcase_widgets = []

        # 设置图标
        self.set_icon()

        # UI
        self.setup_ui()

        # 自动检测编译器（此时 status_var 已存在）
        self.root.after(100, self.auto_detect_and_set)

        # 加载题目
        self.load_problems()

        # 欢迎提示
        self.show_welcome_hints()

    def set_icon(self):
        try:
            icon_path = resource_path("oj_icon.ico")
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except Exception:
            pass

    def show_welcome_hints(self):
        self.root.after(1500, self._show_welcome_dialog)

    def _show_welcome_dialog(self):
        if os.path.exists(resource_path("welcome_shown.txt")):
            return
        welcome_text = """欢迎使用离线OJ系统 - 测试版 1.2！

【快速上手指南】
1. 配置编译器 → 进入"编译器配置"选项卡
2. 创建题目 → 进入"存题模块"选项卡
3. 练习编程 → 进入"写题模块"选项卡

【注意事项】
- 首次使用请先配置编译器
- 批量操作前建议备份数据
- 遇到问题可查看"帮助与关于"
"""
        dialog = tk.Toplevel(self.root)
        dialog.title("欢迎使用离线OJ系统")
        dialog.geometry("600x450")
        dialog.transient(self.root)

        ttk.Label(dialog, text="欢迎使用离线OJ系统",
                  font=('Arial', 14, 'bold')).pack(pady=10)

        text_widget = scrolledtext.ScrolledText(dialog, wrap=tk.WORD, font=('Arial', 10))
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        text_widget.insert(1.0, welcome_text)
        text_widget.config(state=tk.DISABLED)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="开始使用", command=dialog.destroy).pack(padx=5)

        try:
            with open(resource_path("welcome_shown.txt"), 'w') as f:
                f.write("1")
        except Exception:
            pass

    # ==================== 编译器检测 ====================

    def auto_detect_and_set(self):
        self.status_var.set("正在自动检测编译器...")
        self.root.update_idletasks()

        def detect():
            try:
                # C++
                path, version = CompilerDetector.find_best_compiler(
                    ["g++", "g++-12", "g++-11", "clang++", "c++"], "cpp")
                if path and self.test_compiler_works(path, "cpp"):
                    self.compiler_paths["cpp"]["path"] = path
                    self.compiler_paths["cpp"]["version"] = version
                    self.compiler_paths["cpp"]["works"] = True
                    if hasattr(self, 'cpp_path_var'):
                        self.root.after(0, lambda: self.cpp_path_var.set(path))

                # C
                path, version = CompilerDetector.find_best_compiler(
                    ["gcc", "gcc-12", "gcc-11", "clang", "cc"], "c")
                if path and self.test_compiler_works(path, "c"):
                    self.compiler_paths["c"]["path"] = path
                    self.compiler_paths["c"]["version"] = version
                    self.compiler_paths["c"]["works"] = True
                    if hasattr(self, 'c_path_var'):
                        self.root.after(0, lambda: self.c_path_var.set(path))

                # Python
                path, version = CompilerDetector.find_best_compiler(
                    ["python", "python3", "python3.12", "python3.11", "python3.10"], "python")
                if path and self.test_compiler_works(path, "python"):
                    self.compiler_paths["python"]["path"] = path
                    self.compiler_paths["python"]["version"] = version
                    self.compiler_paths["python"]["works"] = True
                    if hasattr(self, 'python_path_var'):
                        self.root.after(0, lambda: self.python_path_var.set(path))

                # Java
                path, version = CompilerDetector.find_best_compiler(["javac"], "javac")
                if path and self.test_compiler_works(path, "javac"):
                    self.compiler_paths["javac"]["path"] = path
                    self.compiler_paths["javac"]["version"] = version
                    self.compiler_paths["javac"]["works"] = True
                    if hasattr(self, 'javac_path_var'):
                        self.root.after(0, lambda: self.javac_path_var.set(path))

                path, version = CompilerDetector.find_best_compiler(["java"], "java")
                if path and self.test_compiler_works(path, "java"):
                    self.compiler_paths["java"]["path"] = path
                    self.compiler_paths["java"]["version"] = version
                    self.compiler_paths["java"]["works"] = True
                    if hasattr(self, 'java_path_var'):
                        self.root.after(0, lambda: self.java_path_var.set(path))

                self.root.after(0, lambda: self.status_var.set("编译器路径已自动检测"))
            except Exception as e:
                print(f"自动检测编译器出错: {e}")

        threading.Thread(target=detect, daemon=True).start()

    def test_compiler_works(self, compiler_path, compiler_type):
        try:
            if compiler_type in ["cpp", "c"]:
                suffix = '.cpp' if compiler_type == "cpp" else '.c'
                with tempfile.NamedTemporaryFile(mode='w', suffix=suffix,
                                                 delete=False, encoding='utf-8') as f:
                    f.write("int main() { return 0; }")
                    test_file = f.name
                out_file = test_file + ".out"
                try:
                    compile_cmd = [compiler_path, test_file, "-o", out_file]
                    result = silent_subprocess_run(compile_cmd, timeout=5)
                    return result.returncode == 0
                finally:
                    safe_remove(test_file)
                    safe_remove(out_file)

            elif compiler_type == "python":
                result = silent_subprocess_run([compiler_path, "-c", "print(1)"], timeout=5)
                return result.returncode == 0

            elif compiler_type == "javac":
                with tempfile.NamedTemporaryFile(mode='w', suffix='.java',
                                                 delete=False, encoding='utf-8') as f:
                    f.write("public class Test { public static void main(String[] a) {} }")
                    test_file = f.name
                try:
                    result = silent_subprocess_run([compiler_path, test_file], timeout=8)
                    return result.returncode == 0
                finally:
                    safe_remove(test_file)
                    safe_remove(test_file.replace('.java', '.class'))
                    # 也清理带数字后缀的 class
                    base = test_file[:-5]
                    for f_ in glob.glob(base + "*.class"):
                        safe_remove(f_)

            elif compiler_type == "java":
                result = silent_subprocess_run([compiler_path, "-version"], timeout=5)
                return result.returncode == 0
        except Exception:
            return False
        return False

    # ==================== UI 设置 ====================

    def setup_ui(self):
        # Notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

        # 状态栏（先 pack 到底部，保证显示）
        self.setup_status_bar()

        # 各个 Tab
        self.setup_problem_storage_tab()
        self.setup_problem_solving_tab()
        self.setup_compiler_config_tab()
        self.setup_advanced_settings_tab()
        self.setup_help_tab()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_status_bar(self):
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=3)
        status_label = ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN)
        status_label.pack(fill=tk.X, padx=2, pady=2)

    # ---------- 存题模块 ----------

    def setup_problem_storage_tab(self):
        storage_frame = ttk.Frame(self.notebook)
        self.notebook.add(storage_frame, text="存题模块")

        main_paned = ttk.PanedWindow(storage_frame, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧
        left_frame = ttk.LabelFrame(main_paned, text="题目列表")
        main_paned.add(left_frame, weight=1)

        steps_frame = ttk.LabelFrame(left_frame, text="操作步骤")
        steps_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(steps_frame, text="1. 搜索或选择题目\n2. 点击\"新建题目\"创建\n"
                                    "3. 填写题目信息\n4. 设置测试点\n5. 点击\"保存题目\"",
                  justify=tk.LEFT).pack(padx=5, pady=5)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT)
        self.search_storage_var = tk.StringVar()
        self.search_storage_var.trace_add('write', self.search_storage_problems)
        ttk.Entry(search_frame, textvariable=self.search_storage_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.problem_listbox = tk.Listbox(list_frame, font=('Arial', 11))
        self.problem_listbox.pack(fill=tk.BOTH, expand=True)
        self.problem_listbox.bind('<<ListboxSelect>>', self.on_problem_select)

        stats_frame = ttk.Frame(left_frame)
        stats_frame.pack(fill=tk.X, padx=10, pady=5)
        self.problem_stats_var = tk.StringVar(value="题目总数: 0")
        ttk.Label(stats_frame, textvariable=self.problem_stats_var).pack()

        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="新建题目",
                   command=self.new_problem_with_hint).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(btn_frame, text="删除题目",
                   command=self.delete_problem).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(btn_frame, text="随机跳题",
                   command=self.random_problem).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        batch_btn_frame = ttk.Frame(left_frame)
        batch_btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(batch_btn_frame, text="批量导出",
                   command=self.batch_export_problems).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(batch_btn_frame, text="批量导入",
                   command=self.batch_import_problems).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        # 右侧
        right_frame = ttk.LabelFrame(main_paned, text="题目编辑器")
        main_paned.add(right_frame, weight=3)

        info_frame = ttk.LabelFrame(right_frame, text="基本信息")
        info_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(info_frame, text="题目ID:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.problem_id_var = tk.StringVar()
        ttk.Entry(info_frame, textvariable=self.problem_id_var, width=20).grid(
            row=0, column=1, sticky=tk.W, padx=5, pady=5)
        ttk.Label(info_frame, text="题目名称:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)
        self.problem_title_var = tk.StringVar()
        ttk.Entry(info_frame, textvariable=self.problem_title_var, width=40).grid(
            row=0, column=3, sticky=tk.W, padx=5, pady=5)

        editor_frame = ttk.LabelFrame(right_frame, text="题目描述 (Markdown)")
        editor_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        toolbar = ttk.Frame(editor_frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(toolbar, text="添加图片", command=self.add_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="添加代码块", command=self.add_code_block).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="添加数学公式", command=self.add_math_formula).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="预览题目", command=self.preview_problem_enhanced).pack(side=tk.LEFT, padx=2)
        self.markdown_editor = scrolledtext.ScrolledText(editor_frame, font=('Consolas', 11), wrap=tk.WORD)
        self.markdown_editor.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        testcase_frame = ttk.LabelFrame(right_frame, text="测试点配置")
        testcase_frame.pack(fill=tk.X, padx=10, pady=10)

        limits_frame = ttk.Frame(testcase_frame)
        limits_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(limits_frame, text="时间限制(ms):").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.time_limit_var = tk.IntVar(value=1000)
        ttk.Entry(limits_frame, textvariable=self.time_limit_var, width=10).grid(
            row=0, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Label(limits_frame, text="内存限制(MB):").grid(row=0, column=2, sticky=tk.W, padx=5, pady=2)
        self.memory_limit_var = tk.IntVar(value=256)
        ttk.Entry(limits_frame, textvariable=self.memory_limit_var, width=10).grid(
            row=0, column=3, sticky=tk.W, padx=5, pady=2)
        ttk.Label(limits_frame, text="测试点数量:").grid(row=0, column=4, sticky=tk.W, padx=5, pady=2)
        self.testcase_count_var = tk.IntVar(value=1)
        # 【兼容性】ttk.Spinbox 兼容
        try:
            spinbox = ttk.Spinbox(limits_frame, from_=1, to=50,
                                  textvariable=self.testcase_count_var,
                                  command=self.update_testcases, width=10)
        except AttributeError:
            spinbox = tk.Spinbox(limits_frame, from_=1, to=50,
                                 textvariable=self.testcase_count_var,
                                 command=self.update_testcases, width=10)
        spinbox.grid(row=0, column=5, sticky=tk.W, padx=5, pady=2)

        self.testcase_container = ttk.Frame(testcase_frame)
        self.testcase_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.testcase_canvas = tk.Canvas(self.testcase_container, height=200)
        scrollbar = ttk.Scrollbar(self.testcase_container, orient=tk.VERTICAL,
                                  command=self.testcase_canvas.yview)
        self.testcase_scrollable_frame = ttk.Frame(self.testcase_canvas)
        self.testcase_scrollable_frame.bind(
            "<Configure>",
            lambda e: self.testcase_canvas.configure(scrollregion=self.testcase_canvas.bbox("all"))
        )
        self.testcase_canvas.create_window((0, 0), window=self.testcase_scrollable_frame, anchor="nw")
        self.testcase_canvas.configure(yscrollcommand=scrollbar.set)
        self.testcase_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        btn_frame2 = ttk.Frame(right_frame)
        btn_frame2.pack(fill=tk.X, padx=10, pady=10)
        self.save_btn = ttk.Button(btn_frame2, text="保存题目", command=self.save_problem_with_hint)
        self.save_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="重置", command=self.new_problem).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="导出题目", command=self.export_problem).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="导入题目", command=self.import_problem).pack(side=tk.LEFT, padx=5)

        self.update_testcases()

    # ---------- 写题模块 ----------

    def setup_problem_solving_tab(self):
        solving_frame = ttk.Frame(self.notebook)
        self.notebook.add(solving_frame, text="写题模块")

        main_paned = ttk.PanedWindow(solving_frame, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_frame = ttk.LabelFrame(main_paned, text="题目列表")
        main_paned.add(left_frame, weight=1)

        steps_frame = ttk.LabelFrame(left_frame, text="操作步骤")
        steps_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(steps_frame, text="1. 搜索或选择题目\n2. 查看题目描述\n"
                                    "3. 选择编程语言\n4. 编写代码\n5. 点击\"提交代码\"",
                  justify=tk.LEFT).pack(padx=5, pady=5)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add('w', self.search_problems)
        ttk.Entry(search_frame, textvariable=self.search_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.solving_problem_listbox = tk.Listbox(list_frame, font=('Arial', 11))
        self.solving_problem_listbox.pack(fill=tk.BOTH, expand=True)
        self.solving_problem_listbox.bind('<<ListboxSelect>>', self.on_solving_problem_select)

        right_paned = ttk.PanedWindow(main_paned, orient=tk.VERTICAL)
        main_paned.add(right_paned, weight=3)

        problem_display_frame = ttk.LabelFrame(right_paned, text="题目内容")
        right_paned.add(problem_display_frame, weight=1)

        self.problem_display = scrolledtext.ScrolledText(problem_display_frame, wrap=tk.WORD,
                                                         font=('Arial', 11))
        self.problem_display.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.problem_display.config(state=tk.DISABLED)

        code_frame = ttk.LabelFrame(right_paned, text="代码编辑器")
        right_paned.add(code_frame, weight=1)

        config_frame = ttk.Frame(code_frame)
        config_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(config_frame, text="编程语言:").pack(side=tk.LEFT)
        self.language_var = tk.StringVar(value="cpp")
        ttk.Combobox(config_frame, textvariable=self.language_var,
                     values=["cpp", "c", "python", "java"], state="readonly",
                     width=10).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(config_frame, text="O2优化(C/C++)",
                        variable=self.o2_optimization).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(config_frame, text="安全检测",
                        variable=self.security_mode).pack(side=tk.LEFT, padx=10)

        self.code_editor = scrolledtext.ScrolledText(code_frame, font=('Consolas', 11))
        self.code_editor.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        result_frame = ttk.LabelFrame(code_frame, text="判题结果")
        result_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.result_text = scrolledtext.ScrolledText(result_frame, height=8, font=('Consolas', 10))
        self.result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.result_text.config(state=tk.DISABLED)

        btn_frame = ttk.Frame(code_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        self.submit_btn = ttk.Button(btn_frame, text="提交代码", command=self.submit_code_with_hint)
        self.submit_btn.pack(side=tk.LEFT, padx=5)
        self.test_btn = ttk.Button(btn_frame, text="测试运行", command=self.test_run)
        self.test_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="清空代码", command=self.clear_code).pack(side=tk.LEFT, padx=5)

    # ---------- 编译器配置 ----------

    def setup_compiler_config_tab(self):
        config_frame = ttk.Frame(self.notebook)
        self.notebook.add(config_frame, text="编译器配置")

        # 用 Canvas + Scrollbar 支持滚动
        canvas = tk.Canvas(config_frame)
        scrollbar = ttk.Scrollbar(config_frame, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(inner, text="编译器路径配置 (自动选择最新版本)",
                  font=('Arial', 14, 'bold')).pack(pady=20)

        def make_row(parent, title, key):
            frame = ttk.LabelFrame(parent, text=title)
            frame.pack(fill=tk.X, padx=10, pady=10)
            ttk.Label(frame, text="路径:").grid(row=0, column=0, sticky=tk.W, padx=10, pady=10)
            var = tk.StringVar(value=self.compiler_paths[key]["path"])
            ttk.Entry(frame, textvariable=var, width=60).grid(row=0, column=1, padx=10, pady=10)
            ttk.Button(frame, text="浏览",
                       command=lambda k=key, v=var: self.browse_compiler(k, v)).grid(
                row=0, column=2, padx=10, pady=10)
            return var

        self.cpp_path_var = make_row(inner, "C++编译器 (g++/clang++)", "cpp")
        self.c_path_var = make_row(inner, "C编译器 (gcc/clang)", "c")
        self.python_path_var = make_row(inner, "Python解释器", "python")
        self.javac_path_var = make_row(inner, "Java编译器 (javac)", "javac")
        self.java_path_var = make_row(inner, "Java运行时 (java)", "java")

        test_frame = ttk.Frame(inner)
        test_frame.pack(fill=tk.X, padx=10, pady=20)
        ttk.Button(test_frame, text="测试所有编译器",
                   command=self.test_compilers).pack(side=tk.LEFT, padx=5)
        ttk.Button(test_frame, text="自动检测",
                   command=self.auto_detect_and_set).pack(side=tk.LEFT, padx=5)
        ttk.Button(inner, text="保存配置",
                   command=self.save_compiler_config).pack(pady=20)

    # ---------- 高级设置 ----------

    def setup_advanced_settings_tab(self):
        advanced_frame = ttk.Frame(self.notebook)
        self.notebook.add(advanced_frame, text="高级设置")

        ttk.Label(advanced_frame, text="高级功能设置",
                  font=('Arial', 14, 'bold')).pack(pady=20)

        settings_container = ttk.Frame(advanced_frame)
        settings_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        file_mode_frame = ttk.LabelFrame(settings_container, text="文件模式设置")
        file_mode_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Checkbutton(file_mode_frame, text="启用文件模式 (支持.in/.out文件操作)",
                        variable=self.file_mode).pack(padx=10, pady=10, anchor=tk.W)
        ttk.Label(file_mode_frame, text=(
            "文件模式说明：\n"
            "1. 启用后，系统将尝试检测代码中的文件操作\n"
            "2. 支持 C/C++: fopen, freopen, ifstream, ofstream\n"
            "3. 支持 Python: open() 打开 .in/.out 文件\n"
            "4. 支持 Java: FileInputStream, FileOutputStream"
        ), justify=tk.LEFT).pack(padx=10, pady=5, fill=tk.X)

        markdown_frame = ttk.LabelFrame(settings_container, text="Markdown渲染设置")
        markdown_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Checkbutton(markdown_frame, text="启用Markdown渲染",
                        variable=self.enable_markdown_render).pack(padx=10, pady=10, anchor=tk.W)

        ttk.Button(advanced_frame, text="保存设置",
                   command=self.save_advanced_settings).pack(pady=20)

    # ---------- 帮助 ----------

    def setup_help_tab(self):
        help_frame = ttk.Frame(self.notebook)
        self.notebook.add(help_frame, text="帮助与关于")

        help_text = scrolledtext.ScrolledText(help_frame, wrap=tk.WORD, font=('Arial', 11))
        help_text.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        help_content = """离线OJ系统 - 测试版 1.2 (2025)

【版本 1.2 修复内容】
1. 修复了程序无法启动的问题
2. 修复了测试点控件索引越界的问题
3. 修复了 Tkinter 线程安全问题
4. 修复了编译器锁超时处理
5. 改进了空行比较逻辑
6. 优化了编译器自动检测

【主要功能】
1. 存题模块：新建/编辑/删除题目，Markdown 描述，测试点配置
2. 写题模块：C/C++/Python/Java 判题，安全检测，O2优化
3. 编译器配置：自动检测最新版本编译器
4. 高级设置：文件模式、Markdown 渲染

【判题结果】
- AC: Accepted（答案正确）
- WA: Wrong Answer（答案错误）
- TLE: Time Limit Exceeded（时间超限）
- MLE: Memory Limit Exceeded（内存超限）
- RE: Runtime Error（运行时错误）
- CE: Compilation Error（编译错误）

【使用步骤】
1. 配置编译器 → 进入"编译器配置"选项卡
2. 创建题目 → 进入"存题模块"选项卡
3. 练习编程 → 进入"写题模块"选项卡

感谢您的使用！
"""
        help_text.insert(1.0, help_content)
        help_text.config(state=tk.DISABLED)

    # ==================== 高级设置 ====================

    def save_advanced_settings(self):
        self.status_var.set("高级设置已应用")
        messagebox.showinfo("成功", "高级设置已保存并应用")

    # ==================== 步骤提示 ====================

    def new_problem_with_hint(self):
        self.new_problem()

    def save_problem_with_hint(self):
        self.save_problem()

    def submit_code_with_hint(self):
        self.submit_code()

    # ==================== 测试点管理 ====================

    def update_testcases(self):
        """更新测试点输入输出框"""
        # 清空
        for widget in self.testcase_scrollable_frame.winfo_children():
            widget.destroy()
        self.testcase_widgets = []

        testcase_count = self.testcase_count_var.get()

        for i in range(testcase_count):
            case_frame = ttk.LabelFrame(self.testcase_scrollable_frame, text=f"测试点 {i+1}")
            case_frame.pack(fill=tk.X, padx=5, pady=5)

            input_frame = ttk.Frame(case_frame)
            input_frame.pack(fill=tk.X, padx=5, pady=2)
            ttk.Label(input_frame, text="输入:").pack(side=tk.LEFT)
            input_entry = scrolledtext.ScrolledText(input_frame, height=3, width=60)
            input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

            output_frame = ttk.Frame(case_frame)
            output_frame.pack(fill=tk.X, padx=5, pady=2)
            ttk.Label(output_frame, text="输出:").pack(side=tk.LEFT)
            output_entry = scrolledtext.ScrolledText(output_frame, height=3, width=60)
            output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

            self.testcase_widgets.append((input_entry, output_entry))

    def new_problem(self):
        self.problem_id_var.set("")
        self.problem_title_var.set("")
        self.markdown_editor.delete(1.0, tk.END)
        self.time_limit_var.set(1000)
        self.memory_limit_var.set(256)
        self.testcase_count_var.set(1)
        self.update_testcases()
        self.current_problem_id = None
        self.status_var.set("已创建新题目")

    def save_problem(self):
        problem_id = self.problem_id_var.get().strip()
        if not problem_id:
            messagebox.showerror("错误", "请输入题目ID")
            return
        if not PathValidator.is_safe_path(problem_id):
            messagebox.showerror("错误", "题目ID包含危险字符")
            return
        if not re.match(r'^[A-Za-z0-9_\u4e00-\u9fa5\-]+$', problem_id):
            messagebox.showerror("错误", "题目ID只能包含字母、数字、下划线、中划线或中文")
            return
        if not self.problem_title_var.get().strip():
            messagebox.showerror("错误", "请输入题目名称")
            return

        try:
            time_limit = int(self.time_limit_var.get())
            memory_limit = int(self.memory_limit_var.get())
        except Exception:
            messagebox.showerror("错误", "时间/内存限制必须为整数")
            return
        if time_limit <= 0 or memory_limit <= 0:
            messagebox.showerror("错误", "时间/内存限制必须为正数")
            return

        # 收集测试点
        testcases = []
        for i, (input_widget, output_widget) in enumerate(self.testcase_widgets):
            if i < self.testcase_count_var.get():
                testcases.append({
                    "input": input_widget.get(1.0, tk.END).rstrip('\n'),
                    "output": output_widget.get(1.0, tk.END).rstrip('\n')
                })

        problem_data = {
            "id": problem_id,
            "title": self.problem_title_var.get(),
            "description": self.markdown_editor.get(1.0, tk.END),
            "time_limit": time_limit,
            "memory_limit": memory_limit,
            "testcases": testcases
        }

        self.problems[problem_id] = problem_data
        self.save_problems_to_file()
        self.update_problem_list()
        self.update_solving_problem_list()
        self.update_problem_stats()
        self.status_var.set(f"题目 {problem_id} 保存成功")
        messagebox.showinfo("成功", "题目保存成功")

    def delete_problem(self):
        selection = self.problem_listbox.curselection()
        if not selection:
            messagebox.showerror("错误", "请选择要删除的题目")
            return
        problem_text = self.problem_listbox.get(selection[0])
        problem_id = problem_text.split(":")[0].strip()
        if messagebox.askyesno("确认", f"确定删除题目 {problem_id} 吗？"):
            self.problems.pop(problem_id, None)
            self.save_problems_to_file()
            self.update_problem_list()
            self.update_solving_problem_list()
            self.update_problem_stats()
            self.new_problem()
            self.status_var.set(f"题目 {problem_id} 已删除")

    def random_problem(self):
        if self.problems:
            import random
            problem_id = random.choice(list(self.problems.keys()))
            self.load_problem_to_editor(problem_id)
            self.status_var.set(f"已随机跳转到题目 {problem_id}")

    def search_problems(self, *args):
        search_text = self.search_var.get().lower()
        self.solving_problem_listbox.delete(0, tk.END)
        for problem_id, problem_data in self.problems.items():
            if (search_text in problem_id.lower() or
                    search_text in problem_data["title"].lower()):
                self.solving_problem_listbox.insert(tk.END, f"{problem_id}: {problem_data['title']}")

    def search_storage_problems(self, *args):
        search_text = self.search_storage_var.get().lower()
        self.problem_listbox.delete(0, tk.END)
        for problem_id, problem_data in self.problems.items():
            if (search_text in problem_id.lower() or
                    search_text in problem_data["title"].lower()):
                self.problem_listbox.insert(tk.END, f"{problem_id}: {problem_data['title']}")
        self.update_problem_stats()

    def update_problem_stats(self):
        self.problem_stats_var.set(f"题目总数: {len(self.problems)}")

    # ==================== 编译器浏览 ====================

    def browse_compiler(self, compiler_type, path_var=None):
        if path_var is None:
            path_var = {
                "cpp": self.cpp_path_var,
                "c": self.c_path_var,
                "python": self.python_path_var,
                "javac": self.javac_path_var,
                "java": self.java_path_var,
            }.get(compiler_type)

        if compiler_type == "python":
            filetypes = [("Python", "python*.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")]
        elif compiler_type in ["javac", "java"]:
            filetypes = [("Java", "*.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")]
        else:
            filetypes = [("可执行文件", "*.exe"), ("所有文件", "*.*")]

        filename = filedialog.askopenfilename(title=f"选择 {compiler_type} 编译器",
                                              filetypes=filetypes)
        if not filename:
            return
        if not PathValidator.is_safe_path(filename):
            messagebox.showerror("错误", "选择的路径包含危险字符")
            return
        if PathValidator.is_removable_drive(filename):
            if not messagebox.askyesno("警告", "路径位于可移动设备上，是否继续？"):
                return

        path_var.set(filename)
        works = self.test_compiler_works(filename, compiler_type)
        self.compiler_paths[compiler_type]["works"] = works
        self.compiler_paths[compiler_type]["path"] = filename
        self.status_var.set(f"{compiler_type} 编译器路径已设置 (可用: {works})")

    # ==================== 提交与判题 ====================

    def submit_code(self):
        if self.judging:
            messagebox.showwarning("警告", "正在判题中，请稍候...")
            return
        if not self.current_problem_id:
            messagebox.showerror("错误", "请先选择题目")
            return
        code = self.code_editor.get(1.0, tk.END)
        if not code.strip():
            messagebox.showerror("错误", "请输入代码")
            return

        language = self.language_var.get()

        # 检查编译器是否配置
        if language in ["cpp", "c"]:
            path = self.cpp_path_var.get() if language == "cpp" else self.c_path_var.get()
            valid, msg = PathValidator.is_valid_compiler_path(path)
            if not valid:
                messagebox.showerror("错误",
                    f"{'C++' if language=='cpp' else 'C'} 编译器未配置或无效：{msg}\n"
                    f"请先到'编译器配置'选项卡进行配置。")
                return
        elif language == "python":
            path = self.python_path_var.get()
            valid, msg = PathValidator.is_valid_compiler_path(path)
            if not valid:
                messagebox.showerror("错误", f"Python 解释器未配置或无效：{msg}")
                return
        elif language == "java":
            valid1, msg1 = PathValidator.is_valid_compiler_path(self.javac_path_var.get())
            valid2, msg2 = PathValidator.is_valid_compiler_path(self.java_path_var.get())
            if not valid1 or not valid2:
                messagebox.showerror("错误",
                    f"Java 环境未配置：{msg1 if not valid1 else ''}{msg2 if not valid2 else ''}")
                return

        if self.security_mode.get():
            issues = self.security_check(code, language)
            if issues:
                self.update_result(f"安全警告:\n{issues}\n")
                if not messagebox.askyesno("安全警告", "代码中包含可能不安全的操作，是否继续执行？"):
                    return

        self.judging = True
        self.submit_btn.config(state=tk.DISABLED, text="判题中...")
        self.test_btn.config(state=tk.DISABLED)
        self.status_var.set("正在判题，请稍候...")

        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete(1.0, tk.END)
        self.result_text.config(state=tk.DISABLED)

        threading.Thread(target=self.judge_code_thread,
                         args=(code, language), daemon=True).start()

    def judge_code_thread(self, code, language):
        try:
            self.judge_code(code, language)
        except Exception as e:
            err = f"判题过程出错: {str(e)}\n{traceback.format_exc()}\n"
            self.update_result(err)
            print(err)
        finally:
            self.root.after(0, self.enable_buttons)

    def enable_buttons(self):
        self.judging = False
        self.submit_btn.config(state=tk.NORMAL, text="提交代码")
        self.test_btn.config(state=tk.NORMAL)

    def security_check(self, code, language):
        dangerous_patterns = [
            r'\bsystem\s*\(', r'\bpopen\s*\(', r'\bexecve\b', r'\bfork\s*\(',
            r'\bsubprocess\b', r'ProcessBuilder', r'Runtime\.getRuntime',
            r'\bsocket\.', r'URLConnection', r'HttpURLConnection',
            r'\brequests\.', r'\burllib\.',
            r'__import__\s*\(', r'\beval\s*\(', r'\bexec\s*\(',
        ]
        issues = []
        for pattern in dangerous_patterns:
            if re.search(pattern, code):
                issues.append(f"检测到: {pattern}")
        return "\n".join(issues) if issues else None

    def judge_code(self, code, language):
        problem = self.problems.get(self.current_problem_id)
        if not problem:
            self.update_result("错误: 题目不存在\n")
            self.root.after(0, lambda: self.status_var.set("判题错误: 题目不存在"))
            return

        total_cases = len(problem["testcases"])
        passed_cases = 0

        self.update_result(f"开始判题，共 {total_cases} 个测试点\n")
        self.update_result("=" * 50 + "\n")

        for i, testcase in enumerate(problem["testcases"]):
            self.update_result(f"测试点 {i+1}: ")
            try:
                result = self.run_code(code, language, testcase["input"],
                                       problem["time_limit"], problem["memory_limit"])

                if result["status"] == "AC":
                    if self.compare_output(result["output"], testcase["output"]):
                        self.update_result("● AC 通过")
                        if "time" in result:
                            self.update_result(f" ({result['time']:.2f}ms)")
                        if "memory" in result:
                            self.update_result(f" [{result['memory']:.1f}MB]")
                        self.update_result("\n")
                        passed_cases += 1
                    else:
                        self.update_result("■ WA 答案错误\n")
                        self.update_result(f"期望输出: {testcase['output'][:200]}\n")
                        self.update_result(f"实际输出: {result['output'][:200]}\n")
                else:
                    status_text = self.get_status_text(result['status'])
                    self.update_result(f"■ {result['status']} {status_text}\n")
                    if result.get("error"):
                        self.update_result(f"错误信息: {result['error'][:300]}\n")
                    if result.get("time"):
                        self.update_result(f"运行时间: {result['time']:.2f}ms\n")
                    if result.get("memory"):
                        self.update_result(f"内存使用: {result['memory']:.1f}MB\n")

            except Exception as e:
                self.update_result(f"■ RE 运行错误 ({str(e)})\n")
                print(f"测试点 {i+1} 运行错误: {traceback.format_exc()}")

        self.update_result("=" * 50 + "\n")
        if passed_cases == total_cases:
            self.update_result(f"全部通过！AC {passed_cases}/{total_cases}\n")
            self.root.after(0, lambda: self.status_var.set(
                f"判题完成: AC {passed_cases}/{total_cases}"))
        else:
            self.update_result(f"通过 {passed_cases}/{total_cases}\n")
            self.root.after(0, lambda: self.status_var.set(
                f"判题完成: 通过 {passed_cases}/{total_cases}"))

    def get_status_text(self, status):
        return {
            "AC": "通过", "WA": "答案错误", "TLE": "时间超限",
            "MLE": "内存超限", "RE": "运行错误", "CE": "编译错误"
        }.get(status, status)

    # ==================== 运行代码 ====================

    def run_code(self, code, language, input_data, time_limit, memory_limit):
        try:
            if language == "cpp":
                return self.run_cpp_code(code, input_data, time_limit, memory_limit)
            elif language == "c":
                return self.run_c_code(code, input_data, time_limit, memory_limit)
            elif language == "python":
                return self.run_python_code(code, input_data, time_limit, memory_limit)
            elif language == "java":
                return self.run_java_code(code, input_data, time_limit, memory_limit)
        except Exception as e:
            print(f"运行代码错误: {traceback.format_exc()}")
            return {"status": "RE", "output": "", "error": f"运行代码出错: {str(e)}",
                    "time": 0, "memory": 0}
        return {"status": "RE", "output": "", "error": "不支持的编程语言",
                "time": 0, "memory": 0}

    def _compile_and_run(self, code, language, input_data, time_limit, memory_limit,
                         compiler_path, suffix, std_flag):
        temp_file = None
        out_file = None
        try:
            is_valid, msg = PathValidator.is_valid_compiler_path(compiler_path)
            if not is_valid:
                return {"status": "CE", "output": "", "error": f"编译器路径无效: {msg}",
                        "time": 0, "memory": 0}

            temp_file = tempfile.NamedTemporaryFile(mode='w', suffix=suffix,
                                                    delete=False, encoding='utf-8')
            temp_file.write(code)
            temp_file.close()
            out_file = temp_file.name + (".exe" if platform.system() == "Windows" else ".out")

            compile_cmd = [compiler_path, std_flag, temp_file.name, "-o", out_file]
            if self.o2_optimization.get():
                compile_cmd.insert(2, "-O2")

            CompilerLock.acquire(compiler_path)
            try:
                compile_proc = silent_subprocess_run(compile_cmd, timeout=15)
            finally:
                CompilerLock.release(compiler_path)

            if compile_proc.returncode != 0:
                return {"status": "CE", "output": "",
                        "error": (compile_proc.stderr or "")[:500], "time": 0, "memory": 0}

            process = subprocess.Popen(
                [out_file],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace'
            )
            monitor = ProcessMonitor(process, time_limit, memory_limit, language)
            return monitor.monitor_and_wait(input_data)

        except subprocess.TimeoutExpired:
            return {"status": "TLE", "output": "", "error": "编译超时", "time": 0, "memory": 0}
        except Exception as e:
            return {"status": "RE", "output": "", "error": f"运行异常: {str(e)}",
                    "time": 0, "memory": 0}
        finally:
            if temp_file and os.path.exists(temp_file.name):
                safe_remove(temp_file.name)
            if out_file and os.path.exists(out_file):
                safe_remove(out_file)

    def run_cpp_code(self, code, input_data, time_limit, memory_limit):
        return self._compile_and_run(code, "cpp", input_data, time_limit, memory_limit,
                                     self.cpp_path_var.get(), ".cpp", "-std=c++17")

    def run_c_code(self, code, input_data, time_limit, memory_limit):
        return self._compile_and_run(code, "c", input_data, time_limit, memory_limit,
                                     self.c_path_var.get(), ".c", "-std=c11")

    def run_python_code(self, code, input_data, time_limit, memory_limit):
        temp_file = None
        try:
            interpreter = self.python_path_var.get()
            is_valid, msg = PathValidator.is_valid_compiler_path(interpreter)
            if not is_valid:
                return {"status": "CE", "output": "", "error": f"解释器路径无效: {msg}",
                        "time": 0, "memory": 0}

            temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                                    delete=False, encoding='utf-8')
            temp_file.write(code)
            temp_file.close()

            process = subprocess.Popen(
                [interpreter, "-X", "utf8", temp_file.name],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace'
            )
            monitor = ProcessMonitor(process, time_limit, memory_limit, "python")
            return monitor.monitor_and_wait(input_data)
        except Exception as e:
            return {"status": "RE", "output": "", "error": f"运行异常: {str(e)}",
                    "time": 0, "memory": 0}
        finally:
            if temp_file and os.path.exists(temp_file.name):
                safe_remove(temp_file.name)

    def run_java_code(self, code, input_data, time_limit, memory_limit):
        try:
            javac_path = self.javac_path_var.get()
            java_path = self.java_path_var.get()
            valid_javac, msg_javac = PathValidator.is_valid_compiler_path(javac_path)
            valid_java, msg_java = PathValidator.is_valid_compiler_path(java_path)
            if not valid_javac:
                return {"status": "CE", "output": "",
                        "error": f"Java编译器路径无效: {msg_javac}", "time": 0, "memory": 0}
            if not valid_java:
                return {"status": "CE", "output": "",
                        "error": f"Java运行时路径无效: {msg_java}", "time": 0, "memory": 0}
            return JavaRunner.run_java_code(code, input_data, time_limit, memory_limit,
                                            javac_path, java_path)
        except Exception as e:
            return {"status": "RE", "output": "", "error": f"Java运行异常: {str(e)}",
                    "time": 0, "memory": 0}

    def compare_output(self, actual, expected):
        """比较输出（忽略行尾空白和末尾空行）"""
        def normalize(s):
            # 按行分割，去掉每行尾部空白，去掉末尾空行
            lines = [line.rstrip() for line in s.strip('\n').split('\n')]
            while lines and not lines[-1]:
                lines.pop()
            return lines
        return normalize(actual) == normalize(expected)

    # ==================== 结果输出 ====================

    def update_result(self, text):
        self.root.after(0, self._update_result_text, text)

    def _update_result_text(self, text):
        self.result_text.config(state=tk.NORMAL)
        self.result_text.insert(tk.END, text)
        self.result_text.see(tk.END)
        self.result_text.config(state=tk.DISABLED)

    # ==================== 测试运行 ====================

    def test_run(self):
        if self.judging:
            messagebox.showwarning("警告", "正在判题中，请稍候...")
            return
        code = self.code_editor.get(1.0, tk.END)
        if not code.strip():
            messagebox.showerror("错误", "请输入代码")
            return

        language = self.language_var.get()

        dialog = tk.Toplevel(self.root)
        dialog.title("测试运行")
        dialog.geometry("600x500")

        ttk.Label(dialog, text="输入测试数据:").pack(pady=5)
        input_text = scrolledtext.ScrolledText(dialog, height=10)
        input_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        ttk.Label(dialog, text="运行结果:").pack(pady=5)
        result_text = scrolledtext.ScrolledText(dialog, height=10)
        result_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        result_text.config(state=tk.DISABLED)

        def run_test():
            test_input = input_text.get(1.0, tk.END)
            result = self.run_code(code, language, test_input, 10000, 512)
            result_text.config(state=tk.NORMAL)
            result_text.delete(1.0, tk.END)
            result_text.insert(tk.END, f"状态: {result['status']}\n")
            if result.get('error'):
                result_text.insert(tk.END, f"错误: {result['error'][:1000]}\n")
            if result.get('time'):
                result_text.insert(tk.END, f"时间: {result['time']:.2f}ms\n")
            if result.get('memory'):
                result_text.insert(tk.END, f"内存: {result['memory']:.1f}MB\n")
            if result.get('output'):
                result_text.insert(tk.END, f"输出:\n{result['output'][:2000]}")
            result_text.config(state=tk.DISABLED)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text="运行", command=run_test).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT, padx=2)

    def clear_code(self):
        self.code_editor.delete(1.0, tk.END)
        self.status_var.set("代码编辑器已清空")

    # ==================== 编译器测试与保存 ====================

    def test_compilers(self):
        results = []
        checks = [
            ("cpp", "C++编译器", self.cpp_path_var),
            ("c", "C编译器", self.c_path_var),
            ("python", "Python解释器", self.python_path_var),
            ("javac", "Java编译器", self.javac_path_var),
            ("java", "Java运行时", self.java_path_var),
        ]
        for key, name, var in checks:
            path = var.get()
            if not path:
                results.append(f"✗ {name}: 未配置")
                self.compiler_paths[key]["works"] = False
                continue
            valid, msg = PathValidator.is_valid_compiler_path(path)
            if not valid:
                results.append(f"✗ {name}: {msg}")
                self.compiler_paths[key]["works"] = False
                continue
            works = self.test_compiler_works(path, key)
            self.compiler_paths[key]["works"] = works
            results.append(f"{'✓' if works else '✗'} {name}: {'可用' if works else '路径有效但无法工作'}")

        messagebox.showinfo("编译器测试结果", "\n".join(results))
        self.status_var.set("编译器测试完成")

    def save_compiler_config(self):
        self.compiler_paths["cpp"]["path"] = self.cpp_path_var.get()
        self.compiler_paths["c"]["path"] = self.c_path_var.get()
        self.compiler_paths["python"]["path"] = self.python_path_var.get()
        self.compiler_paths["javac"]["path"] = self.javac_path_var.get()
        self.compiler_paths["java"]["path"] = self.java_path_var.get()

        # 保存到文件
        try:
            with open(resource_path("compiler_config.json"), 'w', encoding='utf-8') as f:
                json.dump({k: v["path"] for k, v in self.compiler_paths.items()},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        self.status_var.set("编译器配置已保存")
        messagebox.showinfo("成功", "编译器配置已保存")

    def load_compiler_config(self):
        try:
            cfg_file = resource_path("compiler_config.json")
            if os.path.exists(cfg_file):
                with open(cfg_file, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                for k, path in cfg.items():
                    if k in self.compiler_paths and path:
                        self.compiler_paths[k]["path"] = path
                        self.compiler_paths[k]["works"] = self.test_compiler_works(path, k)
                        if hasattr(self, f'{k}_path_var'):
                            getattr(self, f'{k}_path_var').set(path)
        except Exception as e:
            print(f"加载编译器配置失败: {e}")

    # ==================== Markdown 与图片 ====================

    def add_image(self):
        filename = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.gif *.bmp"), ("所有文件", "*.*")]
        )
        if not filename:
            return
        if not PathValidator.is_safe_path(filename):
            messagebox.showerror("错误", "图片路径包含危险字符")
            return

        resources_dir = resource_path("problem_resources")
        os.makedirs(resources_dir, exist_ok=True)

        ext = os.path.splitext(filename)[1]
        unique_name = f"{uuid.uuid4().hex}{ext}"
        dest_filename = os.path.join(resources_dir, unique_name)

        try:
            shutil.copy2(filename, dest_filename)
            self.markdown_editor.insert(tk.INSERT, f"![图片]({unique_name})")
            self.status_var.set("图片已添加到题目描述")
        except Exception as e:
            messagebox.showerror("错误", f"添加图片失败: {str(e)}")

    def add_code_block(self):
        self.markdown_editor.insert(tk.INSERT, "\n```\n// 在这里输入代码\n```\n")
        self.status_var.set("代码块模板已添加")

    def add_math_formula(self):
        self.markdown_editor.insert(tk.INSERT,
            "\n**数学公式支持:**\n- 行内公式: `$公式$`\n- 块级公式: `$$公式$$`\n")
        self.status_var.set("数学公式提示已添加")

    def preview_problem_enhanced(self):
        description = self.markdown_editor.get(1.0, tk.END)

        preview = tk.Toplevel(self.root)
        preview.title("题目预览")
        preview.geometry("800x600")

        text_widget = scrolledtext.ScrolledText(preview, wrap=tk.WORD, font=('Arial', 11))
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        text_widget.tag_config("title", font=('Arial', 14, 'bold'))
        text_widget.tag_config("heading", font=('Arial', 12, 'bold'))
        text_widget.tag_config("code", font=('Consolas', 10), background="#f0f0f0")

        lines = description.split('\n')
        in_code_block = False
        code_content = []

        for line in lines:
            if line.strip().startswith('```'):
                if in_code_block:
                    text_widget.insert(tk.END, '\n'.join(code_content) + '\n', "code")
                    code_content = []
                    in_code_block = False
                else:
                    in_code_block = True
            elif in_code_block:
                code_content.append(line)
            elif line.startswith('# '):
                text_widget.insert(tk.END, line[2:] + '\n', "title")
            elif line.startswith('## '):
                text_widget.insert(tk.END, line[3:] + '\n', "heading")
            elif line.startswith('### '):
                text_widget.insert(tk.END, line[4:] + '\n\n')
            elif '`' in line:
                parts = line.split('`')
                for i, part in enumerate(parts):
                    if i % 2 == 1:
                        text_widget.insert(tk.END, part, "code")
                    else:
                        text_widget.insert(tk.END, part)
                text_widget.insert(tk.END, '\n')
            else:
                text_widget.insert(tk.END, line + '\n')

        text_widget.config(state=tk.DISABLED)
        self.status_var.set("题目预览已打开")

    # ==================== 导入导出 ====================

    def export_problem(self):
        if not self.current_problem_id:
            messagebox.showerror("错误", "请先选择题目")
            return
        filename = filedialog.asksaveasfilename(
            title="导出题目", defaultextension=".json",
            filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")]
        )
        if not filename:
            return
        problem_data = self.problems.get(self.current_problem_id)
        if not problem_data:
            return
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(problem_data, f, ensure_ascii=False, indent=2)
            self.status_var.set(f"题目 {self.current_problem_id} 导出成功")
            messagebox.showinfo("成功", "题目导出成功")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")

    def import_problem(self):
        filename = filedialog.askopenfilename(
            title="导入题目",
            filetypes=[("JSON文件", "*.json"), ("所有文件", "*.*")]
        )
        if not filename:
            return
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                problem_data = json.load(f)
            problem_id = problem_data.get("id")
            if not problem_id:
                messagebox.showerror("错误", "题目数据格式错误：缺少ID字段")
                return

            if problem_id in self.problems:
                response = messagebox.askyesnocancel(
                    "冲突处理",
                    f"题目 {problem_id} 已存在，如何处理？\n\n是: 覆盖\n否: 跳过\n取消: 中止"
                )
                if response is None:
                    return
                elif not response:
                    self.status_var.set(f"题目 {problem_id} 已跳过")
                    return

            self.problems[problem_id] = problem_data
            self.save_problems_to_file()
            self.update_problem_list()
            self.update_solving_problem_list()
            self.update_problem_stats()
            self.load_problem_to_editor(problem_id)
            messagebox.showinfo("成功", "题目导入成功")
        except Exception as e:
            messagebox.showerror("错误", f"导入失败: {str(e)}")

    # ==================== 批量导入导出 ====================

    @async_task
    def batch_import_problems_async(self, import_from_zip, source_path, conflict_strategy):
        try:
            self.root.after(0, lambda: self.status_var.set("正在批量导入题目..."))
            self.root.after(0, lambda: self.root.config(cursor="wait"))

            if import_from_zip:
                success = self.import_manager.import_from_zip(source_path, conflict_strategy)
            else:
                success = self.import_manager.import_from_folder(source_path, conflict_strategy)

            self.root.after(0, self._on_import_complete, success)
        except Exception as e:
            self.root.after(0, messagebox.showerror, "导入错误",
                            f"批量导入过程异常: {str(e)}")
        finally:
            self.root.after(0, lambda: self.root.config(cursor=""))

    def _on_import_complete(self, success):
        if success:
            self.save_problems_to_file()
            self.update_problem_list()
            self.update_solving_problem_list()
            self.update_problem_stats()
            report = self.import_manager.get_report()
            self.show_import_report(report)
        else:
            messagebox.showerror("导入失败", "批量导入过程中出现错误，请查看控制台输出。")
        self.status_var.set("批量导入完成")

    def show_import_report(self, report):
        dialog = tk.Toplevel(self.root)
        dialog.title("批量导入报告")
        dialog.geometry("700x500")
        ttk.Label(dialog, text="批量导入详细报告",
                  font=('Arial', 12, 'bold')).pack(pady=10)
        report_text = scrolledtext.ScrolledText(dialog, wrap=tk.WORD, font=('Consolas', 10))
        report_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        report_text.insert(1.0, report)
        report_text.config(state=tk.DISABLED)
        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=10)

    def batch_import_problems(self):
        import_type = messagebox.askquestion(
            "导入源类型",
            "请选择导入源类型：\n\n是: 从ZIP压缩包导入\n否: 从文件夹导入"
        )
        if import_type == "yes":
            filename = filedialog.askopenfilename(
                title="选择导入的ZIP文件",
                filetypes=[("ZIP压缩包", "*.zip"), ("所有文件", "*.*")]
            )
            if not filename:
                return
            import_from_zip = True
            source_path = filename
        else:
            foldername = filedialog.askdirectory(title="选择导入的文件夹")
            if not foldername:
                return
            import_from_zip = False
            source_path = foldername

        conflict_strategy = self._ask_conflict_strategy_friendly()
        if not conflict_strategy:
            return
        self.batch_import_problems_async(import_from_zip, source_path, conflict_strategy)

    def _ask_conflict_strategy_friendly(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("冲突处理策略")
        dialog.geometry("500x350")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="发现题目ID冲突时，请选择处理方式：",
                  font=('Arial', 11, 'bold')).pack(pady=15)

        strategies = [
            ("跳过", "保留现有题目，跳过新题目（推荐）", "skip"),
            ("覆盖", "用新题目替换现有题目", "overwrite"),
            ("重命名", "为新题目生成新的ID", "rename"),
        ]
        selected_strategy = tk.StringVar(value="skip")
        for display_name, description, value in strategies:
            frame = ttk.Frame(dialog)
            frame.pack(fill=tk.X, padx=20, pady=8)
            ttk.Radiobutton(frame, text=display_name, variable=selected_strategy,
                            value=value).pack(side=tk.LEFT)
            ttk.Label(frame, text=description).pack(side=tk.LEFT, padx=10)

        result = [None]

        def on_ok():
            result[0] = selected_strategy.get()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=20)
        ttk.Button(btn_frame, text="开始导入", command=on_ok).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="取消", command=on_cancel).pack(side=tk.RIGHT, padx=10)

        dialog.wait_window()
        return result[0]

    def batch_export_problems(self):
        if not self.problems:
            messagebox.showerror("错误", "没有题目可以导出")
            return

        export_format = messagebox.askquestion(
            "导出格式",
            "请选择导出格式：\n\n是: 导出为ZIP压缩包（推荐）\n否: 导出为文件夹"
        )
        if export_format == "yes":
            filename = filedialog.asksaveasfilename(
                title="批量导出题目",
                defaultextension=".zip",
                filetypes=[("ZIP压缩包", "*.zip"), ("所有文件", "*.*")]
            )
            export_as_zip = True
        else:
            foldername = filedialog.askdirectory(title="选择导出文件夹")
            if not foldername:
                return
            filename = foldername
            export_as_zip = False

        if not filename:
            return
        if not PathValidator.is_safe_path(filename):
            messagebox.showerror("错误", "导出路径包含危险字符")
            return

        try:
            self.status_var.set("正在批量导出题目...")
            if export_as_zip:
                self._export_to_zip(filename)
                messagebox.showinfo("成功", f"已导出 {len(self.problems)} 个题目到ZIP文件")
            else:
                self._export_to_folder(filename)
                messagebox.showinfo("成功", f"已导出 {len(self.problems)} 个题目到文件夹")
            self.status_var.set("批量导出完成")
        except Exception as e:
            messagebox.showerror("错误", f"批量导出失败: {str(e)}")
            self.status_var.set("批量导出失败")

    def _safe_filename(self, problem_id):
        """将题目ID转为安全文件名"""
        return re.sub(r'[\\/:*?"<>|]', '_', problem_id)

    def _export_to_zip(self, zip_filename):
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for problem_id, problem_data in self.problems.items():
                safe_id = self._safe_filename(problem_id)
                problem_filename = f"problems/{safe_id}.json"
                zipf.writestr(problem_filename,
                              json.dumps(problem_data, ensure_ascii=False, indent=2))

                description = problem_data.get("description", "")
                for image_ref in re.findall(r'!\[.*?\]\((.*?)\)', description):
                    if not image_ref.startswith('http'):
                        source_path = resource_path(os.path.join("problem_resources", image_ref))
                        if os.path.exists(source_path):
                            zipf.write(source_path, f"resources/{image_ref}")

            index_data = {
                "version": "1.2",
                "export_date": datetime.now().isoformat(),
                "total_problems": len(self.problems),
                "problems": list(self.problems.keys())
            }
            zipf.writestr("index.json", json.dumps(index_data, ensure_ascii=False, indent=2))

    def _export_to_folder(self, folder_path):
        problems_dir = os.path.join(folder_path, "problems")
        resources_dir = os.path.join(folder_path, "resources")
        os.makedirs(problems_dir, exist_ok=True)
        os.makedirs(resources_dir, exist_ok=True)

        for problem_id, problem_data in self.problems.items():
            safe_id = self._safe_filename(problem_id)
            with open(os.path.join(problems_dir, f"{safe_id}.json"), 'w',
                      encoding='utf-8') as f:
                json.dump(problem_data, f, ensure_ascii=False, indent=2)

            description = problem_data.get("description", "")
            for image_ref in re.findall(r'!\[.*?\]\((.*?)\)', description):
                if not image_ref.startswith('http'):
                    source_path = resource_path(os.path.join("problem_resources", image_ref))
                    if os.path.exists(source_path):
                        dest_path = os.path.join(resources_dir, image_ref)
                        os.makedirs(os.path.dirname(dest_path) or resources_dir, exist_ok=True)
                        shutil.copy2(source_path, dest_path)

        index_data = {
            "version": "1.2",
            "export_date": datetime.now().isoformat(),
            "total_problems": len(self.problems),
            "problems": list(self.problems.keys())
        }
        with open(os.path.join(folder_path, "index.json"), 'w', encoding='utf-8') as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)

    # ==================== 数据加载与保存 ====================

    def load_problems(self):
        try:
            problems_file = resource_path("problems.json")
            if os.path.exists(problems_file):
                with open(problems_file, "r", encoding="utf-8") as f:
                    self.problems = json.load(f)
                if not isinstance(self.problems, dict):
                    self.problems = {}
                self.status_var.set(f"已加载 {len(self.problems)} 个题目")
                print(f"已加载 {len(self.problems)} 个题目")
                self.update_problem_list()
                self.update_solving_problem_list()
                self.update_problem_stats()
        except Exception as e:
            print(f"加载题目数据失败: {e}")
            self.problems = {}

    def save_problems_to_file(self):
        try:
            with open(resource_path("problems.json"), "w", encoding="utf-8") as f:
                json.dump(self.problems, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("错误", f"保存题目数据失败: {str(e)}")

    def update_problem_list(self):
        if hasattr(self, 'problem_listbox'):
            self.problem_listbox.delete(0, tk.END)
            for problem_id, problem_data in self.problems.items():
                self.problem_listbox.insert(tk.END, f"{problem_id}: {problem_data['title']}")

    def update_solving_problem_list(self):
        if hasattr(self, 'solving_problem_listbox'):
            self.solving_problem_listbox.delete(0, tk.END)
            for problem_id, problem_data in self.problems.items():
                self.solving_problem_listbox.insert(
                    tk.END, f"{problem_id}: {problem_data['title']}")

    # ==================== 事件处理 ====================

    def on_problem_select(self, event):
        selection = self.problem_listbox.curselection()
        if selection:
            problem_text = self.problem_listbox.get(selection[0])
            problem_id = problem_text.split(":")[0].strip()
            self.load_problem_to_editor(problem_id)
            self.status_var.set(f"已选择题目: {problem_id}")

    def on_solving_problem_select(self, event):
        selection = self.solving_problem_listbox.curselection()
        if selection:
            problem_text = self.solving_problem_listbox.get(selection[0])
            problem_id = problem_text.split(":")[0].strip()
            self.load_problem_for_solving(problem_id)
            self.status_var.set(f"已选择题目: {problem_id}")

    def load_problem_to_editor(self, problem_id):
        problem_data = self.problems.get(problem_id)
        if not problem_data:
            return
        self.current_problem_id = problem_id
        self.problem_id_var.set(problem_data["id"])
        self.problem_title_var.set(problem_data["title"])
        self.markdown_editor.delete(1.0, tk.END)
        self.markdown_editor.insert(tk.END, problem_data.get("description", ""))
        self.time_limit_var.set(problem_data.get("time_limit", 1000))
        self.memory_limit_var.set(problem_data.get("memory_limit", 256))

        testcases = problem_data.get("testcases", [])
        self.testcase_count_var.set(max(1, len(testcases)))
        self.update_testcases()

        # 【修复】用保存的引用填数据
        for i, (input_widget, output_widget) in enumerate(self.testcase_widgets):
            if i < len(testcases):
                input_widget.delete(1.0, tk.END)
                input_widget.insert(tk.END, testcases[i].get("input", ""))
                output_widget.delete(1.0, tk.END)
                output_widget.insert(tk.END, testcases[i].get("output", ""))

    def load_problem_for_solving(self, problem_id):
        problem_data = self.problems.get(problem_id)
        if not problem_data:
            return
        self.current_problem_id = problem_id

        self.problem_display.config(state=tk.NORMAL)
        self.problem_display.delete(1.0, tk.END)
        self.problem_display.insert(tk.END, problem_data.get("description", ""))
        self.problem_display.config(state=tk.DISABLED)

        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete(1.0, tk.END)
        self.result_text.config(state=tk.DISABLED)

        self.code_editor.delete(1.0, tk.END)
        self.code_editor.insert(tk.END, self.get_example_code(self.language_var.get()))

        self.status_var.set(f"已加载题目: {problem_id}")

    def get_example_code(self, language):
        examples = {
            "cpp": """#include <iostream>
using namespace std;

int main() {
    int a, b;
    cin >> a >> b;
    cout << a + b << endl;
    return 0;
}""",
            "c": """#include <stdio.h>

int main() {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\\n", a + b);
    return 0;
}""",
            "python": """a, b = map(int, input().split())
print(a + b)""",
            "java": """import java.util.Scanner;

public class Main {
    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
        int a = sc.nextInt();
        int b = sc.nextInt();
        System.out.println(a + b);
        sc.close();
    }
}"""
        }
        return examples.get(language, "")

    def on_closing(self):
        try:
            self.save_compiler_config()
        except Exception:
            pass
        self.root.destroy()


# ==================== 主函数 ====================

def main():
    try:
        resources_dir = resource_path("problem_resources")
        os.makedirs(resources_dir, exist_ok=True)

        data_file = resource_path("problems.json")
        if not os.path.exists(data_file):
            with open(data_file, 'w', encoding='utf-8') as f:
                json.dump({}, f)

        root = tk.Tk()
        app = OfflineOJSystem(root)
        root.mainloop()
    except Exception as e:
        print(f"程序启动失败: {e}")
        print(traceback.format_exc())
        try:
            tk.Tk().withdraw()
            messagebox.showerror("启动错误", f"程序启动失败:\n{e}\n\n{traceback.format_exc()}")
        except Exception:
            pass


if __name__ == "__main__":
    main()