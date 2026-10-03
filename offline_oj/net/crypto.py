"""ChaCha20-Poly1305（RFC 8439）与密钥派生。

为什么自己实现而不用现成库
--------------------------
本项目从第一天起就守着"零第三方依赖"的底线（PySide6 之外只有可选的 psutil），
因为它要能拷进 U 盘、解压即用，出现在装不了也不想装东西的机房里。为了一个
传输加密拖进一个几 MB 的编译扩展，代价和收益不成比例。

**但自己写密码学通常是错的。** 这里之所以敢写，是因为：

1. 算法本身很紧凑 —— ChaCha20 是纯 32 位字的加/异或/循环移位，Poly1305 是
   一次多项式求值，没有查表和分支，天然**常量时间**（不像 AES 需要提防缓存
   计时攻击），实现出错的空间比 AES 小得多；
2. 有**官方的逐字节测试向量**可对 —— 见 ``tests/test_net_crypto.py``，
   四组向量分别覆盖分组函数（§2.3.2）、加密流（§2.4.2）、Poly1305（§2.5.2）、
   AEAD 整体（§2.8.2）。任何一位写错都会当场露馅；
3. 面对的攻击者是**同网段的同学**，不是国家级对手。

威胁模型与边界（README 里也写了同样的边界，不要夸大它）
----------------------------------------------------
能挡住的：同网段抓包看到题面、看到别人提交的源码、看到测试点；重放旧帧；
伪造服务端。
挡不住的：拿到房间号的人可以进场；持有主机的人能看一切；流量分析
（谁在什么时候提交了多少字节）。这些都不在目标内。

**房间号只有 6 位数字**，它把攻击者的搜索空间压到了 10^6。scrypt 让每次尝试
要花约 350ms，在线爆破被按 IP 的握手限速挡住；但抓到一个握手帧之后可以离线爆破，
多核并行下是"小时"量级而不是"年"量级。所以房间号的定位是"分房间 + 挡住隔壁教室
的人"，**不是强凭据**；要真正抗离线爆破，请由老师另设房间口令
（:attr:`offline_oj.net.session.ExamSession.password`）。

性能
----
纯 Python，实测约 1~3 MB/s。题库题面是 KB 级文本，偶尔带几百 KB 的图，
单帧都在毫秒到百毫秒之间 —— 对这个场景完全够用。

非随机数使用策略
----------------
每帧用 12 字节**随机** nonce。RFC 8439 允许这样用（随机 nonce 下，
ChaCha20-Poly1305 在约 2^32 条消息内安全），一场测验的帧数远低于这个量级。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct

__all__ = [
    "KEY_SIZE", "NONCE_SIZE", "TAG_SIZE", "BLOCK_SIZE",
    "AuthenticationError", "UnsupportedSuiteError", "Suite",
    "chacha20_block", "chacha20_xor",
    "poly1305_mac",
    "aead_encrypt", "aead_decrypt",
    "derive_secret_key", "random_nonce", "random_salt", "SESSION_SALT_SIZE",
    # X25519 / HKDF（#12 前向保密握手）
    "x25519_generate_keypair", "x25519_public_key", "x25519_derive_shared_secret",
    "hkdf_sha256",
    # 国密（#13）
    "sm3", "sm4_block_encrypt", "sm4_gcm_encrypt", "sm4_gcm_decrypt",
    # 套件抽象（#12/#13 注册表 + 分派）
    "SUITES", "SUPPORTED_SUITES", "DEFAULT_SUITE", "negotiate_suite",
    "aead_encrypt_suite", "aead_decrypt_suite", "kex_session_key", "suite_digest",
    "aead_key_size",
]

MASK32 = 0xFFFFFFFF
BLOCK_SIZE = 64
KEY_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
SESSION_SALT_SIZE = 16

#: ChaCha20 的状态常量，就是 "expand 32-byte k" 的四个小端字
SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)

#: 每轮里 8 个 quarter-round 的操作数索引。顺序照 RFC 8439 §2.3.1：
#: 先 4 个列轮，再 4 个对角轮，合起来算一个 double round，共 10 次 = 20 轮。
_COLUMN_ROUNDS = ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15))
_DIAGONAL_ROUNDS = ((0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14))


class AuthenticationError(Exception):
    """AEAD 校验失败：密文被改动、密钥不对，或者根本是别人伪造的帧。

    与"格式错误"分开，是因为这两种情况的处置完全不同：格式错误是 bug，
    校验失败是安全事件，必须断开连接并在主机端留痕。
    """


def _rotl32(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & MASK32


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """ChaCha20 分组函数，返回 64 字节密钥流（RFC 8439 §2.3）。

    :param key: 32 字节密钥
    :param counter: 32 位块计数器（AEAD 里加密用 1，派生 Poly1305 密钥用 0）
    :param nonce: 12 字节 nonce
    """
    if len(key) != KEY_SIZE:
        raise ValueError(f"密钥必须是 {KEY_SIZE} 字节，收到 {len(key)}")
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"nonce 必须是 {NONCE_SIZE} 字节，收到 {len(nonce)}")

    initial = list(SIGMA)
    initial += struct.unpack("<8I", key)
    initial.append(counter & MASK32)
    initial += struct.unpack("<3I", nonce)

    work = initial[:]
    for _ in range(10):
        for a, b, c, d in _COLUMN_ROUNDS:
            work[a] = (work[a] + work[b]) & MASK32
            work[d] = _rotl32(work[d] ^ work[a], 16)
            work[c] = (work[c] + work[d]) & MASK32
            work[b] = _rotl32(work[b] ^ work[c], 12)
            work[a] = (work[a] + work[b]) & MASK32
            work[d] = _rotl32(work[d] ^ work[a], 8)
            work[c] = (work[c] + work[d]) & MASK32
            work[b] = _rotl32(work[b] ^ work[c], 7)
        for a, b, c, d in _DIAGONAL_ROUNDS:
            work[a] = (work[a] + work[b]) & MASK32
            work[d] = _rotl32(work[d] ^ work[a], 16)
            work[c] = (work[c] + work[d]) & MASK32
            work[b] = _rotl32(work[b] ^ work[c], 12)
            work[a] = (work[a] + work[b]) & MASK32
            work[d] = _rotl32(work[d] ^ work[a], 8)
            work[c] = (work[c] + work[d]) & MASK32
            work[b] = _rotl32(work[b] ^ work[c], 7)

    return struct.pack("<16I", *[(work[i] + initial[i]) & MASK32
                                 for i in range(16)])


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """ChaCha20 加解密（同一操作）。

    逐块异或是用**大整数异或**做的，不是逐字节循环 —— 后者在 Python 里慢一个
    数量级。64 字节的小整数异或开销可以忽略，这是纯 Python 实现还能跑到
    几 MB/s 的关键。
    """
    if not data:
        return b""
    out = bytearray(len(data))
    offset = 0
    index = counter & MASK32
    while offset < len(data):
        keystream = chacha20_block(key, index, nonce)
        chunk = data[offset:offset + BLOCK_SIZE]
        size = len(chunk)
        mixed = int.from_bytes(chunk, "little") ^ int.from_bytes(
            keystream[:size], "little")
        out[offset:offset + size] = mixed.to_bytes(size, "little")
        offset += size
        index = (index + 1) & MASK32
    return bytes(out)


#: Poly1305 的模数 2^130 - 5
_POLY_MODULUS = (1 << 130) - 5
#: r 的钳位掩码：r[3],r[7],r[11],r[15] 高 4 位清零，r[4],r[8],r[12] 低 2 位清零
_R_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def poly1305_mac(one_time_key: bytes, message: bytes) -> bytes:
    """Poly1305 一次性认证码（RFC 8439 §2.5），返回 16 字节标签。

    ``one_time_key`` 是 32 字节 = r(16) || s(16)。**绝不能用同一个 key 签两条
    消息** —— 那样 r 会重复，攻击者能解出 (acc+block)，进而伪造任意消息。
    这里每帧的 key 都从"密钥 + 帧 nonce"派生，天然不重复。
    """
    if len(one_time_key) != 32:
        raise ValueError("Poly1305 一次性密钥必须是 32 字节")

    r = int.from_bytes(one_time_key[:16], "little") & _R_CLAMP
    s = int.from_bytes(one_time_key[16:], "little")

    accumulator = 0
    for offset in range(0, len(message), 16):
        block = message[offset:offset + 16]
        # 末尾补一个 0x01 字节：等价于"块值 + 2^(8*块长)"
        number = int.from_bytes(block + b"\x01", "little")
        accumulator = ((accumulator + number) * r) % _POLY_MODULUS
    accumulator = (accumulator + s) & ((1 << 128) - 1)
    return accumulator.to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    """补零到 16 字节边界（已对齐则不加）。"""
    remainder = len(data) % 16
    return data + b"\x00" * (16 - remainder) if remainder else data


def _mac_data(aad: bytes, ciphertext: bytes) -> bytes:
    """AEAD 的 MAC 输入：AAD || pad || 密文 || pad || len(AAD) || len(密文)。"""
    return (b"".join((_pad16(aad), _pad16(ciphertext)))
            + struct.pack("<Q", len(aad)) + struct.pack("<Q", len(ciphertext)))


def _poly1305_key(key: bytes, nonce: bytes) -> bytes:
    """用 ChaCha20 分组函数（计数器 0）导出本次的一次性 Poly1305 密钥。"""
    return chacha20_block(key, 0, nonce)[:32]


def aead_encrypt(key: bytes, nonce: bytes, plaintext: bytes,
                 aad: bytes = b"") -> bytes:
    """AEAD_CHACHA20_POLY1305 加密，返回 ``密文 || 16 字节标签``。

    :param aad: 附加认证数据。它不加密但参与认证 —— 帧头（方向位、序号）
        放这里，这样**改动帧头一定会导致校验失败**，重放与反射就都挡掉了。
    """
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"nonce 必须是 {NONCE_SIZE} 字节")
    if not plaintext:
        # 空明文也要产出合法的认证标签，否则空体消息会绕过完整性校验
        return poly1305_mac(_poly1305_key(key, nonce), _mac_data(aad, b""))
    ciphertext = chacha20_xor(key, 1, nonce, plaintext)
    tag = poly1305_mac(_poly1305_key(key, nonce), _mac_data(aad, ciphertext))
    return ciphertext + tag


def aead_decrypt(key: bytes, nonce: bytes, payload: bytes,
                 aad: bytes = b"") -> bytes:
    """校验并解密。失败抛 :class:`AuthenticationError`，绝不放行任何明文。"""
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"nonce 必须是 {NONCE_SIZE} 字节")
    if len(payload) < TAG_SIZE:
        raise AuthenticationError(f"帧体不足 {TAG_SIZE} 字节，连标签都不完整")

    ciphertext, tag = payload[:-TAG_SIZE], payload[-TAG_SIZE:]
    expected = poly1305_mac(_poly1305_key(key, nonce), _mac_data(aad, ciphertext))
    # 常量时间比较：用 compare_digest 而不是 ==，避免通过响应时间逐字节爆破标签
    if not hmac.compare_digest(expected, tag):
        raise AuthenticationError("认证标签不匹配，帧可能被篡改或伪造")
    if not ciphertext:
        return b""
    return chacha20_xor(key, 1, nonce, ciphertext)


# ---------------------------------------------------------------------------
# 密钥派生
# ---------------------------------------------------------------------------

#: scrypt 参数。本机实测：n=2^15 约 350ms、峰值内存 32MB。
#: 这个代价放在**每次连接握手一次**，用户感受不到；而攻击者要拿它去爆破
#: 40 位的入场令牌，2^40 × 0.35s 是不可行的。maxmem 必须显式给够，
#: 否则 Python 会按默认的 32MB 上限直接拒绝 n=2^15。
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 128 * 1024 * 1024


def derive_secret_key(secret: str, salt: bytes, *,
                      n: int = SCRYPT_N, r: int = SCRYPT_R,
                      p: int = SCRYPT_P) -> bytes:
    """从一段**已归一化**的共享秘密派生出 32 字节会话密钥。

    **salt 必须每次连接由服务端重新随机生成。** 如果 salt 固定，那么同一房间
    每次都派生出同一个密钥，攻击者录下一次握手就能重放；随机 salt 让每次
    连接的密钥都不同，录下来的东西下次用不了。

    与"顺手帮你归一化"的做法相比，这里**刻意不做任何折叠**：房间口令区分大小写，
    把它统一成大写会白白削掉一大截熵。归一化的责任交给调用方
    （见 :func:`offline_oj.net.session.room_secret`），因为"什么算同一个房间号 /
    同一条口令"是业务规则，不是密码学规则；把它塞进这一层，两边一旦理解不一致
    就会表现成"密码明明对却连不上"。
    """
    if len(salt) < 8:
        raise ValueError("salt 太短，至少 8 字节")
    if not secret:
        raise ValueError("共享秘密不能为空")
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=KEY_SIZE, maxmem=SCRYPT_MAXMEM)


def random_nonce() -> bytes:
    """每帧一个新的随机 nonce。"""
    return os.urandom(NONCE_SIZE)


def random_salt() -> bytes:
    """每次连接一个新的随机 salt。"""
    return os.urandom(SESSION_SALT_SIZE)


# ===========================================================================
# 加密套件：Suite 抽象 + X25519 握手（#12）+ 国密 SM3/SM4（#13）
# ===========================================================================
#
# 设计目标：
#
# * **零第三方依赖**—— 整段是标准算法的纯 Python 实现，与 ChaCha20 同一个
#   底线（U 盘解压即用）。AES-256-GCM 需要 ``cryptography`` 的编译扩展会打破
#   这条底线，所以**不引入**，改用等价的国密 SM4-GCM 凑齐"另一套套件"。
# * **帧格式不变**—— 三者都是 ``[4字节长度][12字节nonce][密文||16字节tag]``
#   （nonce=12 / tag=16），``SecureChannel`` 只多一个 ``suite`` 参数，读写逻辑
#   不变，仅"用哪个 AEAD"由 ``suite`` 决定。
# * **前向保密**—— ``chacha20``(scrypt) 无前向保密；``curve25519-chacha20`` 与
#   ``x25519-sm4gcm`` 走 X25519 临时密钥对，握完即弃，录下流量也无法事后解密。
# * **进场鉴权**—— X25519 本身不能鉴权（任何人都能与服务端公钥完成 DH），所以
#   把**房间口令**作为 HKDF 的 ``info`` 绑进会话密钥派生：不知口令 → 密钥错 →
#   AUTH 解密失败，从而恢复 scrypt 白送的进场校验。
#
# 协议：自 v3 起**强制协商**，不再兼容 v2。HELLO 带 ``supported_suites`` +
# ``client_pub``，CHALLENGE 回 ``chosen_suite`` + ``server_pub``；服务器偏好序
# 见 ``SUPPORTED_SUITES``（国密优先）。威胁模型与 ``chacha20`` 一致
# （同网段同学、纯 Python 1~3 MB/s），多一层"算法被禁运"的自主可控应对。

import hmac as _hmac  # noqa: F401  （下方 hkdf 直接复用标准库 hmac）

from dataclasses import dataclass


class UnsupportedSuiteError(Exception):
    """客户端要了一个我们没实现的套件（或握手阶段协商不出公共套件）。"""


@dataclass(frozen=True)
class Suite:
    """一个加密套件三元组：密钥交换 / AEAD / 哈希。

    ``kex`` 决定握手怎么产出共享秘密；``aead`` 决定每帧怎么加解密；``hash``
    是套件自带的哈希（目前仅用于指纹/完整性兜底，HKDF 固定走 SHA-256）。
    """

    name: str
    kex: str          # "scrypt" | "x25519"
    aead: str         # "chacha20-poly1305" | "sm4-gcm"
    hash: str         # "sha256" | "sm3"


#: 全部已知套件。``chacha20`` 是历史默认、永远向后兼容；另两个是本期新增的
#: 前向保密 / 国密套件。
SUITES = {
    "chacha20": Suite("chacha20", "scrypt", "chacha20-poly1305", "sha256"),
    "curve25519-chacha20": Suite("curve25519-chacha20", "x25519",
                                 "chacha20-poly1305", "sha256"),
    "x25519-sm4gcm": Suite("x25519-sm4gcm", "x25519", "sm4-gcm", "sm3"),
}

#: 握手阶段实际会协商、也会写进 HELLO 的套件列表（顺序即"主机偏好"）。
#: 国赛语境下把自主可控的国密套件放首位。
SUPPORTED_SUITES = ("x25519-sm4gcm", "curve25519-chacha20", "chacha20")

#: 协商不出任何公共套件时的兜底（目前不会被握手层用到，仅作常量保留）。
DEFAULT_SUITE = "chacha20"


def negotiate_suite(client_supported: list[str] | tuple[str, ...],
                    server_supported: tuple[str, ...] = SUPPORTED_SUITES
                    ) -> str | None:
    """从客户端声明的能力里挑一个服务器也支持的套件。

    服务器偏好序即 ``server_supported`` 的顺序（当前国密优先）。取第一个
    两边都支持的；没有交集返回 ``None``，由握手层转成 REJECTED。
    """
    for suite in server_supported:
        if suite in client_supported:
            return suite
    return None


# ---------------------------------------------------------------------------
# X25519（RFC 7748）—— 纯 Python 蒙哥马利梯子
# ---------------------------------------------------------------------------

_X25519_P = (1 << 255) - 19
_X25519_A24 = 121665


def _x25519_cswap(swap: int, a: int, b: int) -> tuple[int, int]:
    """常量时间条件交换（swap 取 0 或 1；Python 本身不是常量时间，仅表意）。"""
    dummy = (a ^ b) & (-swap)
    a ^= dummy
    b ^= dummy
    return a, b


def _x25519_ladder(k: int, u: int) -> int:
    """蒙哥马利梯子：给定标量 ``k`` 与基点 u 坐标，返回共享点的 u 坐标。"""
    x_1 = u
    x_2, z_2 = 1, 0
    x_3, z_3 = x_1, 1
    swap = 0
    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        x_2, x_3 = _x25519_cswap(swap, x_2, x_3)
        z_2, z_3 = _x25519_cswap(swap, z_2, z_3)
        swap = k_t
        a = (x_2 + z_2) % _X25519_P
        aa = (a * a) % _X25519_P
        b = (x_2 - z_2) % _X25519_P
        bb = (b * b) % _X25519_P
        e = (aa - bb) % _X25519_P
        c = (x_3 + z_3) % _X25519_P
        d = (x_3 - z_3) % _X25519_P
        da = (d * a) % _X25519_P
        cb = (c * b) % _X25519_P
        x_3 = ((da + cb) % _X25519_P) ** 2 % _X25519_P
        z_3 = (x_1 * (((da - cb) % _X25519_P) ** 2 % _X25519_P)) % _X25519_P
        x_2 = (aa * bb) % _X25519_P
        z_2 = (e * ((aa + _X25519_A24 * e) % _X25519_P)) % _X25519_P
    x_2, x_3 = _x25519_cswap(swap, x_2, x_3)
    z_2, z_3 = _x25519_cswap(swap, z_2, z_3)
    return (x_2 * pow(z_2, _X25519_P - 2, _X25519_P)) % _X25519_P


def _x25519_clamp(scalar: bytes) -> bytes:
    """RFC 7748 标量钳位：bit0..1 清零、bit254 置 1（消掉低位的弱比特）。"""
    sc = bytearray(scalar)
    sc[0] &= 248
    sc[31] &= 127
    sc[31] |= 64
    return bytes(sc)


def x25519_scalar_mult(scalar: bytes, point: bytes) -> bytes:
    """X25519 标量乘法，返回 32 字节共享密钥（u 坐标）。"""
    if len(scalar) != 32 or len(point) != 32:
        raise ValueError("X25519 的 scalar 与 point 都必须是 32 字节")
    k = int.from_bytes(_x25519_clamp(scalar), "little")
    u = int.from_bytes(point, "little")
    return _x25519_ladder(k, u).to_bytes(32, "little")


def x25519_generate_keypair() -> tuple[bytes, bytes]:
    """生成临时密钥对，返回 ``(私钥, 公钥)``。私钥直接 ``os.urandom`` 后钳位。"""
    private = _x25519_clamp(os.urandom(32))
    return private, x25519_public_key(private)


def x25519_public_key(private_key: bytes) -> bytes:
    """从私钥推出公钥（基点 u = 9）。"""
    return x25519_scalar_mult(private_key, b"\x09" + b"\x00" * 31)


def x25519_derive_shared_secret(private_key: bytes, peer_public: bytes) -> bytes:
    """两方各用自己的私钥与对方的公钥算，结果应当一致（这就是会话密钥材料）。"""
    return x25519_scalar_mult(private_key, peer_public)


# ---------------------------------------------------------------------------
# HKDF-SHA256（RFC 5869）—— 把 X25519 的原始共享秘密提炼成定长会话密钥
# ---------------------------------------------------------------------------

def hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """提取-扩展两步。X25519 直接算出的共享秘密分布不均（高位恒 0），
    必须经过 HKDF 再当密钥用，否则 ChaCha20/SM4 看到的密钥熵不够。"""
    prk = _hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    t = b""
    i = 0
    while len(out) < length:
        i += 1
        t = _hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
    return out[:length]


# ---------------------------------------------------------------------------
# SM3（GB/T 0004-2012）—— 国密哈希
# ---------------------------------------------------------------------------

#: GB/T 0004-2012 初始链接变量（IV）。注意第二个字是 0x4914B2B9，
#: 不是容易记错的 0x4914B2B1——差这一个字，空串/“abc” 的摘要就全错。
_SM3_IV = (0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
           0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E)


def _sm3_ff(x: int, y: int, z: int, j: int) -> int:
    return x ^ y ^ z if j < 16 else (x & y) | (x & z) | (y & z)


def _sm3_gg(x: int, y: int, z: int, j: int) -> int:
    return x ^ y ^ z if j < 16 else (x & y) | ((~x & 0xFFFFFFFF) & z)


def _sm3_t(j: int) -> int:
    return 0x79CC4519 if j < 16 else 0x7A879D8A


def _sm3_p0(x: int) -> int:
    return x ^ _rotl32(x, 9) ^ _rotl32(x, 17)


def _sm3_p1(x: int) -> int:
    return x ^ _rotl32(x, 15) ^ _rotl32(x, 23)


def sm3(data: bytes) -> bytes:
    """SM3 哈希，返回 32 字节摘要。"""
    m = bytearray(data)
    m.append(0x80)
    while (len(m) * 8 + 64) % 512 != 0:
        m.append(0)
    m += (len(data) * 8).to_bytes(8, "big")

    v = list(_SM3_IV)
    for block_start in range(0, len(m), 64):
        block = m[block_start:block_start + 64]
        w = [int.from_bytes(block[4 * j:4 * j + 4], "big") for j in range(16)]
        for j in range(16, 68):
            w.append(_sm3_p1(w[j - 16] ^ w[j - 9] ^ _rotl32(w[j - 3], 15))
                     ^ _rotl32(w[j - 13], 7) ^ w[j - 6])
        wp = [w[j] ^ w[j + 4] for j in range(64)]

        a, b, c, d, e, f, g, h = v
        for j in range(64):
            t = _sm3_t(j)
            ss1 = _rotl32((_rotl32(a, 12) + e + _rotl32(t, j % 32)) & MASK32, 7)
            ss2 = ss1 ^ _rotl32(a, 12)
            tt1 = (_sm3_ff(a, b, c, j) + d + ss2 + wp[j]) & MASK32
            tt2 = (_sm3_gg(e, f, g, j) + h + ss1 + w[j]) & MASK32
            d, c, b, a = c, _rotl32(b, 9), a, tt1
            h, g, f, e = g, _rotl32(f, 19), e, _sm3_p0(tt2)
        v = [(x ^ y) & MASK32 for x, y in zip(v, (a, b, c, d, e, f, g, h))]
    return b"".join(word.to_bytes(4, "big") for word in v)


# ---------------------------------------------------------------------------
# SM4（GB/T 0002-2012）—— 国密分组密码 + SM4-GCM
# ---------------------------------------------------------------------------

_SM4_SBOX = bytes([
    0xD6, 0x90, 0xE9, 0xFE, 0xCC, 0xE1, 0x3D, 0xB7, 0x16, 0xB6, 0x14, 0xC2, 0x28, 0xFB, 0x2C, 0x05,
    0x2B, 0x67, 0x9A, 0x76, 0x2A, 0xBE, 0x04, 0xC3, 0xAA, 0x44, 0x13, 0x26, 0x49, 0x86, 0x06, 0x99,
    0x9C, 0x42, 0x50, 0xF4, 0x91, 0xEF, 0x98, 0x7A, 0x33, 0x54, 0x0B, 0x43, 0xED, 0xCF, 0xAC, 0x62,
    0xE4, 0xB3, 0x1C, 0xA9, 0xC9, 0x08, 0xE8, 0x95, 0x80, 0xDF, 0x94, 0xFA, 0x75, 0x8F, 0x3F, 0xA6,
    0x47, 0x07, 0xA7, 0xFC, 0xF3, 0x73, 0x17, 0xBA, 0x83, 0x59, 0x3C, 0x19, 0xE6, 0x85, 0x4F, 0xA8,
    0x68, 0x6B, 0x81, 0xB2, 0x71, 0x64, 0xDA, 0x8B, 0xF8, 0xEB, 0x0F, 0x4B, 0x70, 0x56, 0x9D, 0x35,
    0x1E, 0x24, 0x0E, 0x5E, 0x63, 0x58, 0xD1, 0xA2, 0x25, 0x22, 0x7C, 0x3B, 0x01, 0x21, 0x78, 0x87,
    0xD4, 0x00, 0x46, 0x57, 0x9F, 0xD3, 0x27, 0x52, 0x4C, 0x36, 0x02, 0xE7, 0xA0, 0xC4, 0xC8, 0x9E,
    0xEA, 0xBF, 0x8A, 0xD2, 0x40, 0xC7, 0x38, 0xB5, 0xA3, 0xF7, 0xF2, 0xCE, 0xF9, 0x61, 0x15, 0xA1,
    0xE0, 0xAE, 0x5D, 0xA4, 0x9B, 0x34, 0x1A, 0x55, 0xAD, 0x93, 0x32, 0x30, 0xF5, 0x8C, 0xB1, 0xE3,
    0x1D, 0xF6, 0xE2, 0x2E, 0x82, 0x66, 0xCA, 0x60, 0xC0, 0x29, 0x23, 0xAB, 0x0D, 0x53, 0x4E, 0x6F,
    0xD5, 0xDB, 0x37, 0x45, 0xDE, 0xFD, 0x8E, 0x2F, 0x03, 0xFF, 0x6A, 0x72, 0x6D, 0x6C, 0x5B, 0x51,
    0x8D, 0x1B, 0xAF, 0x92, 0xBB, 0xDD, 0xBC, 0x7F, 0x11, 0xD9, 0x5C, 0x41, 0x1F, 0x10, 0x5A, 0xD8,
    0x0A, 0xC1, 0x31, 0x88, 0xA5, 0xCD, 0x7B, 0xBD, 0x2D, 0x74, 0xD0, 0x12, 0xB8, 0xE5, 0xB4, 0xB0,
    0x89, 0x69, 0x97, 0x4A, 0x0C, 0x96, 0x77, 0x7E, 0x65, 0xB9, 0xF1, 0x09, 0xC5, 0x6E, 0xC6, 0x84,
    0x18, 0xF0, 0x7D, 0xEC, 0x3A, 0xDC, 0x4D, 0x20, 0x79, 0xEE, 0x5F, 0x3E, 0xD7, 0xCB, 0x39, 0x48,
])

_SM4_FK = (0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC)

#: 轮密钥常数 CK_i。第 j 个字节 = (7·(4i+j)) mod 256，再大端拼成 32 位字。
#: 即 CK_0 = 0x00070E15、CK_1 = 0x1C232A31 …… 不是容易写错的 (4i+1,4i+2,4i+3,4i+4)。
_SM4_CK = [
    0x00070E15, 0x1C232A31, 0x383F464D, 0x545B6269,
    0x70777E85, 0x8C939AA1, 0xA8AFB6BD, 0xC4CBD2D9,
    0xE0E7EEF5, 0xFC030A11, 0x181F262D, 0x343B4249,
    0x50575E65, 0x6C737A81, 0x888F969D, 0xA4ABB2B9,
    0xC0C7CED5, 0xDCE3EAF1, 0xF8FF060D, 0x141B2229,
    0x30373E45, 0x4C535A61, 0x686F767D, 0x848B9299,
    0xA0A7AEB5, 0xBCC3CAD1, 0xD8DFE6ED, 0xF4FB0209,
    0x10171E25, 0x2C333A41, 0x484F565D, 0x646B7279,
]


def _sm4_tau(a: int) -> int:
    out = 0
    for i in range(4):
        out |= _SM4_SBOX[(a >> (8 * i)) & 0xFF] << (8 * i)
    return out


def _sm4_l(a: int) -> int:
    return a ^ _rotl32(a, 2) ^ _rotl32(a, 10) ^ _rotl32(a, 18) ^ _rotl32(a, 24)


def _sm4_l_prime(a: int) -> int:
    return a ^ _rotl32(a, 13) ^ _rotl32(a, 23)


def _sm4_round_keys(key: bytes) -> list[int]:
    k = [int.from_bytes(key[4 * i:4 * i + 4], "big") for i in range(4)]
    k[0] ^= _SM4_FK[0]
    k[1] ^= _SM4_FK[1]
    k[2] ^= _SM4_FK[2]
    k[3] ^= _SM4_FK[3]
    rk: list[int] = []
    for i in range(32):
        x = k[1] ^ k[2] ^ k[3] ^ _SM4_CK[i]
        t = _sm4_l_prime(_sm4_tau(x))
        k.append(k[0] ^ t)
        rk.append(k[4])
        k.pop(0)
    return rk


def _sm4_block(key: bytes, block: bytes, rk: list[int] | None = None) -> bytes:
    """SM4 单块加/解密（轮密钥逆序即解密）。"""
    if rk is None:
        rk = _sm4_round_keys(key)
    x = [int.from_bytes(block[4 * i:4 * i + 4], "big") for i in range(4)]
    for i in range(32):
        t = _sm4_l(_sm4_tau(x[1] ^ x[2] ^ x[3] ^ rk[i]))
        x.append(x[0] ^ t)
        x.pop(0)
    return b"".join(word.to_bytes(4, "big") for word in (x[3], x[2], x[1], x[0]))


def sm4_block_encrypt(key: bytes, block: bytes) -> bytes:
    """SM4 加密一个 16 字节块（GCM 的底层分组原语）。"""
    if len(key) != 16 or len(block) != 16:
        raise ValueError("SM4 的密钥与块都必须是 16 字节")
    return _sm4_block(key, block)


def _sm4_ctr(key: bytes, initial_counter: bytes, data: bytes) -> bytes:
    out = bytearray()
    ctr = initial_counter
    for i in range(0, len(data), 16):
        ks = sm4_block_encrypt(key, ctr)
        chunk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, ks))
        ctr = ctr[:12] + ((int.from_bytes(ctr[12:], "big") + 1) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(out)


def _gf_mult(x: int, y: int) -> int:
    """GHASH 的 GF(2^128) 乘法，约化多项式 R = 0xe1<<120。"""
    r = 0xE1 << 120
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ r
        else:
            v >>= 1
    return z


def _ghash(h: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    """GHASH（GF(2^128) 上的多项式求值）。

    **注意**输入是 ``pad16(aad) || pad16(ciphertext) || len(aad) || len(ciphertext)``，
    每个域只出现一次；先前写成 ``aad + _pad16(aad)`` 会把 AAD 喂两遍，
    导致带 AAD 的向量（RFC 8998 A.1）标签算错，而空 AAD 的零密钥向量恰好
    ``b"" + b"" == b""`` 才没暴露。
    """
    h_int = int.from_bytes(h, "big")
    data = (_pad16(aad) + _pad16(ciphertext)
            + struct.pack(">Q", len(aad) * 8) + struct.pack(">Q", len(ciphertext) * 8))
    y = 0
    for i in range(0, len(data), 16):
        block = int.from_bytes(data[i:i + 16], "big")
        y = _gf_mult(y ^ block, h_int)
    return y.to_bytes(16, "big")


def sm4_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes,
                    aad: bytes = b"") -> bytes:
    """SM4-GCM 加密，返回 ``密文 || 16 字节 tag``。nonce 取 12 字节（与帧格式一致）。"""
    if len(key) != 16:
        raise ValueError("SM4-GCM 的密钥必须是 16 字节")
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"SM4-GCM 的 nonce 必须是 {NONCE_SIZE} 字节")
    h = sm4_block_encrypt(key, b"\x00" * 16)
    j0 = nonce + b"\x00\x00\x00\x01"
    ciphertext = _sm4_ctr(key, _inc32(j0), plaintext)
    tag = int.from_bytes(_ghash(h, aad, ciphertext), "big") \
        ^ int.from_bytes(sm4_block_encrypt(key, j0), "big")
    return ciphertext + tag.to_bytes(16, "big")


def sm4_gcm_decrypt(key: bytes, nonce: bytes, payload: bytes,
                    aad: bytes = b"") -> bytes:
    """校验并解密。失败抛 :class:`AuthenticationError`。"""
    if len(key) != 16:
        raise ValueError("SM4-GCM 的密钥必须是 16 字节")
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"SM4-GCM 的 nonce 必须是 {NONCE_SIZE} 字节")
    if len(payload) < TAG_SIZE:
        raise AuthenticationError("SM4-GCM 帧体不足一个标签")
    ciphertext, tag = payload[:-TAG_SIZE], payload[-TAG_SIZE:]
    h = sm4_block_encrypt(key, b"\x00" * 16)
    j0 = nonce + b"\x00\x00\x00\x01"
    expected = int.from_bytes(_ghash(h, aad, ciphertext), "big") \
        ^ int.from_bytes(sm4_block_encrypt(key, j0), "big")
    if not _hmac.compare_digest(expected.to_bytes(16, "big"), tag):
        raise AuthenticationError("SM4-GCM 认证标签不匹配，帧可能被篡改或伪造")
    return _sm4_ctr(key, _inc32(j0), ciphertext)


def _inc32(ctr: bytes) -> bytes:
    """GCM 计数器：只增最右 32 位。"""
    return ctr[:12] + ((int.from_bytes(ctr[12:], "big") + 1) & 0xFFFFFFFF).to_bytes(4, "big")


# ---------------------------------------------------------------------------
# 套件分派：让上层只传 suite 名，不必关心底层调哪个算法
# ---------------------------------------------------------------------------

def aead_key_size(aead: str) -> int:
    """该 AEAD 需要的会话密钥字节数（ChaCha20-Poly1305=32，SM4-GCM=16）。"""
    if aead == "chacha20-poly1305":
        return 32
    if aead == "sm4-gcm":
        return 16
    raise UnsupportedSuiteError(f"未知 AEAD：{aead}")


def aead_encrypt_suite(suite: str, key: bytes, nonce: bytes,
                       plaintext: bytes, aad: bytes = b"") -> bytes:
    """按套件名选 AEAD 加密。``chacha20`` 走现成的 ChaCha20-Poly1305，
    ``x25519-sm4gcm`` 走 SM4-GCM；其余套件的 aead 落到这里还是同名的ChaCha。"""
    name = SUITES[suite].aead
    if name == "chacha20-poly1305":
        return aead_encrypt(key, nonce, plaintext, aad)
    if name == "sm4-gcm":
        return sm4_gcm_encrypt(key, nonce, plaintext, aad)
    raise UnsupportedSuiteError(f"未知 AEAD：{name}")


def aead_decrypt_suite(suite: str, key: bytes, nonce: bytes,
                       payload: bytes, aad: bytes = b"") -> bytes:
    name = SUITES[suite].aead
    if name == "chacha20-poly1305":
        return aead_decrypt(key, nonce, payload, aad)
    if name == "sm4-gcm":
        return sm4_gcm_decrypt(key, nonce, payload, aad)
    raise UnsupportedSuiteError(f"未知 AEAD：{name}")


def kex_session_key(suite: str, *, secret: str | None = None,
                    salt: bytes | None = None,
                    private_key: bytes | None = None,
                    peer_public: bytes | None = None,
                    info: bytes = b"offline-oj v3 session") -> bytes:
    """从握手材料产出会话密钥（长度随 AEAD 走：ChaCha20=32，SM4-GCM=16）。

    * ``scrypt`` 套件：用 ``secret``（已归一化的房间口令）+ ``salt`` 走 scrypt（旧路径）；
    * ``x25519`` 套件：用双方临时密钥对算出原始共享秘密，再 HKDF-SHA256 提炼。

      **安全：必须把房间口令作为 ``info`` 传入**（默认 ``b"offline-oj v3 session"``
      仅用于测试）。HKDF 的 info 参与密钥派生，不知口令 → 派生出的密钥错 →
      后续 AUTH 解密失败，从而恢复 scrypt 白送的进场鉴权。单独一个 X25519 共享
      秘密本身不鉴权（任何人都能和服务端公钥完成 DH）。
    """
    kex = SUITES[suite].kex
    if kex == "scrypt":
        if not secret or not salt:
            raise ValueError("scrypt 套件需要 secret 与 salt")
        return derive_secret_key(secret, salt)
    if kex == "x25519":
        if not private_key or not peer_public:
            raise ValueError("x25519 套件需要 private_key 与 peer_public")
        shared = x25519_derive_shared_secret(private_key, peer_public)
        # 密钥长度跟着 AEAD 走：ChaCha20-Poly1305 要 32 字节，
        # SM4-GCM 只要 16 字节——之前这里写死 KEY_SIZE=32，导致
        # x25519-sm4gcm 套件在派发层就抛 ValueError。
        length = aead_key_size(SUITES[suite].aead)
        return hkdf_sha256(b"offline-oj v3 handshake", shared, info, length)
    raise UnsupportedSuiteError(f"未知密钥交换：{kex}")


def suite_digest(suite: str, data: bytes) -> bytes:
    """套件自带的哈希（``sha256`` 或 ``sm3``）。目前用于握手签名/指纹。"""
    which = SUITES[suite].hash
    if which == "sha256":
        return hashlib.sha256(data).digest()
    if which == "sm3":
        return sm3(data)
    raise UnsupportedSuiteError(f"未知哈希：{which}")
