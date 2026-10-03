"""加密帧协议。

线上格式只有两种，靠握手阶段区分，服务端自己知道当前处在哪一段：

**明文握手帧**（只出现在头两条消息上）::

    [4 字节 长度][JSON]

**加密帧**（握手之后的一切）::

    [4 字节 长度][12 字节 nonce][密文 || 16 字节标签]
    长度 = 12 + len(密文) + 16

加密帧的明文部分又是一层结构，用来把"结构化元数据"和"二进制大块"分开::

    [4 字节 JSON 长度][JSON][二进制体（可选）]

分开的理由：题面里的插图如果塞进 JSON 得先 base64，体积膨胀 33%，
而纯 Python 的 ChaCha20 只有 0.7 MB/s，白白多花三分之一时间是没必要的。

为什么要序列号与方向位
----------------------
两者都放进 **AAD**（附加认证数据）。AAD 不加密但参与认证，所以改动帧头一定
导致校验失败。于是：

* **重放** —— 序号单调递增，把旧帧原样重发时序号对不上，AAD 不匹配，直接拒绝；
* **反射** —— 把发给自己方向上的帧原样丢回去，方向位不同，AAD 不匹配，同样拒绝。

如果只把序号放在明文头里而不参与 AAD，攻击者就能改序号；如果只加密不认证，
攻击者就能随便构造帧。**认证必须覆盖帧头**，这是这个协议里唯一容易写错的地方。
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

from . import crypto

__all__ = [
    "ProtocolError", "HandshakeRejected", "MessageKind",
    "DIRECTION_CLIENT", "DIRECTION_SERVER", "MAX_FRAME_BYTES",
    "PlainChannel", "SecureChannel", "encode_body", "decode_body",
]

#: 单帧上限。题面可能带图，给到 16MB；再大就说明不是正常用途了。
MAX_FRAME_BYTES = 16 * 1024 * 1024

#: 方向位，参与 AAD 的计算，用于阻断反射攻击
DIRECTION_CLIENT = 1     # 客户端 → 服务端
DIRECTION_SERVER = 2     # 服务端 → 客户端

_HEADER = struct.Struct("<I")
_JSON_LEN = struct.Struct("<I")
_SEQ = struct.Struct("<Q")


class ProtocolError(Exception):
    """帧格式不对、JSON 解析失败、消息类型未知 —— 都属于"对方没按协议说话"。"""


class HandshakeRejected(Exception):
    """握手被拒（令牌不对、用户名冲突、测验未开始或已结束）。

    与 :class:`ProtocolError` 分开，因为这条是**业务上的正常拒绝**，
    要原样转达给用户；前者是 bug 或攻击，应当断开连接并记日志。
    """


class MessageKind:
    """消息类型常量。用字符串而不是数字，抓包调试时可读。"""

    # --- 握手阶段（明文） ---
    HELLO = "hello"                  # C→S: 房间号 + 设备 ID + 用户名
    CHALLENGE = "challenge"          # S→C: 本次连接的随机 salt + 测验元信息
    AUTH = "auth"                    # C→S: （加密）证明持有房间号（及房间口令）
    WELCOME = "welcome"              # S→C: （加密）鉴权通过
    REJECTED = "rejected"            # S→C: 鉴权不通过或测验不可用

    # --- 正常阶段（全部加密） ---
    FETCH = "fetch"                  # C→S: 拉题面
    PROBLEMS = "problems"            # S→C: 题面（只含样例测试点）
    SUBMIT = "submit"                # C→S: 提交源码
    VERDICT = "verdict"              # S→C: 判定结果
    LEADERBOARD = "leaderboard"      # S→C: 排名快照（考试模式封榜期间为空表）
    EXAM = "exam"                    # S→C: 测验状态与剩余时间
    START = "start"                  # S→C: 开考指令（载荷与 EXAM 同构，多一个 reason）
    COLLECT = "collect"              # S→C: 收卷指令（到点强制收卷 / 老师提前收卷）
    LOCK = "lock"                    # S→C: 离场锁屏 / 解除（学生按键或老师远程）
    UNLOCK = "unlock"                # C→S: 请求解锁（带口令，**校验在主机**）
    UNLOCKED = "unlocked"            # S→C: 解锁结果（ok + 给用户看的话）
    PING = "ping"                    # C→S
    PONG = "pong"                    # S→C
    ERROR = "error"                  # S→C: 业务错误（如题目不存在）


# ---------------------------------------------------------------------------
# 底层收发
# ---------------------------------------------------------------------------


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """收满 ``count`` 字节；对端正常关闭返回空串。

    必须循环收 —— TCP 是字节流，一次 ``recv`` 可能只回一半。早期版本直接
    ``sock.recv(count)``，在局域网小包下几乎总是对的，一旦题面带图就会
    随机解析失败，且现象是"偶尔崩"，极难查。
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            if remaining == count:
                return b""
            raise ProtocolError(f"连接在读满 {count} 字节前就断了"
                                f"（已读 {count - remaining} 字节）")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_all(sock: socket.socket, data: bytes) -> None:
    sock.sendall(data)


