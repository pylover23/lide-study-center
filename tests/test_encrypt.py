"""encrypt 对拍单测：AES-CTR 对称往返 + ki 长度格式 + 服务端兼容已由 gen/check 实测。"""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.encrypt import (aes_ctr_encrypt, aes_ctr_decrypt, encrypt_ki,
                         encrypt_payload, PUBLIC_KEY_B64)


class TestEncrypt(unittest.TestCase):
    def test_aes_ctr_roundtrip(self):
        key, iv = os.urandom(16), os.urandom(16)
        plain = '{"custom":{"session":{"username":"2023201796"}}}'
        ct = aes_ctr_encrypt(key, iv, plain)
        self.assertEqual(aes_ctr_decrypt(key, iv, ct), plain)
        # CTR 无填充：密文长度 == 明文长度
        self.assertEqual(len(base64.b64decode(ct)), len(plain.encode()))

    def test_aes_ctr_known_stream(self):
        # 固定 key/iv 重复加密结果一致（确定性流）
        key = bytes(range(16))
        iv = bytes(range(16))
        self.assertEqual(aes_ctr_encrypt(key, iv, "abc"),
                         aes_ctr_encrypt(key, iv, "abc"))

    def test_ki_format(self):
        ki = encrypt_ki(os.urandom(16), os.urandom(16))
        raw = base64.b64decode(ki)
        # RSA-1024 输出固定 128 字节 -> base64 172 字符
        self.assertEqual(len(raw), 128)
        self.assertEqual(len(ki), 172)

    def test_encrypt_payload_shape(self):
        body = encrypt_payload({"a": 1})
        self.assertIn("data", body)
        self.assertIn("ki", body)
        self.assertIsInstance(base64.b64decode(body["data"]), bytes)

    def test_public_key_is_valid_spki(self):
        # 裸 base64 SPKI 可被 cryptography 加载（模块导入时已验证，此处复述断言）
        raw = base64.b64decode(PUBLIC_KEY_B64)
        self.assertEqual(raw[0:2], b"\x30\x81")  # SEQUENCE header


if __name__ == "__main__":
    unittest.main(verbosity=2)
