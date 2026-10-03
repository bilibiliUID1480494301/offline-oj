"""加密帧协议的测试。

重点不在"能收发"，而在**四类攻击必须被挡住**：

* 重放 —— 把录下来的旧帧原样重发；
* 反射 —— 把发出去的帧原样打回发送方；
* 篡改 —— 改动密文或帧头任意一位；
* 越权 —— 用别的密钥解。

这四类挡不住，前面 :mod:`offline_oj.net.crypto` 的加密就白做了：
密码学保证"密文不可读"，完整性由 AAD 绑定帧头来保证，两者都得验。

实现细节：要拿到"线上真实字节"，**不能用裸 ``recv`` 去抢读** —— 那会把帧从
套接字里消费掉，通道随后就永远等不到数据（第一版就是这么把测试挂死的）。
这里用 :class:`Tap` 代理套接字：它转发一切，同时把发出去的字节抄一份。
"""

from __future__ import annotations

import socket
import struct
import unittest

from offline_oj.net import crypto, protocol


class Tap:
    """套接字代理：转发所有调用，并把本端**发出**的字节抄一份留底。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.written = bytearray()

    def sendall(self, data: bytes) -> None:
        self.written.extend(data)
        self.sock.sendall(data)

    def recv(self, size: int) -> bytes:
        return self.sock.recv(size)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def take_written(self) -> bytes:
        """取出并清空留底的字节。"""
        data = bytes(self.written)
        self.written.clear()
        return data


def make_key(secret: str = "135790") -> bytes:
    """测试用低强度 scrypt 参数 —— 只求快，不追求抗爆破。"""
    return crypto.derive_secret_key(secret, b"0123456789abcdef", n=1 << 12)


class ChannelPair:
    """一对相连的加密通道，并各自带一个记录线上字节的 Tap。"""

    def __init__(self, key: bytes | None = None) -> None:
        left, right = socket.socketpair()
        shared = key if key is not None else make_key()
        self.client_tap = Tap(left)
        self.server_tap = Tap(right)
        self.client = protocol.SecureChannel(self.client_tap, shared,
                                             protocol.DIRECTION_CLIENT)
        self.server = protocol.SecureChannel(self.server_tap, shared,
                                             protocol.DIRECTION_SERVER)

    def close(self) -> None:
        self.client_tap.close()
        self.server_tap.close()


def feed(frame: bytes, key: bytes, direction: int,
          suite: str = "chacha20") -> protocol.SecureChannel:
    """把一段字节喂给一个新建的通道，用于验证它会被怎样处理。"""
    left, right = socket.socketpair()
    left.sendall(frame)
    left.close()
    return protocol.SecureChannel(right, key, direction, suite)


def capture_frame(suite: str, key: bytes, direction: int,
                  message: dict, blob: bytes = b"") -> bytes:
    """在一条临时连接上用给定套件发一帧，把线上字节完整录下来。"""
    left, right = socket.socketpair()
    tap = Tap(left)
    channel = protocol.SecureChannel(tap, key, direction, suite)
    channel.send(message, blob)
    frame = bytes(tap.written)
    left.close()
    right.close()
    return frame


class TestPlainChannel(unittest.TestCase):
    """握手阶段的明文通道。"""

    def setUp(self) -> None:
        left, right = socket.socketpair()
        self.sender = protocol.PlainChannel(left)
        self.receiver = protocol.PlainChannel(right)

    def tearDown(self) -> None:
        self.sender.sock.close()
        self.receiver.sock.close()

    def test_roundtrip(self):
        self.sender.send({"kind": "hello", "username": "张三"})
        message, blob = self.receiver.recv()
        self.assertEqual(message["kind"], "hello")
        self.assertEqual(message["username"], "张三")
        self.assertEqual(blob, b"")

    def test_carries_binary_blob(self):
        self.sender.send({"kind": "challenge"}, b"\x00\xff\x10raw")
        message, blob = self.receiver.recv()
        self.assertEqual(message["kind"], "challenge")
        self.assertEqual(blob, b"\x00\xff\x10raw")

    def test_plaintext_is_actually_plain(self):
        """握手帧确实是明文的 —— 把"不要往里放隐私"这个约定钉死成用例。"""
        left, right = socket.socketpair()
        try:
            protocol.PlainChannel(left).send({"kind": "hello", "username": "张三"})
            raw = right.recv(4096)
            self.assertIn(b"hello", raw[4:])
            self.assertIn("张三".encode("utf-8"), raw[4:])
        finally:
            left.close()
            right.close()

    def test_rejects_oversized_declared_length(self):
        """对端声称一个超上限的帧长，必须在**分配内存之前**拒绝。"""
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("<I", protocol.MAX_FRAME_BYTES + 1))
            with self.assertRaises(protocol.ProtocolError) as caught:
                protocol.PlainChannel(right).recv()
            self.assertIn("超过上限", str(caught.exception))
        finally:
            left.close()
            right.close()

    def test_send_rejects_oversized_payload(self):
        left, right = socket.socketpair()
        try:
            with self.assertRaises(protocol.ProtocolError):
                protocol.PlainChannel(left).send(
                    {"kind": "x", "pad": "A" * (protocol.MAX_FRAME_BYTES + 100)})
        finally:
            left.close()
            right.close()


class TestSecureChannel(unittest.TestCase):
    """加密帧通道。"""

    def setUp(self) -> None:
        self.pair = ChannelPair()

    def tearDown(self) -> None:
        self.pair.close()

    def test_both_directions(self):
        self.pair.client.send_kind(protocol.MessageKind.FETCH, problem="P0001")
        kind, message, _ = self.pair.server.recv_kind()
        self.assertEqual(kind, protocol.MessageKind.FETCH)
        self.assertEqual(message["problem"], "P0001")

        self.pair.server.send_kind(protocol.MessageKind.PONG)
        kind, _, _ = self.pair.client.recv_kind()
        self.assertEqual(kind, protocol.MessageKind.PONG)

    def test_carries_chinese_and_blob(self):
        text = "读入两个整数，输出它们的和。\n样例：1 2 → 3"
        self.pair.client.send({"kind": "problems", "text": text}, b"\x89PNG\r\n")
        message, blob = self.pair.server.recv()
        self.assertEqual(message["text"], text)
        self.assertEqual(blob, b"\x89PNG\r\n")

    def test_wire_is_actually_encrypted(self):
        """题面与期望输出不能在裸线上出现 —— 这是"加密共享题库"的字面要求。"""
        secret = "SECRET-EXPECTED-OUTPUT-42"
        self.pair.client.send({"kind": "problems", "output": secret})
        wire = self.pair.client_tap.take_written()
        self.assertTrue(wire, "没有记录到任何线上字节")
        self.assertNotIn(secret.encode("utf-8"), wire)

    def test_sequences_increment(self):
        for _ in range(5):
            self.pair.client.send({"kind": "ping"})
            self.pair.server.recv()
        self.assertEqual(self.pair.client.send_seq, 5)
        self.assertEqual(self.pair.server.recv_seq, 5)

    def test_replay_is_rejected(self):
        """把录下来的帧原样重发：序号对不上 → AAD 不匹配 → 拒绝。"""
        self.pair.client.send({"kind": "submit", "code": "int main(){}"})
        recorded = self.pair.client_tap.take_written()
        self.pair.server.recv()                      # 第一次正常消费

        # 同一条连接上，重放刚录下来的那一帧
        self.pair.client_tap.sock.sendall(recorded)
        with self.assertRaises(protocol.ProtocolError) as caught:
            self.pair.server.recv()
        self.assertIn("认证失败", str(caught.exception))

    def test_reflection_is_rejected(self):
        """把客户端发出的帧原样打回客户端。

        客户端发送时 AAD 用「客户端方向」，接收时按「服务端方向」算 AAD，
        两者必然不同，于是校验失败。**防护的关键是收发方向位分开维护**。
        """
        self.pair.client.send({"kind": "ping"})
        frame = self.pair.client_tap.take_written()
        victim = feed(frame, make_key(), protocol.DIRECTION_CLIENT)
        try:
            with self.assertRaises(protocol.ProtocolError) as caught:
                victim.recv()
            self.assertIn("认证失败", str(caught.exception))
        finally:
            victim.sock.close()

    def test_same_frame_decrypts_in_its_own_direction(self):
        """同一个帧在正确方向上能解开。

        这条是上一条的对照组：如果方向是唯一变量，那么"反射被拒"才有意义；
        否则可能只是帧本身有问题、碰巧也报错。
        """
        self.pair.client.send({"kind": "ping"})
        frame = self.pair.client_tap.take_written()
        channel = feed(frame, make_key(), protocol.DIRECTION_SERVER)
        try:
            message, _ = channel.recv()
            self.assertEqual(message["kind"], "ping")
        finally:
            channel.sock.close()

    def test_tampered_ciphertext_rejected(self):
        self.pair.client.send({"kind": "submit", "code": "x"})
        frame = bytearray(self.pair.client_tap.take_written())
        frame[-20] ^= 0x01
        channel = feed(bytes(frame), make_key(), protocol.DIRECTION_SERVER)
        try:
            with self.assertRaises(protocol.ProtocolError):
                channel.recv()
        finally:
            channel.sock.close()

    def test_tampered_length_header_rejected(self):
        """只把长度字段改一位，也应当被发现 —— 因为长度不在 AAD 里，
        所以这里其实是让接收方读到截断的帧体而报错。两种结局都可接受，
        关键是**不能悄悄放行一份错误的明文**。"""
        self.pair.client.send({"kind": "submit", "code": "x"})
        frame = bytearray(self.pair.client_tap.take_written())
        frame[0] ^= 0x01
        channel = feed(bytes(frame), make_key(), protocol.DIRECTION_SERVER)
        try:
            with self.assertRaises(protocol.ProtocolError):
                channel.recv()
        finally:
            channel.sock.close()

    def test_wrong_key_rejected(self):
        self.pair.client.send({"kind": "submit", "code": "x"})
        frame = self.pair.client_tap.take_written()
        channel = feed(frame, b"\x00" * 32, protocol.DIRECTION_SERVER)
        try:
            with self.assertRaises(protocol.ProtocolError):
                channel.recv()
        finally:
            channel.sock.close()

    def test_short_frame_rejected(self):
        channel = feed(struct.pack("<I", 10) + b"x" * 10, make_key(),
                       protocol.DIRECTION_SERVER)
        try:
            with self.assertRaises(protocol.ProtocolError):
                channel.recv()
        finally:
            channel.sock.close()

    def test_expect_rejects_unexpected_kind(self):
        self.pair.server.send_kind(protocol.MessageKind.ERROR, message="boom")
        with self.assertRaises(protocol.ProtocolError) as caught:
            self.pair.client.expect(protocol.MessageKind.WELCOME)
        self.assertIn("error", str(caught.exception))

    def test_bad_direction_rejected(self):
        with self.assertRaises(ValueError):
            protocol.SecureChannel(self.pair.client_tap, b"\x00" * 32, 99)


class TestSecureChannelSuite(unittest.TestCase):
    """套件感知：同一套读写逻辑，按 ``suite`` 选不同 AEAD。

    默认套件（chaCha20-poly1305，32 字节密钥）的回归由上面的
    :class:`TestSecureChannel` 覆盖；这里专门钉死"换个 suite 仍能通、
    但跨 suite 不通"。
    """

    @staticmethod
    def _pair(suite: str) -> tuple[protocol.SecureChannel, protocol.SecureChannel]:
        left, right = socket.socketpair()
        aead = crypto.SUITES[suite].aead
        key = b"\x00" * crypto.aead_key_size(aead)
        client = protocol.SecureChannel(left, key, protocol.DIRECTION_CLIENT, suite)
        server = protocol.SecureChannel(right, key, protocol.DIRECTION_SERVER, suite)
        return client, server

    def test_sm4gcm_roundtrip(self):
        client, server = self._pair("x25519-sm4gcm")
        try:
            client.send_kind(protocol.MessageKind.FETCH, problem="P0001")
            kind, message, _ = server.recv_kind()
            self.assertEqual(kind, protocol.MessageKind.FETCH)
            self.assertEqual(message["problem"], "P0001")
        finally:
            client.sock.close()
            server.sock.close()

    def test_chacha20_explicit_roundtrip(self):
        client, server = self._pair("curve25519-chacha20")
        try:
            client.send({"kind": "ping"})
            message, _ = server.recv()
            self.assertEqual(message["kind"], "ping")
        finally:
            client.sock.close()
            server.sock.close()

    def test_cross_suite_rejected(self):
        """SM4-GCM 发的帧用 ChaCha20 通道收 → AEAD 不同 → 认证失败。"""
        frame = capture_frame("x25519-sm4gcm",
                               b"\x00" * crypto.aead_key_size("sm4-gcm"),
                               protocol.DIRECTION_CLIENT, {"kind": "ping"})
        receiver = feed(frame, b"\x00" * crypto.aead_key_size("chacha20-poly1305"),
                        protocol.DIRECTION_SERVER)
        try:
            with self.assertRaises(protocol.ProtocolError):
                receiver.recv()
        finally:
            receiver.sock.close()

    def test_unknown_suite_rejected(self):
        left, right = socket.socketpair()
        try:
            with self.assertRaises(ValueError):
                protocol.SecureChannel(left, b"\x00" * 32,
                                       protocol.DIRECTION_CLIENT, "no-such-suite")
        finally:
            left.close()
            right.close()

    def test_room_secret_binding_blocks_wrong_password(self):
        """X25519 本身不鉴权：任何人都能和服务端公钥完成 DH。

        但房间口令被绑进 HKDF 的 ``info`` 后，不知口令的一方派生出的密钥不同，
        于是 AUTH 解密必然失败 —— scrypt 白送的进场鉴权在这里恢复。
        """
        c_priv, c_pub = crypto.x25519_generate_keypair()
        s_priv, s_pub = crypto.x25519_generate_keypair()
        good_key = crypto.kex_session_key(
            "x25519-sm4gcm", private_key=c_priv, peer_public=s_pub,
            info=b"correct-room-secret")
        bad_key = crypto.kex_session_key(
            "x25519-sm4gcm", private_key=c_priv, peer_public=s_pub,
            info=b"wrong-guess")
        self.assertNotEqual(good_key, bad_key)

        frame = capture_frame("x25519-sm4gcm", good_key, protocol.DIRECTION_CLIENT,
                              {"kind": "auth", "device_id": "DEV00001"})
        receiver = feed(frame, bad_key, protocol.DIRECTION_SERVER, "x25519-sm4gcm")
        try:
            with self.assertRaises(protocol.ProtocolError):
                receiver.recv()
        finally:
            receiver.sock.close()


class TestSuiteNegotiation(unittest.TestCase):
    """``negotiate_suite`` 取服务器偏好序（国密优先）的首个交集。"""

    def test_prefers_server_order(self):
        client = ["x25519-sm4gcm", "curve25519-chacha20", "chacha20"]
        self.assertEqual(crypto.negotiate_suite(client), "x25519-sm4gcm")

    def test_client_lacking_national_suite_falls_to_chacha20(self):
        client = ["curve25519-chacha20", "chacha20"]
        self.assertEqual(crypto.negotiate_suite(client), "curve25519-chacha20")

    def test_only_common_is_chacha20(self):
        self.assertEqual(crypto.negotiate_suite(["chacha20"]), "chacha20")

    def test_no_intersection_returns_none(self):
        self.assertIsNone(crypto.negotiate_suite(["aes-gcm"]))


class TestBodyCodec(unittest.TestCase):
    """JSON 与二进制体的拼装。"""

    def test_roundtrip_without_blob(self):
        message, blob = protocol.decode_body(protocol.encode_body({"a": 1}))
        self.assertEqual(message, {"a": 1})
        self.assertEqual(blob, b"")

    def test_roundtrip_with_blob(self):
        message, blob = protocol.decode_body(
            protocol.encode_body({"a": "中文"}, b"\x00\x01\x02"))
        self.assertEqual(message["a"], "中文")
        self.assertEqual(blob, b"\x00\x01\x02")

    def test_rejects_non_object(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_body(struct.pack("<I", 4) + b"[1,2")

    def test_rejects_bad_json_length(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_body(struct.pack("<I", 999) + b"{}")

    def test_rejects_truncated_header(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_body(b"\x01\x02")

    def test_rejects_invalid_utf8(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_body(struct.pack("<I", 2) + b"\xff\xfe")


class TestFrameSizeLimits(unittest.TestCase):
    """超大帧必须被拒，否则一个恶意对端能让对方分配任意内存。"""

    def test_declared_oversize_rejected(self):
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("<I", protocol.MAX_FRAME_BYTES + 1))
            channel = protocol.SecureChannel(right, b"\x00" * 32,
                                             protocol.DIRECTION_SERVER)
            with self.assertRaises(protocol.ProtocolError) as caught:
                channel.recv()
            self.assertIn("超过上限", str(caught.exception))
        finally:
            left.close()
            right.close()

    def test_peer_closing_is_a_protocol_error(self):
        left, right = socket.socketpair()
        left.close()
        try:
            channel = protocol.SecureChannel(right, b"\x00" * 32,
                                             protocol.DIRECTION_SERVER)
            with self.assertRaises(protocol.ProtocolError):
                channel.recv()
        finally:
            right.close()


if __name__ == "__main__":
    unittest.main()
