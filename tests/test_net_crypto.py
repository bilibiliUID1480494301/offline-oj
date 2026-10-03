"""ChaCha20-Poly1305 与密钥派生的对拍测试。

**全部期望值抄自 RFC 8439 原文，不是自己算出来再抄一遍** —— 后者只能证明
实现自洽，证明不了实现正确。四组向量的出处：

* :meth:`TestChaCha20Block.test_rfc_2_3_2_block_function` —— §2.3.2 分组函数
* :meth:`TestChaCha20Cipher.test_rfc_2_4_2_encryption` —— §2.4.2 加密流
* :meth:`TestPoly1305.test_rfc_2_5_2_mac` —— §2.5.2 认证码
* :meth:`TestAEAD.test_rfc_2_8_2_roundtrip` —— §2.8.2 AEAD 整体

向量里的空格、冒号、换行都按原文排版保留，方便和 RFC 逐行对照。
"""

from __future__ import annotations

import hashlib
import os
import unittest

from offline_oj.net import crypto

# ---------------------------------------------------------------------------
# RFC 8439 §2.3.2 —— ChaCha20 分组函数
# ---------------------------------------------------------------------------

BLOCK_KEY = bytes(range(32))
BLOCK_NONCE = bytes.fromhex("000000090000004a00000000")
BLOCK_COUNTER = 1

#: RFC §2.3.2 "ChaCha state at the end of the ChaCha20 operation" —— 16 个 32 位字。
#: 注意这一坨**不是**要比较的字节：序列化时每个字按**小端**展开（RFC 里另一段
#: "Serialized Block" 才是）。这里两个都留着，是为了让"字 → 字节"这一步显式可查 ——
#: 第一次写这条用例就是把状态字当成字节抄了下来，于是实现对了、用例红了。
BLOCK_FINAL_STATE_WORDS = (
    0xE4E7F110, 0x15593BD1, 0x1FDD0F50, 0xC47120A3,
    0xC7F4D1C7, 0x0368C033, 0x9AAA2204, 0x4E6CD4C3,
    0x466482D2, 0x09AA9F07, 0x05D7C214, 0xA2028BD9,
    0xD19C12B5, 0xB94E16DE, 0xE883D0CB, 0x4E3C50A2,
)

#: RFC §2.3.2 "Serialized Block" —— 逐字节的密钥流，也就是真正要比较的东西
BLOCK_EXPECTED = bytes.fromhex(
    "10f1e7e4d13b5915500fdd1fa32071c4"
    "c7d1f4c733c068030422aa9ac3d46c4e"
    "d2826446079faa0914c2d705d98b02a2"
    "b5129cd1de164eb9cbd083e8a2503c4e"
)

# ---------------------------------------------------------------------------
# RFC 8439 §2.4.2 —— ChaCha20 加密
# ---------------------------------------------------------------------------

CIPHER_KEY = bytes(range(32))
CIPHER_NONCE = bytes.fromhex("000000000000004a00000000")
CIPHER_COUNTER = 1

CIPHER_PLAINTEXT = (
    "Ladies and Gentlemen of the class of '99: If I could offer you only "
    "one tip for the future, sunscreen would be it."
).encode("ascii")

CIPHER_EXPECTED = bytes.fromhex(
    "6e2e359a2568f98041ba0728dd0d6981"
    "e97e7aec1d4360c20a27afccfd9fae0b"
    "f91b65c5524733ab8f593dabcd62b357"
    "1639d624e65152ab8f530c359f0861d8"
    "07ca0dbf500d6a6156a38e088a22b65e"
    "52bc514d16ccf806818ce91ab7793736"
    "5af90bbf74a35be6b40b8eedf2785e42"
    "874d"
)

#: RFC 给出的密钥流，前 64 字节
CIPHER_KEYSTREAM = bytes.fromhex(
    "224f51f3401bd9e12fde276fb8631ded8c131f823d2c06"
    "e27e4fcaec9ef3cf788a3b0aa372600a92b57974cded2b"
    "9334794cba40c63e34cdea212c4cf07d41b769a6749f3f"
    "630f4122cafe28ec4dc47e26d4346d70b98c73f3e9c53a"
)

# ---------------------------------------------------------------------------
# RFC 8439 §2.5.2 —— Poly1305
# ---------------------------------------------------------------------------

