from __future__ import annotations

import random
import secrets
import string
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except Exception:  # pragma: no cover - cryptography is a runtime dependency
    serialization = None
    ec = None


@dataclass
class TokenBundle:
    token: str = ""
    refresh_token: str = ""
    token_expires_at: Optional[int] = None
    refresh_expires_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeviceProfile:
    # Legacy fields kept first so existing keyword constructors keep working.
    machine_id: str
    device_id: str
    brand: str
    cpu: str
    device_type: str
    os_version: str
    app_version: str
    ide_version: str
    ide_version_code: str
    web_id: str = ""

    device_name: str = ""
    device_model: str = ""
    device_manufacturer: str = ""
    cpu_manufacturer: str = ""
    cpu_brand: str = ""
    cpu_model: str = ""
    cpu_family: str = ""
    cpu_stepping: str = ""
    cpu_speed: float = 0.0
    cpu_count: int = 0
    memory: int = 0
    os_name: str = ""
    os_release: str = ""
    os_kernel: str = ""
    platform: str = ""
    platform_code: str = ""
    device_class: str = ""
    screen_width: int = 0
    screen_height: int = 0
    language: str = ""
    public_key: str = ""
    private_key_pem: str = field(default="", repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("private_key_pem", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeviceProfile":
        data = data or {}
        return cls(
            machine_id=str(data.get("machine_id") or ""),
            device_id=str(data.get("device_id") or ""),
            brand=str(data.get("brand") or ""),
            cpu=str(data.get("cpu") or ""),
            device_type=str(data.get("device_type") or "windows"),
            os_version=str(data.get("os_version") or ""),
            app_version=str(data.get("app_version") or ""),
            ide_version=str(data.get("ide_version") or ""),
            ide_version_code=str(data.get("ide_version_code") or ""),
            web_id=str(data.get("web_id") or ""),
            device_name=str(data.get("device_name") or ""),
            device_model=str(data.get("device_model") or ""),
            device_manufacturer=str(data.get("device_manufacturer") or ""),
            cpu_manufacturer=str(data.get("cpu_manufacturer") or ""),
            cpu_brand=str(data.get("cpu_brand") or ""),
            cpu_model=str(data.get("cpu_model") or ""),
            cpu_family=str(data.get("cpu_family") or ""),
            cpu_stepping=str(data.get("cpu_stepping") or ""),
            cpu_speed=float(data.get("cpu_speed") or 0),
            cpu_count=int(data.get("cpu_count") or 0),
            memory=int(data.get("memory") or 0),
            os_name=str(data.get("os_name") or ""),
            os_release=str(data.get("os_release") or ""),
            os_kernel=str(data.get("os_kernel") or ""),
            platform=str(data.get("platform") or ""),
            platform_code=str(data.get("platform_code") or ""),
            device_class=str(data.get("device_class") or ""),
            screen_width=int(data.get("screen_width") or 0),
            screen_height=int(data.get("screen_height") or 0),
            language=str(data.get("language") or ""),
            public_key=str(data.get("public_key") or ""),
            private_key_pem=str(data.get("private_key_pem") or ""),
        )

    def common_params(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "machine_id": self.machine_id,
            "device_name": self.device_name,
            "device_model": self.device_model or self.brand,
            "device_manufacturer": self.device_manufacturer,
            "cpu": self.cpu_manufacturer or self.cpu,
            "cpu_brand": self.cpu_brand,
            "cpu_family": self.cpu_family,
            "cpu_model": self.cpu_model or self.cpu_brand,
            "cpu_speed": self.cpu_speed,
            "cpu_stepping": self.cpu_stepping,
            "cpu_vendor": self.cpu_manufacturer or self.cpu,
            "memory": self.memory,
            "os_name": self.os_name or self.device_type,
            "os_version": self.os_version,
            "os_release": self.os_release,
            "os_build": self.os_release,
            "platform": self.platform or "electron",
            "language": self.language or "zh-CN",
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
        }


@dataclass(frozen=True)
class DeviceSpec:
    model: str
    manufacturer: str
    cpu_manufacturer: str
    cpu_brand: str
    cpu_family: str
    cpu_speed: float
    cpu_count: int
    memory_gb: int
    os_version: str
    os_release: str
    screen_width: int
    screen_height: int


DEVICE_SPECS = [
    DeviceSpec(
        model="83DG",
        manufacturer="LENOVO",
        cpu_manufacturer="AMD",
        cpu_brand="AMD Ryzen 7 5800H with Radeon Graphics",
        cpu_family="Ryzen 7",
        cpu_speed=3.2,
        cpu_count=16,
        memory_gb=16,
        os_version="Windows 11 Pro",
        os_release="10.0.22631",
        screen_width=1920,
        screen_height=1080,
    ),
    DeviceSpec(
        model="82WM",
        manufacturer="LENOVO",
        cpu_manufacturer="AMD",
        cpu_brand="AMD Ryzen 7 6800H with Radeon Graphics",
        cpu_family="Ryzen 7",
        cpu_speed=3.2,
        cpu_count=16,
        memory_gb=16,
        os_version="Windows 11 Home",
        os_release="10.0.22631",
        screen_width=2560,
        screen_height=1440,
    ),
    DeviceSpec(
        model="20XW",
        manufacturer="LENOVO",
        cpu_manufacturer="Intel",
        cpu_brand="12th Gen Intel(R) Core(TM) i7-1260P",
        cpu_family="Core i7",
        cpu_speed=2.1,
        cpu_count=16,
        memory_gb=16,
        os_version="Windows 11 Pro",
        os_release="10.0.22631",
        screen_width=1920,
        screen_height=1200,
    ),
    DeviceSpec(
        model="FA507RM",
        manufacturer="ASUSTeK COMPUTER INC.",
        cpu_manufacturer="AMD",
        cpu_brand="AMD Ryzen 7 6800H with Radeon Graphics",
        cpu_family="Ryzen 7",
        cpu_speed=3.2,
        cpu_count=16,
        memory_gb=16,
        os_version="Windows 11 Home",
        os_release="10.0.22631",
        screen_width=2560,
        screen_height=1440,
    ),
    DeviceSpec(
        model="15-dk2xxx",
        manufacturer="HP",
        cpu_manufacturer="Intel",
        cpu_brand="11th Gen Intel(R) Core(TM) i7-11370H",
        cpu_family="Core i7",
        cpu_speed=3.3,
        cpu_count=8,
        memory_gb=16,
        os_version="Windows 11 Home",
        os_release="10.0.22000",
        screen_width=1920,
        screen_height=1080,
    ),
    DeviceSpec(
        model="XPS 15 9520",
        manufacturer="Dell Inc.",
        cpu_manufacturer="Intel",
        cpu_brand="12th Gen Intel(R) Core(TM) i7-12700H",
        cpu_family="Core i7",
        cpu_speed=2.3,
        cpu_count=20,
        memory_gb=32,
        os_version="Windows 11 Pro",
        os_release="10.0.22631",
        screen_width=3456,
        screen_height=2160,
    ),
    DeviceSpec(
        model="MateBook 14",
        manufacturer="HUAWEI",
        cpu_manufacturer="Intel",
        cpu_brand="13th Gen Intel(R) Core(TM) i7-1360P",
        cpu_family="Core i7",
        cpu_speed=2.2,
        cpu_count=16,
        memory_gb=16,
        os_version="Windows 11 Home",
        os_release="10.0.22631",
        screen_width=2160,
        screen_height=1440,
    ),
]

DEVICE_BRANDS = [spec.model for spec in DEVICE_SPECS]


@dataclass
class AccountProfile:
    user_id: str
    username: str = ""
    avatar_url: str = ""
    edition: str = "cn"
    host: str = "https://api.trae.cn"
    client_id: str = ""
    plan: str = ""
    label: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImportedAccount:
    profile: AccountProfile
    tokens: TokenBundle
    device: DeviceProfile

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "tokens": self.tokens.to_dict(),
            "device": self.device.to_dict(),
        }


def new_machine_id() -> str:
    return secrets.token_hex(16)


def new_device_id() -> str:
    return str(random.randint(10**18, 10**19 - 1))


def new_web_id() -> str:
    return str(random.randint(10**17, 10**18 - 1))


def new_device_name() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "DESKTOP-" + "".join(secrets.choice(alphabet) for _ in range(7))


def generate_keypair() -> tuple[str, str]:
    """Return (public PEM, private PEM) or ("", "") if unavailable."""
    if ec is None or serialization is None:
        return "", ""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return public_pem, private_pem


def _spec_for(model: str) -> DeviceSpec:
    for spec in DEVICE_SPECS:
        if spec.model.lower() == (model or "").lower():
            return spec
    return random.choice(DEVICE_SPECS)


def build_device(
    *,
    app_version: str = "0.1.7",
    ide_version: str = "3.4.22",
    ide_version_code: str = "20261118",
    web_id: str = "",
    brand: str = "",
) -> DeviceProfile:
    spec = _spec_for(brand) if brand else random.choice(DEVICE_SPECS)
    public_key, private_key = generate_keypair()
    return DeviceProfile(
        machine_id=new_machine_id(),
        device_id=new_device_id(),
        brand=spec.model,
        cpu=spec.cpu_manufacturer,
        device_type="windows",
        os_version=spec.os_version,
        app_version=app_version,
        ide_version=ide_version,
        ide_version_code=ide_version_code,
        web_id=web_id,
        device_name=new_device_name(),
        device_model=spec.model,
        device_manufacturer=spec.manufacturer,
        cpu_manufacturer=spec.cpu_manufacturer,
        cpu_brand=spec.cpu_brand,
        cpu_model=spec.cpu_brand,
        cpu_family=spec.cpu_family,
        cpu_stepping="0",
        cpu_speed=spec.cpu_speed,
        cpu_count=spec.cpu_count,
        memory=spec.memory_gb * 1024**3,
        os_name="windows",
        os_release=spec.os_release,
        os_kernel=spec.os_release,
        platform="electron",
        platform_code="IDE_PC",
        device_class="PC",
        screen_width=spec.screen_width,
        screen_height=spec.screen_height,
        language="zh-CN",
        public_key=public_key,
        private_key_pem=private_key,
    )


def normalize_device(
    data: dict[str, Any] | None,
    *,
    app_version: str = "0.1.7",
    ide_version: str = "3.4.22",
    ide_version_code: str = "20261118",
) -> DeviceProfile:
    device = DeviceProfile.from_dict(data)
    if not device.machine_id:
        device.machine_id = new_machine_id()
    if not device.device_id:
        device.device_id = new_device_id()

    model = device.device_model or device.brand
    spec = _spec_for(model) if model else random.choice(DEVICE_SPECS)
    if not device.brand:
        device.brand = spec.model
    if not device.device_model:
        device.device_model = device.brand

    if not device.device_name:
        device.device_name = new_device_name()
    if not device.device_manufacturer:
        device.device_manufacturer = spec.manufacturer
    if not device.cpu:
        device.cpu = spec.cpu_manufacturer
    if not device.cpu_manufacturer:
        device.cpu_manufacturer = spec.cpu_manufacturer
    if not device.cpu_brand:
        device.cpu_brand = spec.cpu_brand
    if not device.cpu_model:
        device.cpu_model = device.cpu_brand
    if not device.cpu_family:
        device.cpu_family = spec.cpu_family
    if not device.cpu_stepping:
        device.cpu_stepping = "0"
    if not device.cpu_speed:
        device.cpu_speed = spec.cpu_speed
    if not device.cpu_count:
        device.cpu_count = spec.cpu_count
    if not device.memory:
        device.memory = spec.memory_gb * 1024**3
    if not device.os_name:
        device.os_name = "windows"
    if not device.device_type:
        device.device_type = device.os_name
    if not device.os_version:
        device.os_version = spec.os_version
    if not device.os_release:
        device.os_release = spec.os_release
    if not device.os_kernel:
        device.os_kernel = device.os_release
    if not device.platform:
        device.platform = "electron"
    if not device.platform_code:
        device.platform_code = "IDE_PC"
    if not device.device_class:
        device.device_class = "PC"
    if not device.screen_width:
        device.screen_width = spec.screen_width
    if not device.screen_height:
        device.screen_height = spec.screen_height
    if not device.language:
        device.language = "zh-CN"
    if not device.app_version:
        device.app_version = app_version
    if not device.ide_version:
        device.ide_version = ide_version
    if not device.ide_version_code:
        device.ide_version_code = ide_version_code
    if not device.public_key:
        device.public_key, device.private_key_pem = generate_keypair()
    return device


def random_trace_id() -> str:
    alphabet = string.hexdigits.lower()[:16]
    return "".join(secrets.choice(alphabet) for _ in range(32))
