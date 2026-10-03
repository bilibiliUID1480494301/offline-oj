"""设备标识的持久化测试。

要点只有两个，但两个都出过事：

1. **跨场次稳定** —— 设备 ID 是学生在测验里的身份，每场重摇就认不出自己那一行；
2. **读坏了要能自愈** —— 这个文件在用户机器上，可能被手工改、被同步工具截断、
   被别的版本写坏。读不出来时最合理的处置是"当作没有，重新生成一个"，
   而不是让整个程序起不来。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from offline_oj.net import identity as identity_mod
from offline_oj.net.identity import (DeviceIdentity, get_identity, load_identity,
                                     save_identity)
from offline_oj.net.session import DEVICE_ID_ALPHABET, DEVICE_ID_LENGTH


class TestDeviceIdentityFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "device.json"

    # ---- 生成 ---------------------------------------------------------------

    def test_first_call_generates_and_persists(self):
        identity = get_identity(self.path)
        self.assertTrue(identity.ready)
        self.assertEqual(len(identity.device_id), DEVICE_ID_LENGTH)
        self.assertTrue(set(identity.device_id) <= set(DEVICE_ID_ALPHABET))
        self.assertTrue(self.path.exists(), "首次生成后必须落盘")
        self.assertTrue(identity.host, "应当顺手记下本机名，方便老师认机器")

    def test_second_call_returns_the_same_id(self):
        """跨场次稳定 —— 这是整个身份模型的地基。"""
        first = get_identity(self.path)
        second = get_identity(self.path)
        self.assertEqual(first.device_id, second.device_id)

    def test_identity_survives_a_fresh_read(self):
        issued = get_identity(self.path)
        self.assertEqual(load_identity(self.path).device_id, issued.device_id)

    # ---- 容错 ---------------------------------------------------------------

    def test_missing_file_is_not_an_error(self):
        identity = load_identity(self.path)
        self.assertFalse(identity.ready)
        self.assertFalse(self.path.exists(), "只读的 load 不该顺手创建文件")

    def test_corrupt_json_is_recovered(self):
        """被截断的 JSON：读回来当没有，重新生成一个能用的。"""
        self.path.write_text('{"device_id": "ABCD', encoding="utf-8")
        identity = get_identity(self.path)
        self.assertTrue(identity.ready)
        self.assertNotEqual(identity.device_id, "")

    def test_wrong_shapes_do_not_crash(self):
        for raw in ("[]", "42", '"text"', "null", "{}"):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                identity = load_identity(self.path)
                self.assertFalse(identity.ready)
                self.assertTrue(get_identity(self.path).ready)

    def test_illegal_id_in_the_file_is_replaced(self):
        """人工改成 7 位、或含 0/O 混淆字符，都要被识别成"不可用"并重生成。"""
        for bad in ("ABC", "ABCD2345678", "ABCD234O"):
            with self.subTest(bad=bad):
                self.path.write_text(json.dumps({"device_id": bad}),
                                     encoding="utf-8")
                self.assertTrue(get_identity(self.path).ready)

    def test_confusable_id_is_folded_back_rather_than_replaced(self):
        """把 O 打成 0 是有歧义的写法，应当**纠正**而不是当成另一个 ID。

        纠正后仍是 8 位合法 ID，于是这个文件可以直接用，不必重摇。
        """
        self.path.write_text(json.dumps({"device_id": "abcd234o"}),
                             encoding="utf-8")
        identity = load_identity(self.path)
        self.assertEqual(identity.device_id, "ABCD2340")

    def test_username_round_trip(self):
        issued = get_identity(self.path)
        issued.username = "  张   三  "
        save_identity(self.path, issued)
        self.assertEqual(load_identity(self.path).username, "张 三")

    def test_overlong_username_is_truncated(self):
        identity = DeviceIdentity(device_id="ABCD2345", username="名" * 100)
        save_identity(self.path, identity)
        self.assertLessEqual(len(load_identity(self.path).username), 24)

    def test_save_creates_the_parent_directory(self):
        nested = Path(self._tmp.name) / "a" / "b" / "device.json"
        self.assertTrue(save_identity(nested, DeviceIdentity(device_id="ABCD2345")))
        self.assertTrue(nested.exists())

    # ---- 写盘 ---------------------------------------------------------------

    def test_write_leaves_no_temp_file_behind(self):
        """原子替换之后不该留下 .tmp，否则目录里会慢慢堆出垃圾。"""
        get_identity(self.path)
        leftovers = [p.name for p in self.path.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_write_replaces_atomically(self):
        """写新值不应看到半截内容：os.replace 是同分区原子操作。"""
        save_identity(self.path, DeviceIdentity(device_id="AAAA1111",
                                                username="甲"))
        save_identity(self.path, DeviceIdentity(device_id="BBBB2222",
                                                username="乙"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["device_id"], "BBBB2222")
        self.assertEqual(data["username"], "乙")

    def test_unwritable_target_reports_failure_instead_of_raising(self):
        """写不进去只记日志 —— 不该让用户因为存不下 ID 就连测验都进不去。"""
        target = Path(self._tmp.name) / "sub"
        target.mkdir()
        # 把目标路径做成一个目录：open 必然失败，且失败方式是 OSError
        blocked = target / "device.json"
        blocked.mkdir()
        self.assertFalse(save_identity(blocked, DeviceIdentity(device_id="AAAA1111")))

    def test_file_is_utf8_and_readable(self):
        save_identity(self.path, DeviceIdentity(device_id="ABCD2345",
                                                username="张三", host="机房-01"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("张三", text)
        self.assertTrue(text.endswith("\n"))

    def test_blank_identity_is_ready(self):
        self.assertTrue(identity_mod.blank_identity().ready)

    def test_env_override_keeps_device_file_beside_the_bank(self):
        """设备标识与题库、设置同处一个数据根目录。"""
        from offline_oj.paths import build_paths

        old = os.environ.get("OFFLINE_OJ_HOME")
        os.environ["OFFLINE_OJ_HOME"] = self._tmp.name
        try:
            paths = build_paths()
            self.assertEqual(paths.device_file.parent, paths.data_root)
            self.assertEqual(paths.device_file.name, "device.json")
            # 与设置分开存：恢复默认设置不该换掉学生的身份
            self.assertNotEqual(paths.device_file, paths.settings_file)
        finally:
            if old is None:
                os.environ.pop("OFFLINE_OJ_HOME", None)
            else:
                os.environ["OFFLINE_OJ_HOME"] = old


if __name__ == "__main__":
    unittest.main()