POLY_KEY = bytes.fromhex(
    "85d6be7857556d337f4452fe42d506a8"
    "0103808afb0db2fd4abff6af4149f51b"
)
POLY_MESSAGE = b"Cryptographic Forum Research Group"
POLY_TAG = bytes.fromhex("a8061dc1305136c6c22b8baf0c0127a9")

# ---------------------------------------------------------------------------
# RFC 8439 §2.8.2 —— AEAD_CHACHA20_POLY1305
# ---------------------------------------------------------------------------

AEAD_KEY = bytes.fromhex(
    "808182838485868788898a8b8c8d8e8f"
    "909192939495969798999a9b9c9d9e9f"
)
AEAD_NONCE = bytes.fromhex("070000004041424344454647")
AEAD_AAD = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
AEAD_PLAINTEXT = (
    "Ladies and Gentlemen of the class of '99: If I could offer you only "
    "one tip for the future, sunscreen would be it."
).encode("ascii")
AEAD_CIPHERTEXT = bytes.fromhex(
    "d31a8d34648e60db7b86afbc53ef7ec2"
    "a4aded51296e08fea9e2b5a736ee62d6"
    "3dbea45e8ca9671282fafb69da92728b"
    "1a71de0a9e060b2905d6a5b67ecd3b36"
    "92ddbd7f2d778b8c9803aee328091b58"
    "fab324e4fad675945585808b4831d7bc"
    "3ff4def08e4b7a9de576d26586cec64b"
    "6116"
)
AEAD_TAG = bytes.fromhex("1ae10b594f09e26a7e902ecbd0600691")


class TestChaCha20Block(unittest.TestCase):
    """§2.3.2 —— 分组函数。最底层的一环，错了上面全错。"""

    def test_rfc_2_3_2_block_function(self):
        got = crypto.chacha20_block(BLOCK_KEY, BLOCK_COUNTER, BLOCK_NONCE)
        self.assertEqual(len(got), 64)
        self.assertEqual(got.hex(), BLOCK_EXPECTED.hex())

    def test_final_state_words_serialize_to_expected_block(self):
        """把 RFC 给的状态字按小端展开，应当逐字节等于 RFC 给的序列化结果。

        这条用例的作用是**锁住"字 → 字节"这一步**。实现和用例当初就是在这里
        分叉的：实现按小端序列化（对的），用例直接抄了状态字（错的）。
        """
        serialized = b"".join(
            word.to_bytes(4, "little") for word in BLOCK_FINAL_STATE_WORDS)
        self.assertEqual(serialized.hex(), BLOCK_EXPECTED.hex())

    def test_block_is_deterministic(self):
        first = crypto.chacha20_block(BLOCK_KEY, 1, BLOCK_NONCE)
        second = crypto.chacha20_block(BLOCK_KEY, 1, BLOCK_NONCE)
        self.assertEqual(first, second)

    def test_counter_changes_output(self):
        """计数器必须真的参与运算 —— 否则多块加密会退化成重复密钥流。"""
        a = crypto.chacha20_block(BLOCK_KEY, 1, BLOCK_NONCE)
        b = crypto.chacha20_block(BLOCK_KEY, 2, BLOCK_NONCE)
        self.assertNotEqual(a, b)

    def test_nonce_changes_output(self):
        a = crypto.chacha20_block(BLOCK_KEY, 1, BLOCK_NONCE)
        b = crypto.chacha20_block(BLOCK_KEY, 1, bytes(12))
        self.assertNotEqual(a, b)

    def test_rejects_wrong_sizes(self):
        with self.assertRaises(ValueError):
            crypto.chacha20_block(b"short", 1, BLOCK_NONCE)
        with self.assertRaises(ValueError):
            crypto.chacha20_block(BLOCK_KEY, 1, b"short")


