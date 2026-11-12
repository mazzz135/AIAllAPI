from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_pkce() -> tuple[str, str]:
    verifier = b64url(os.urandom(48))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_device(ide_version: str) -> tuple[dict[str, Any], str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    device = {
        # 官方客户端使用 15 位纯数字设备 ID，UUID 会被判为无效设备
        "DeviceID": str(int.from_bytes(os.urandom(8), "big") % 10**15).zfill(15),
        "MachineID": hashlib.sha512(os.urandom(32)).hexdigest(),
        "PlatformCode": "IDE_PC",
        "DeviceType": "PC",
        "DeviceName": "TraeRelay",
        "DeviceModel": "",
        "DeviceBrand": "",
        "DeviceCPU": "",
        "OSInfo": "Windows",
        "OSVersion": "",
        "ClientVersion": ide_version,
        "DevicePublicKey": public_pem,
    }
    return device, private_pem


def device_proof(private_pem: str, client_id: str, refresh_token: str) -> dict[str, Any]:
    path = "/trae/api/v3/oauth/ExchangeToken"
    stamp = int(time.time())
    nonce = uuid.uuid4().hex
    content = f"POST\n{path}\n{client_id}\n{refresh_token}\n{stamp}\n{nonce}"
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    signature = key.sign(content.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return {
        "Signature": base64.b64encode(signature).decode("ascii"),
        "Timestamp": stamp,
        "Nonce": nonce,
    }
