from __future__ import annotations

import base64
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .models import DeviceProfile


def build_device_info(device: DeviceProfile) -> dict[str, Any]:
    return {
        "DeviceID": device.device_id,
        "MachineID": device.machine_id,
        "PlatformCode": device.platform_code or "IDE_PC",
        "DeviceType": device.device_class or "PC",
        "DeviceName": device.device_name,
        "DeviceModel": device.device_model or device.brand,
        "ClientVersion": device.app_version,
        "DevicePublicKey": device.public_key,
        "DeviceBrand": device.device_manufacturer,
        "DeviceCPU": device.cpu_brand,
        "OSInfo": device.os_name or "windows",
        "OSVersion": device.os_version,
    }


def build_device_proof(
    method: str,
    path: str,
    client_id: str,
    refresh_token: str,
    private_key_pem: str,
) -> dict[str, Any]:
    if not private_key_pem:
        raise ValueError("device private key is empty")
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    message = "\n".join(
        [method, path, client_id, refresh_token, str(timestamp), nonce]
    )
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("ascii"), password=None
    )
    signature = private_key.sign(message.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return {
        "Signature": base64.b64encode(signature).decode("ascii"),
        "Timestamp": timestamp,
        "Nonce": nonce,
    }