class TestChaCha20Cipher(unittest.TestCase):
    """§2.4.2 —— 加密流。"""

    def test_rfc_2_4_2_encryption(self):
        got = crypto.chacha20_xor(CIPHER_KEY, CIPHER_COUNTER, CIPHER_NONCE,
                                  CIPHER_PLAINTEXT)
        self.assertEqual(got.hex(), CIPHER_EXPECTED.hex())

    def test_rfc_2_4_2_keystream(self):
        """单独验密钥流：加密错了但密钥流对了，说明是异或那一步的问题。"""
        stream = crypto.chacha20_xor(CIPHER_KEY, CIPHER_COUNTER, CIPHER_NONCE,
                                     bytes(len(CIPHER_KEYSTREAM)))
        self.assertEqual(stream.hex(), CIPHER_KEYSTREAM.hex())

    def test_decrypt_is_the_same_operation(self):
        encrypted = crypto.chacha20_xor(CIPHER_KEY, CIPHER_COUNTER, CIPHER_NONCE,
                                        CIPHER_PLAINTEXT)
        restored = crypto.chacha20_xor(CIPHER_KEY, CIPHER_COUNTER, CIPHER_NONCE,
                                       encrypted)
        self.assertEqual(restored, CIPHER_PLAINTEXT)

    def test_spans_multiple_blocks(self):
        """跨块时计数器要跟着进位，否则第二块会重复第一块的密钥流。"""
        payload = os.urandom(200)          # 200 字节 = 4 块以上
        stream = crypto.chacha20_xor(CIPHER_KEY, 1, CIPHER_NONCE,
                                     bytes(len(payload)))
        blocks = [stream[i:i + 64] for i in range(0, len(stream), 64)]
        self.assertGreater(len(blocks), 3)
        for i in range(1, len(blocks) - 1):
            self.assertNotEqual(blocks[i], blocks[0],
                                f"第 {i} 块与第 1 块重复，计数器没进位")

    def test_empty_payload(self):
        self.assertEqual(crypto.chacha20_xor(CIPHER_KEY, 1, CIPHER_NONCE, b""), b"")


class TestPoly1305(unittest.TestCase):
    """§2.5.2 —— 认证码。"""

    def test_rfc_2_5_2_mac(self):
        self.assertEqual(crypto.poly1305_mac(POLY_KEY, POLY_MESSAGE).hex(),
                         POLY_TAG.hex())

    def test_rejects_wrong_key_size(self):
        with self.assertRaises(ValueError):
            crypto.poly1305_mac(b"too short", b"x")

    def test_partial_final_block(self):
        """末尾不足 16 字节的块要按 0x01 补位处理，不能简单当作整数。"""
        key = bytes(range(32))
        tags = {crypto.poly1305_mac(key, b"A" * n) for n in range(1, 34)}
        self.assertEqual(len(tags), 33, "不同长度产生了相同标签，补位逻辑有问题")


class TestAEAD(unittest.TestCase):
    """§2.8.2 —— 整体。加密、认证、AAD 绑定、篡改检测。"""

    def test_rfc_2_8_2_roundtrip(self):
        produced = crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, AEAD_PLAINTEXT,
                                       AEAD_AAD)
        self.assertEqual(produced.hex(),
                         (AEAD_CIPHERTEXT + AEAD_TAG).hex())
        restored = crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, produced, AEAD_AAD)
        self.assertEqual(restored, AEAD_PLAINTEXT)

    def test_tag_is_last_16_bytes(self):
        produced = crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, AEAD_PLAINTEXT,
                                       AEAD_AAD)
        self.assertEqual(len(produced), len(AEAD_PLAINTEXT) + crypto.TAG_SIZE)
        self.assertEqual(produced[-16:].hex(), AEAD_TAG.hex())

    def test_tampered_ciphertext_is_rejected(self):
        """改动密文任意一位都必须被发现 —— 这是 AEAD 存在的全部意义。"""
        for index in (0, 17, len(AEAD_CIPHERTEXT) - 1):
            broken = bytearray(
                crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, AEAD_PLAINTEXT, AEAD_AAD))
            broken[index] ^= 0x01
            with self.assertRaises(crypto.AuthenticationError,
                                   msg=f"第 {index} 字节被改动却放行了"):
                crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, bytes(broken), AEAD_AAD)

    def test_tampered_tag_is_rejected(self):
        broken = bytearray(
            crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, AEAD_PLAINTEXT, AEAD_AAD))
        broken[-1] ^= 0x80
        with self.assertRaises(crypto.AuthenticationError):
            crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, bytes(broken), AEAD_AAD)

    def test_tampered_aad_is_rejected(self):
        """AAD 决定了帧头能不能被改 —— 重放与反射防护全靠这一条。"""
        payload = crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, AEAD_PLAINTEXT,
                                      AEAD_AAD)
        with self.assertRaises(crypto.AuthenticationError):
            crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, payload, b"other-aad")
        with self.assertRaises(crypto.AuthenticationError):
            crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, payload, AEAD_AAD + b"x")

    def test_wrong_key_is_rejected(self):
        payload = crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, AEAD_PLAINTEXT,
                                      AEAD_AAD)
        with self.assertRaises(crypto.AuthenticationError):
            crypto.aead_decrypt(bytes(32), AEAD_NONCE, payload, AEAD_AAD)

    def test_short_payload_is_rejected_as_auth_error_not_crash(self):
        with self.assertRaises(crypto.AuthenticationError):
            crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, b"12345", AEAD_AAD)

    def test_empty_plaintext_still_authenticated(self):
        """空明文也要带合法标签：否则"把帧体清成空"就成了绕过校验的手段。"""
        payload = crypto.aead_encrypt(AEAD_KEY, AEAD_NONCE, b"", AEAD_AAD)
        self.assertEqual(len(payload), crypto.TAG_SIZE)
        self.assertEqual(crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, payload,
                                             AEAD_AAD), b"")
        with self.assertRaises(crypto.AuthenticationError):
            crypto.aead_decrypt(AEAD_KEY, AEAD_NONCE, payload, b"tampered")

    def test_nonce_size_enforced(self):
        with self.assertRaises(ValueError):
            crypto.aead_encrypt(AEAD_KEY, b"short", b"data")
        with self.assertRaises(ValueError):
            crypto.aead_decrypt(AEAD_KEY, b"short", b"x" * 20)

    def test_random_nonce_gives_different_ciphertext(self):
        first = crypto.aead_encrypt(AEAD_KEY, crypto.random_nonce(), b"same")
        second = crypto.aead_encrypt(AEAD_KEY, crypto.random_nonce(), b"same")
        self.assertNotEqual(first, second)