# ---------------------------------------------------------------------------
# 明文体编解码（JSON + 可选二进制）
# ---------------------------------------------------------------------------


def encode_body(message: dict[str, Any], blob: bytes = b"") -> bytes:
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    raw = text.encode("utf-8")
    return _JSON_LEN.pack(len(raw)) + raw + blob


def decode_body(payload: bytes) -> tuple[dict[str, Any], bytes]:
    if len(payload) < _JSON_LEN.size:
        raise ProtocolError("帧体太短，连 JSON 长度字段都没有")
    (json_len,) = _JSON_LEN.unpack_from(payload, 0)
    if json_len > len(payload) - _JSON_LEN.size:
        raise ProtocolError("JSON 长度字段超出帧体范围")
    start = _JSON_LEN.size
    try:
        message = json.loads(payload[start:start + json_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"帧体 JSON 解析失败: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("帧体 JSON 必须是对象")
    return message, payload[start + json_len:]


# ---------------------------------------------------------------------------
# 明文握手通道
# ---------------------------------------------------------------------------


class PlainChannel:
    """握手阶段用的明文帧通道。**只在拿到会话密钥之前使用。**

    这里没有加密，所以对帧内容有硬约束：

    * 明文 HELLO 帧里**只有房间号的单向索引**（``sha256(房间号+口令)`` 前 16 位）
      与协议版本。房间号是凭据，绝不能明文上线 —— 一旦上线，同网段抓一次包就
      等于拿到了钥匙，后面所有加密都成了摆设；
    * 用户名与设备 ID 都放在**加密的 AUTH 帧**里。它们在旧版是明文进场的，
      现在顺手收进加密段：主机不需要提前知道它们才能派生密钥，
      所以没有任何理由让它们裸奔；
    * 服务端对握手要有速率限制，否则可以被用来做房间号的在线爆破。
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def send(self, message: dict[str, Any], blob: bytes = b"") -> None:
        payload = encode_body(message, blob)
        if len(payload) > MAX_FRAME_BYTES:
            raise ProtocolError(f"帧过大: {len(payload)} 字节")
        _send_all(self.sock, _HEADER.pack(len(payload)) + payload)

    def recv(self) -> tuple[dict[str, Any], bytes]:
        header = _recv_exactly(self.sock, _HEADER.size)
        if not header:
            raise ProtocolError("对端在发送帧头前就关闭了连接")
        (size,) = _HEADER.unpack(header)
        if size > MAX_FRAME_BYTES:
            raise ProtocolError(f"声明的帧长 {size} 超过上限 {MAX_FRAME_BYTES}")
        payload = _recv_exactly(self.sock, size)
        if not payload:
            raise ProtocolError("对端在发送帧体时断开了连接")
        return decode_body(payload)


# ---------------------------------------------------------------------------
# 加密通道
# ---------------------------------------------------------------------------


class SecureChannel:
    """握手之后的加密帧通道。

    收发各自维护一个单调递增的序号。序号从 0 开始、逐帧加一，并且**参与 AAD
    认证**，所以任何乱序、重放、丢帧都会被识别为认证失败而不是被悄悄接受。
    """

    def __init__(self, sock: socket.socket, key: bytes, direction: int,
                 suite: str = "chacha20") -> None:
        if direction not in (DIRECTION_CLIENT, DIRECTION_SERVER):
            raise ValueError(f"方向位非法: {direction}")
        if suite not in crypto.SUITES:
            raise ValueError(f"未知加密套件: {suite}")
        self.sock = sock
        self.key = key
        self.direction = direction
        self.suite = suite
        self.send_seq = 0
        self.recv_seq = 0

    # ---- 收发 ---------------------------------------------------------------

    def send(self, message: dict[str, Any], blob: bytes = b"") -> None:
        plaintext = encode_body(message, blob)
        nonce = crypto.random_nonce()
        aad = self._aad(self.direction, self.send_seq)
        sealed = crypto.aead_encrypt_suite(self.suite, self.key, nonce,
                                           plaintext, aad)
        payload = nonce + sealed
        if len(payload) > MAX_FRAME_BYTES:
            raise ProtocolError(f"加密后帧过大: {len(payload)} 字节")
        _send_all(self.sock, _HEADER.pack(len(payload)) + payload)
        self.send_seq += 1

    def recv(self) -> tuple[dict[str, Any], bytes]:
        header = _recv_exactly(self.sock, _HEADER.size)
        if not header:
            raise ProtocolError("对端已关闭连接")
        (size,) = _HEADER.unpack(header)
        if size > MAX_FRAME_BYTES:
            raise ProtocolError(f"声明的帧长 {size} 超过上限 {MAX_FRAME_BYTES}")
        if size < crypto.NONCE_SIZE + crypto.TAG_SIZE:
            raise ProtocolError(f"加密帧太短: {size} 字节")
        payload = _recv_exactly(self.sock, size)
        if not payload:
            raise ProtocolError("对端在发送帧体时断开了连接")

        nonce = payload[:crypto.NONCE_SIZE]
        sealed = payload[crypto.NONCE_SIZE:]
        # 对端用的是它自己方向上的方向位，这里必须按对端方向算 AAD
        aad = self._aad(self._opposite(), self.recv_seq)
        try:
            plaintext = crypto.aead_decrypt_suite(self.suite, self.key, nonce,
                                                  sealed, aad)
        except crypto.AuthenticationError as exc:
            raise ProtocolError(
                f"第 {self.recv_seq} 帧认证失败（可能是重放、篡改或密钥不匹配）"
            ) from exc
        self.recv_seq += 1
        return decode_body(plaintext)

    # ---- 便利方法 -----------------------------------------------------------

    def send_kind(self, kind: str, **fields: Any) -> None:
        self.send({"kind": kind, **fields})

    def recv_kind(self) -> tuple[str, dict[str, Any], bytes]:
        message, blob = self.recv()
        kind = message.get("kind")
        if not isinstance(kind, str):
            raise ProtocolError("帧里没有 kind 字段")
        return kind, message, blob

    def expect(self, kind: str) -> tuple[dict[str, Any], bytes]:
        got, message, blob = self.recv_kind()
        if got != kind:
            raise ProtocolError(f"期望 {kind} 帧，收到 {got} 帧")
        return message, blob

    # ---- 内部 ---------------------------------------------------------------

    def _opposite(self) -> int:
        return (DIRECTION_SERVER if self.direction == DIRECTION_CLIENT
                else DIRECTION_CLIENT)

    @staticmethod
    def _aad(direction: int, seq: int) -> bytes:
        return bytes([direction]) + _SEQ.pack(seq)
