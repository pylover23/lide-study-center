"""tianai-captcha 加密封装：AES-128-CTR + RSA-1024 PKCS1v1.5 信封。

协议（逆向自 tac.min.js 的 rsaaes 处理器，站点公钥为 chunk-detail.js 覆盖版）：
- key/iv 各 16 随机字节
- 明文 JSON 字符串 -> AES-CTR -> base64，填充 data / custom 字段
- ki = RSA(pubKey, keyHex + "|" + ivHex) -> base64
"""
import base64
import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 站点覆盖版公钥（chunk-detail.js 中 TAC.enc.rsaPublicKey 的值）
PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDXRMEk7baUetStHq6IPIxwKB9ga9"
    "UyCepDEEFIZUS5cc1/FyS90Tbd1VA4j+AfqurclfBHUWgvuzAj4oW5b/sdS1SC14259"
    "tLexFbT5EfPsyY0BPfMXkzUerSbzgL8ZIUtHfHV1z6/WA6iHVmB1SpWT2BwaE9Aledn"
    "p9EO8dLQvQIDAQAB"
)

_PEM = "-----BEGIN PUBLIC KEY-----\n{}\n-----END PUBLIC KEY-----".format(PUBLIC_KEY_B64)
_PUBLIC_KEY = serialization.load_pem_public_key(_PEM.encode())


def _dumps(obj) -> str:
    # 与 JSON.stringify 一致的紧凑格式，键序保持插入序（勿 sort）
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def aes_ctr_encrypt(key: bytes, iv: bytes, plaintext: str) -> str:
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ct = enc.update(plaintext.encode("utf-8")) + enc.finalize()
    return base64.b64encode(ct).decode()


def aes_ctr_decrypt(key: bytes, iv: bytes, b64_ciphertext: str) -> str:
    dec = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    pt = dec.update(base64.b64decode(b64_ciphertext)) + dec.finalize()
    return pt.decode("utf-8")


def encrypt_ki(key: bytes, iv: bytes) -> str:
    msg = f"{key.hex()}|{iv.hex()}".encode()
    ct = _PUBLIC_KEY.encrypt(msg, padding.PKCS1v15())
    return base64.b64encode(ct).decode()


def encrypt_payload(obj: dict) -> dict:
    """加密一个明文对象，返回 {ki, data} 形式的加密体。"""
    key, iv = os.urandom(16), os.urandom(16)
    return {"data": aes_ctr_encrypt(key, iv, _dumps(obj)), "ki": encrypt_ki(key, iv)}


def decrypt_with_ki(body: dict) -> dict:
    """用 ki 反解 key/iv 需要 RSA 私钥，此函数仅用于自测（不适用生产）。
    生产中 gen 响应的 captcha.data 用请求时的 key/iv 解密。"""
    raise NotImplementedError