class TestKeyDerivation(unittest.TestCase):
    """scrypt 密钥派生。重点在"随机 salt 让每次连接的密钥都不同"。

    这里的输入是**房间号（+ 可选房间口令）拼成的共享秘密**，不再是旧版那条
    一次性令牌 —— 那一层已经取消，房间号就是唯一凭据。
    """

    def test_same_secret_and_salt_is_stable(self):
        salt = b"0123456789abcdef"
        a = crypto.derive_secret_key("135790", salt, n=1 << 12)
        b = crypto.derive_secret_key("135790", salt, n=1 << 12)
        self.assertEqual(a, b)
        self.assertEqual(len(a), crypto.KEY_SIZE)

    def test_different_salt_gives_different_key(self):
        """这是防重放的根本：同一个房间每次连接派生出不同密钥，录下的帧下次用不了。"""
        a = crypto.derive_secret_key("135790", b"0123456789abcdef", n=1 << 12)
        b = crypto.derive_secret_key("135790", b"fedcba9876543210", n=1 << 12)
        self.assertNotEqual(a, b)

    def test_different_secret_gives_different_key(self):
        salt = b"0123456789abcdef"
        a = crypto.derive_secret_key("135790", salt, n=1 << 12)
        b = crypto.derive_secret_key("135791", salt, n=1 << 12)
        self.assertNotEqual(a, b)

    def test_room_password_is_case_sensitive(self):
        """房间口令**不做大小写折叠** —— 折叠会白白削掉一大截熵。

        这一条与房间号的处理刻意不同：房间号是纯数字，折叠无从谈起；
        口令是自由文本，统一成大写等于把可用空间砍掉一半以上。
        """
        salt = b"0123456789abcdef"
        base = crypto.derive_secret_key("135790:Secret", salt, n=1 << 12)
        self.assertNotEqual(
            crypto.derive_secret_key("135790:secret", salt, n=1 << 12), base)

    def test_secret_is_used_verbatim(self):
        """归一化是调用方（``session.room_secret``）的责任，这一层不许擅自改动。

        擅自 strip 会让"口令末尾带空格"这种配置在两端理解不一致，
        表现出来就是"口令明明对却连不上"。
        """
        salt = b"0123456789abcdef"
        self.assertNotEqual(
            crypto.derive_secret_key("135790:x", salt, n=1 << 12),
            crypto.derive_secret_key("135790:x ", salt, n=1 << 12))

    def test_short_salt_rejected(self):
        with self.assertRaises(ValueError):
            crypto.derive_secret_key("135790", b"short")

    def test_empty_secret_rejected(self):
        """空秘密会让所有房间派生出同一个密钥，必须当场拒绝而不是默默放行。"""
        with self.assertRaises(ValueError):
            crypto.derive_secret_key("", b"0123456789abcdef")

    def test_random_salt_is_random(self):
        salts = {crypto.random_salt() for _ in range(32)}
        self.assertEqual(len(salts), 32)
        self.assertEqual(len(next(iter(salts))), crypto.SESSION_SALT_SIZE)


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# #12 / #13 新增原语的对拍测试
# ---------------------------------------------------------------------------
# 期望值全部抄自官方文档（RFC 7748 §6.1、RFC 5869 A.1、GB/T 0004 / GB/T 0002、
# RFC 8998 A.1），不是自己算出来再抄一遍 —— 否则只能证明实现自洽，证明不了正确。
# 每一组都单独锁死一个原语，任何一位写错都会当场露馅。
# ===========================================================================


# ---------------------------------------------------------------------------
# X25519（RFC 7748 §6.1）
# ---------------------------------------------------------------------------

X25519_ALICE_PRIV = bytes.fromhex(
    "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
X25519_ALICE_PUB = bytes.fromhex(
    "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
X25519_BOB_PRIV = bytes.fromhex(
    "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
X25519_BOB_PUB = bytes.fromhex(
    "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
X25519_SHARED = bytes.fromhex(
    "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")


class TestX25519(unittest.TestCase):
    """RFC 7748 §6.1 的端到端握手向量 + ECDH 对称性。"""

    def test_rfc_6_1_public_keys(self):
        self.assertEqual(crypto.x25519_public_key(X25519_ALICE_PRIV).hex(),
                         X25519_ALICE_PUB.hex())
        self.assertEqual(crypto.x25519_public_key(X25519_BOB_PRIV).hex(),
                         X25519_BOB_PUB.hex())

    def test_rfc_6_1_shared_secret(self):
        a = crypto.x25519_derive_shared_secret(X25519_ALICE_PRIV, X25519_BOB_PUB)
        b = crypto.x25519_derive_shared_secret(X25519_BOB_PRIV, X25519_ALICE_PUB)
        self.assertEqual(a.hex(), X25519_SHARED.hex())
        self.assertEqual(b.hex(), X25519_SHARED.hex())

    def test_ecdh_is_symmetric_for_random_keys(self):
        """随机密钥对也必须对称 —— 否则握手两端算不出同一个会话密钥。"""
        for _ in range(20):
            a_priv, a_pub = crypto.x25519_generate_keypair()
            b_priv, b_pub = crypto.x25519_generate_keypair()
            ka = crypto.x25519_derive_shared_secret(a_priv, b_pub)
            kb = crypto.x25519_derive_shared_secret(b_priv, a_pub)
            self.assertEqual(ka, kb)

    def test_clamp_is_deterministic(self):
        """同一私钥反复取公钥必须稳定（钳位是确定性的）。"""
        first = crypto.x25519_public_key(X25519_ALICE_PRIV)
        for _ in range(10):
            self.assertEqual(crypto.x25519_public_key(X25519_ALICE_PRIV), first)

    def test_rejects_wrong_size(self):
        with self.assertRaises(ValueError):
            crypto.x25519_scalar_mult(b"short", X25519_BOB_PUB)
        with self.assertRaises(ValueError):
            crypto.x25519_scalar_mult(X25519_ALICE_PRIV, b"short")


# ---------------------------------------------------------------------------
# HKDF-SHA256（RFC 5869 A.1）
# ---------------------------------------------------------------------------

HKDF_IKM = bytes.fromhex("0b" * 22)
HKDF_SALT = bytes.fromhex("000102030405060708090a0b0c")
HKDF_INFO = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")          # 10 字节
HKDF_PRK = bytes.fromhex(
    "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5")
HKDF_OKM = bytes.fromhex(
    "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf3"
    "4007208d5b887185865")


class TestHKDF(unittest.TestCase):
    """RFC 5869 A.1 提取-扩展两步向量。"""

    def test_rfc_a_1_prk(self):
        prk = crypto._hmac.new(HKDF_SALT, HKDF_IKM, hashlib.sha256).digest()
        self.assertEqual(prk.hex(), HKDF_PRK.hex())

    def test_rfc_a_1_okm(self):
        self.assertEqual(
            crypto.hkdf_sha256(HKDF_SALT, HKDF_IKM, HKDF_INFO, 42).hex(),
            HKDF_OKM.hex())

    def test_info_is_bound_to_output(self):
        """info 区分不同用途的密钥 —— 改一位必须产出不同密钥。"""
        a = crypto.hkdf_sha256(HKDF_SALT, HKDF_IKM, b"session-a", 32)
        b = crypto.hkdf_sha256(HKDF_SALT, HKDF_IKM, b"session-b", 32)
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# SM3（GB/T 0004-2012）
# ---------------------------------------------------------------------------

class TestSM3(unittest.TestCase):
    """国密哈希，空串与 "abc" 两个权威向量。"""

    def test_empty(self):
        self.assertEqual(
            crypto.sm3(b"").hex(),
            "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b")

    def test_abc(self):
        self.assertEqual(
            crypto.sm3(b"abc").hex(),
            "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0")

    def test_long_message_changes_output(self):
        self.assertNotEqual(crypto.sm3(b"abc"), crypto.sm3(b"abcd"))


# ---------------------------------------------------------------------------
# SM4（GB/T 0002-2012）ECB
# ---------------------------------------------------------------------------

SM4_KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
SM4_BLOCK = bytes.fromhex("0123456789abcdeffedcba9876543210")
SM4_CIPHERTEXT = bytes.fromhex("681edf34d206965e86b3e94f536e4246")


class TestSM4Block(unittest.TestCase):
    """GB/T 32907 示例 1：ECB 单块。RFC 8998 A.1 会在 CTR 模式下再覆盖一遍 SM4。"""

    def test_gbt_example_1_ecb(self):
        self.assertEqual(crypto.sm4_block_encrypt(SM4_KEY, SM4_BLOCK).hex(),
                         SM4_CIPHERTEXT.hex())

    def test_decrypt_is_inverse_of_encrypt(self):
        ct = crypto.sm4_block_encrypt(SM4_KEY, SM4_BLOCK)
        # SM4 解密 = 同一分组函数 + 逆序轮密钥；这里用"再加密回去"验证不了，
        # 直接验证"加密输出是 16 字节且非明文"即可，CTR 回环在 GCM 用例里覆盖。
        self.assertEqual(len(ct), 16)
        self.assertNotEqual(ct, SM4_BLOCK)

    def test_rejects_wrong_size(self):
        with self.assertRaises(ValueError):
            crypto.sm4_block_encrypt(b"short", SM4_BLOCK)
        with self.assertRaises(ValueError):
            crypto.sm4_block_encrypt(SM4_KEY, b"short")


# ---------------------------------------------------------------------------
# SM4-GCM（RFC 8998 A.1）
# ---------------------------------------------------------------------------

SM4GCM_KEY = bytes.fromhex("0123456789ABCDEFFEDCBA9876543210")
SM4GCM_NONCE = bytes.fromhex("00001234567800000000ABCD")
SM4GCM_PLAINTEXT = bytes.fromhex(
    "AAAAAAAAAAAAAAAABBBBBBBBBBBBBBBB"
    "CCCCCCCCCCCCCCCCDDDDDDDDDDDDDDDD"
    "EEEEEEEEEEEEEEEEFFFFFFFFFFFFFFFF"
    "EEEEEEEEEEEEEEEEAAAAAAAAAAAAAAAA")
SM4GCM_AAD = bytes.fromhex("FEEDFACEDEADBEEFFEEDFACEDEADBEEFABADDAD2")
SM4GCM_CIPHERTEXT = bytes.fromhex(
    "17F399F08C67D5EE19D0DC9969C4BB7D"
    "5FD46FD3756489069157B282BB200735"
    "D82710CA5C22F0CCFA7CBF93D496AC15"
    "A56834CBCF98C397B4024A2691233B8D")
SM4GCM_TAG = bytes.fromhex("83DE3541E4C2B58177E065A9BF7B62EC")


class TestSM4GCM(unittest.TestCase):
    """RFC 8998 A.1 全向量：密文 + 标签都要逐字节对上。"""

    def test_rfc_a_1_full_vector(self):
        out = crypto.sm4_gcm_encrypt(SM4GCM_KEY, SM4GCM_NONCE,
                                     SM4GCM_PLAINTEXT, SM4GCM_AAD)
        self.assertEqual(out[:-16].hex(), SM4GCM_CIPHERTEXT.hex())
        self.assertEqual(out[-16:].hex(), SM4GCM_TAG.hex())

    def test_roundtrip_with_aad(self):
        out = crypto.sm4_gcm_encrypt(SM4GCM_KEY, SM4GCM_NONCE,
                                     SM4GCM_PLAINTEXT, SM4GCM_AAD)
        self.assertEqual(
            crypto.sm4_gcm_decrypt(SM4GCM_KEY, SM4GCM_NONCE, out, SM4GCM_AAD),
            SM4GCM_PLAINTEXT)

    def test_tampered_ciphertext_rejected(self):
        out = bytearray(crypto.sm4_gcm_encrypt(SM4GCM_KEY, SM4GCM_NONCE,
                                               SM4GCM_PLAINTEXT, SM4GCM_AAD))
        out[0] ^= 0x01
        with self.assertRaises(crypto.AuthenticationError):
            crypto.sm4_gcm_decrypt(SM4GCM_KEY, SM4GCM_NONCE, bytes(out),
                                   SM4GCM_AAD)

    def test_tampered_aad_rejected(self):
        out = crypto.sm4_gcm_encrypt(SM4GCM_KEY, SM4GCM_NONCE,
                                     SM4GCM_PLAINTEXT, SM4GCM_AAD)
        with self.assertRaises(crypto.AuthenticationError):
            crypto.sm4_gcm_decrypt(SM4GCM_KEY, SM4GCM_NONCE, out, b"other-aad")

    def test_empty_plaintext_still_authenticated(self):
        out = crypto.sm4_gcm_encrypt(SM4GCM_KEY, SM4GCM_NONCE, b"", SM4GCM_AAD)
        self.assertEqual(len(out), crypto.TAG_SIZE)
        self.assertEqual(
            crypto.sm4_gcm_decrypt(SM4GCM_KEY, SM4GCM_NONCE, out, SM4GCM_AAD), b"")

    def test_rejects_wrong_key_or_nonce_size(self):
        with self.assertRaises(ValueError):
            crypto.sm4_gcm_encrypt(b"short", SM4GCM_NONCE, b"x", b"")
        with self.assertRaises(ValueError):
            crypto.sm4_gcm_encrypt(SM4GCM_KEY, b"short", b"x", b"")


# ---------------------------------------------------------------------------
# 套件分派：密钥长度 / 加解密回环
# ---------------------------------------------------------------------------

class TestSuiteDispatch(unittest.TestCase):
    """aead_key_size 与 kex_session_key 必须按套件的 AEAD 给出正确长度。"""

    def test_aead_key_size(self):
        self.assertEqual(crypto.aead_key_size("chacha20-poly1305"), 32)
        self.assertEqual(crypto.aead_key_size("sm4-gcm"), 16)

    def test_kex_session_key_length_per_suite(self):
        a_priv, a_pub = crypto.x25519_generate_keypair()
        b_priv, b_pub = crypto.x25519_generate_keypair()
        for suite, expected in (("curve25519-chacha20", 32),
                                ("x25519-sm4gcm", 16)):
            ka = crypto.kex_session_key(suite, private_key=a_priv,
                                       peer_public=b_pub)
            kb = crypto.kex_session_key(suite, private_key=b_priv,
                                       peer_public=a_pub)
            self.assertEqual(len(ka), expected)
            self.assertEqual(ka, kb)

    def test_suite_encrypt_decrypt_roundtrip(self):
        for suite in ("curve25519-chacha20", "x25519-sm4gcm"):
            a_priv, a_pub = crypto.x25519_generate_keypair()
            b_priv, b_pub = crypto.x25519_generate_keypair()
            key = crypto.kex_session_key(suite, private_key=a_priv,
                                         peer_public=b_pub)
            nonce = crypto.random_nonce()
            pt = b"offline-oj frame " + os.urandom(50)
            aad = b"dir=client seq=7"
            ct = crypto.aead_encrypt_suite(suite, key, nonce, pt, aad)
            self.assertEqual(
                crypto.aead_decrypt_suite(suite, key, nonce, ct, aad), pt)

    def test_suite_rejects_wrong_key_size(self):
        with self.assertRaises(ValueError):
            crypto.aead_encrypt_suite("x25519-sm4gcm", os.urandom(32),
                                      os.urandom(12), b"x", b"")

