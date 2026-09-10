# -*- coding: utf-8 -*-
"""
CloudAutoSearch（115 RSS 离线下载）MoviePilot 插件

功能：
- 定时轮询多条 RSS（每行一个 URL）
- 按 包含关键词 / 排除关键词 / 最小-最大体积(GB) 过滤
- 将 torrent 链接转为 magnet（bencode 解析 + sha1(info)）
- 通过 m115 加密提交到 115 离线下载指定文件夹
- 仅提交成功后持久化去重（info_hash 小写为 key），失败下轮自动重试
- threading.Lock 非阻塞并发锁，防止 cron 与手动运行重叠
- 115 传统扫码 Cookie 登录（非开放平台 Token）

设计说明：
- 所有与 MoviePilot 运行环境无关的核心逻辑（bencode / torrent->magnet /
  passes_filter / RSS 解析 / m115 加解密 / 去重判定）均定义为模块级纯函数或
  可独立调用的函数，便于在没有 MoviePilot 的环境下用 pytest 单独测试。
- RSA 层（rsa_encrypt / rsa_decrypt）按规范原样实现，生产环境用于与 115 服务端
  通信；m115_pack / m115_unpack 为真正互逆的 XOR 流水线，测试时可将 RSA 替换为
  恒等桩验证加解密 round-trip。
"""

import base64
import datetime
import hashlib
import os
import re
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

# ----------------------------------------------------------------------------
# MoviePilot / 第三方依赖。
# 在测试环境（无 MoviePilot）下提供最小桩，保证本模块可被独立 import。
# ----------------------------------------------------------------------------
_MP_AVAILABLE = True
try:
    from app import schemas
    from app.core.config import settings
    from app.log import logger
    from app.plugins import _PluginBase
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except Exception:  # pragma: no cover - 仅在无 MoviePilot 的测试环境触发
    _MP_AVAILABLE = False

    class _PluginBase:  # type: ignore
        """测试环境桩基类"""

        def get_data(self, key: str, default: Any = None) -> Any:
            return default

        def save_data(self, key: str, value: Any) -> None:
            pass

        def del_data(self, key: str) -> None:
            pass

        def update_config(self, config: dict) -> None:
            pass

        def get_config(self) -> dict:
            return {}

        def post_message(self, **kwargs) -> None:
            pass

    class _StubSettings:
        TZ = "Asia/Shanghai"
        PROXY_HOST = ""

    settings = _StubSettings()

    class _StubLogger:
        @staticmethod
        def _emit(level: str, *args, **kwargs):
            msg = " ".join(str(a) for a in args)
            print(f"[{level}] {msg}")

        def info(self, *a, **k):
            self._emit("INFO", *a)

        def warning(self, *a, **k):
            self._emit("WARNING", *a)

        def error(self, *a, **k):
            self._emit("ERROR", *a)

        def debug(self, *a, **k):
            self._emit("DEBUG", *a)

    logger = _StubLogger()  # type: ignore

    class _StubScheduler:
        def __init__(self, *a, **k):
            self._jobs = []

        def add_job(self, **kwargs):
            self._jobs.append(kwargs)

        def get_jobs(self):
            return self._jobs

        def start(self):
            pass

        def shutdown(self, *a, **k):
            pass

        def remove_all_jobs(self):
            self._jobs = []

        def print_jobs(self):
            pass

    BackgroundScheduler = _StubScheduler  # type: ignore

    class _StubCronTrigger:
        @staticmethod
        def from_crontab(expr):
            return expr

    CronTrigger = _StubCronTrigger  # type: ignore

    class _StubSchemas:
        class Response:
            def __init__(self, success=True, data=None, message=""):
                self.success = success
                self.data = data or {}
                self.message = message

    schemas = _StubSchemas()  # type: ignore

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


# ============================================================================
# bencode 编解码（仅依赖标准库；返回 bytes 作为字符串值）
# ============================================================================
def bencode_decode(data: bytes):
    """解码 bencode 字节串为 Python 对象。

    字符串值返回 bytes，整数返回 int，列表返回 list，字典返回 dict（key 为 bytes）。
    """
    pos = 0

    def decode():
        nonlocal pos
        c = data[pos:pos + 1]
        if c == b"i":
            pos += 1
            end = data.index(b"e", pos)
            val = int(data[pos:end])
            pos = end + 1
            return val
        if c == b"l":
            pos += 1
            lst = []
            while data[pos:pos + 1] != b"e":
                lst.append(decode())
            pos += 1
            return lst
        if c == b"d":
            pos += 1
            dct = {}
            while data[pos:pos + 1] != b"e":
                k = decode()
                v = decode()
                dct[k] = v
            pos += 1
            return dct
        # 字符串：<长度>:<内容>
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        pos = colon + 1
        s = data[pos:pos + length]
        pos += length
        return s

    return decode()


def bencode_encode(obj) -> bytes:
    """将 Python 对象编码为 bencode 字节串。"""
    if isinstance(obj, bool):
        raise TypeError("bool is not a valid bencode type")
    if isinstance(obj, int):
        return b"i" + str(obj).encode("ascii") + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode("ascii") + b":" + obj
    if isinstance(obj, str):
        s = obj.encode("utf-8")
        return str(len(s)).encode("ascii") + b":" + s
    if isinstance(obj, (list, tuple)):
        return b"l" + b"".join(bencode_encode(i) for i in obj) + b"e"
    if isinstance(obj, dict):
        parts = []
        for k in sorted(obj.keys()):
            parts.append(bencode_encode(k))
            parts.append(bencode_encode(obj[k]))
        return b"d" + b"".join(parts) + b"e"
    raise TypeError(f"cannot bencode object of type {type(obj)!r}")


# ============================================================================
# Torrent -> Magnet
# ============================================================================
def torrent_to_magnet(torrent_bytes: bytes) -> Tuple[str, str]:
    """解析 torrent 文件字节串，返回 (magnet_link, torrent_name)。"""
    meta = bencode_decode(torrent_bytes)
    info = meta[b"info"]
    info_bytes = bencode_encode(info)
    info_hash = hashlib.sha1(info_bytes).hexdigest().upper()

    name = info.get(b"name", b"").decode("utf-8", errors="replace") if isinstance(info, dict) else ""

    trackers: List[str] = []
    if isinstance(meta, dict):
        if b"announce" in meta:
            trackers.append(meta[b"announce"].decode("utf-8", errors="replace"))
        if b"announce-list" in meta:
            for tier in meta[b"announce-list"]:
                for tracker in tier:
                    t = tracker.decode("utf-8", errors="replace")
                    if t not in trackers:
                        trackers.append(t)

    parts = [f"xt=urn:btih:{info_hash}"]
    if name:
        parts.append(f"dn={quote(name)}")
    for t in trackers:
        parts.append(f"tr={quote(t)}")

    return "magnet:?" + "&".join(parts), name


def extract_info_hash_from_magnet(magnet: str) -> Optional[str]:
    """从 magnet 链接中提取 info_hash（小写 40 位十六进制），失败返回 None。"""
    if not magnet:
        return None
    m = re.search(r"urn:btih:([0-9a-fA-F]{40})", magnet)
    if m:
        return m.group(1).lower()
    return None


# ============================================================================
# 过滤逻辑（纯函数）
# ============================================================================
def passes_filter(
    title: str,
    size_bytes: int,
    include_keywords: List[str],
    exclude_keywords: List[str],
    min_size_gb: Optional[float],
    max_size_gb: Optional[float],
) -> bool:
    """判断一个 RSS 条目是否通过过滤。

    :param title: 条目标题
    :param size_bytes: 条目大小（字节），未知传 0
    :param include_keywords: 包含关键词列表，空列表/None 表示不过滤
    :param exclude_keywords: 排除关键词列表
    :param min_size_gb: 最小体积 GB，None/0 表示不限制
    :param max_size_gb: 最大体积 GB，None/0 表示不限制
    """
    title_l = (title or "").lower()
    # 包含过滤
    if include_keywords:
        if not any(kw and kw.lower() in title_l for kw in include_keywords):
            return False
    # 排除过滤
    if exclude_keywords:
        if any(kw and kw.lower() in title_l for kw in exclude_keywords):
            return False
    # 大小过滤
    try:
        size_bytes = int(size_bytes or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if min_size_gb:
        if size_bytes and size_bytes < float(min_size_gb) * (1024 ** 3):
            return False
    if max_size_gb:
        if size_bytes and size_bytes > float(max_size_gb) * (1024 ** 3):
            return False
    return True


def parse_keywords(raw: str) -> List[str]:
    """将逗号分隔的关键词字符串解析为列表（去空白、去空项）。"""
    if not raw:
        return []
    return [k.strip() for k in re.split(r"[,，\n]", raw) if k and k.strip()]


# ============================================================================
# RSS / Torznab 解析（纯函数）
# ============================================================================
def parse_rss_items(xml_bytes: bytes) -> List[Dict[str, Any]]:
    """解析 RSS/Torznab XML 字节串为条目列表。

    每个条目: {title, link, guid, download_url, size_bytes, seeders}
    download_url 优先级: enclosure.url > link > guid
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_bytes)
    items: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()

        enclosure_url = ""
        length = 0
        enc = item.find("enclosure")
        if enc is not None:
            enclosure_url = (enc.get("url") or "").strip()
            try:
                length = int(enc.get("length") or 0)
            except (TypeError, ValueError):
                length = 0

        seeders = 0
        # torznab:attr（带或不带命名空间）
        for elem in list(item):
            tag = elem.tag.split("}")[-1]
            if tag != "attr":
                continue
            aname = (elem.get("name") or "").lower()
            aval = elem.get("value") or ""
            if aname == "size" and not length:
                try:
                    length = int(float(aval))
                except (TypeError, ValueError):
                    pass
            elif aname == "seeders":
                try:
                    seeders = int(aval)
                except (TypeError, ValueError):
                    pass

        download_url = enclosure_url or link or guid
        items.append(
            {
                "title": title,
                "link": link,
                "guid": guid,
                "download_url": download_url,
                "size_bytes": length,
                "seeders": seeders,
            }
        )
    return items


# ============================================================================
# 去重逻辑（纯函数，操作外部传入的 dict，便于测试）
# ============================================================================
def is_deduped(info_hash: str, dedup_history: Dict[str, dict]) -> bool:
    """info_hash（小写）是否已在去重历史中。"""
    if not info_hash:
        return False
    return info_hash.lower() in (dedup_history or {})


def record_success(info_hash: str, title: str, dedup_history: Dict[str, dict]) -> Dict[str, dict]:
    """提交成功后写入去重历史。返回更新后的 dict（原 dict 被原地修改）。"""
    if not info_hash:
        return dedup_history
    dedup_history[info_hash.lower()] = {
        "title": title,
        "submitted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "success",
    }
    return dedup_history


# ============================================================================
# m115 加密（RSA + XOR 流水线）
# ============================================================================
RSA_N = int(
    "8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021"
    "36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad633"
    "8c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f1"
    "0a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683",
    16,
)
RSA_E = 0x10001
KEY_LENGTH = RSA_N.bit_length() // 8  # 128 bytes

XOR_KEY_SEED = bytes([
    0xf0, 0xe5, 0x69, 0xae, 0xbf, 0xdc, 0xbf, 0x8a, 0x1a, 0x45, 0xe8, 0xbe,
    0x7d, 0xa6, 0x73, 0xb8, 0xde, 0x8f, 0xe7, 0xc4, 0x45, 0xda, 0x86, 0xc4,
    0x9b, 0x64, 0x8b, 0x14, 0x6a, 0xb4, 0xf1, 0xaa, 0x38, 0x01, 0x35, 0x9e,
    0x26, 0x69, 0x2c, 0x86, 0x00, 0x6b, 0x4f, 0xa5, 0x36, 0x34, 0x62, 0xa6,
    0x2a, 0x96, 0x68, 0x18, 0xf2, 0x4a, 0xfd, 0xbd, 0x6b, 0x97, 0x8f, 0x4d,
    0x8f, 0x89, 0x13, 0xb7, 0x6c, 0x8e, 0x93, 0xed, 0x0e, 0x0d, 0x48, 0x3e,
    0xd7, 0x2f, 0x88, 0xd8, 0xfe, 0xfe, 0x7e, 0x86, 0x50, 0x95, 0x4f, 0xd1,
    0xeb, 0x83, 0x26, 0x34, 0xdb, 0x66, 0x7b, 0x9c, 0x7e, 0x9d, 0x7a, 0x81,
    0x32, 0xea, 0xb6, 0x33, 0xde, 0x3a, 0xa9, 0x59, 0x34, 0x66, 0x3b, 0xaa,
    0xba, 0x81, 0x60, 0x48, 0xb9, 0xd5, 0x81, 0x9c, 0xf8, 0x6c, 0x84, 0x77,
    0xff, 0x54, 0x78, 0x26, 0x5f, 0xbe, 0xe8, 0x1e, 0x36, 0x9f, 0x34, 0x80,
    0x5c, 0x45, 0x2c, 0x9b, 0x76, 0xd5, 0x1b, 0x8f, 0xcc, 0xc3, 0xb8, 0xf5,
])

XOR_CLIENT_KEY = bytes([0x78, 0x06, 0xad, 0x4c, 0x33, 0x86, 0x5d, 0x18, 0x4c, 0x01, 0x3f, 0x46])


def generate_key() -> bytes:
    return os.urandom(16)


def xor_derive_key(seed: bytes, size: int) -> bytes:
    key = bytearray(size)
    for i in range(size):
        key[i] = (seed[i] + XOR_KEY_SEED[size * i]) & 0xFF
        key[i] ^= XOR_KEY_SEED[size * (size - i - 1)]
    return bytes(key)


def xor_transform_bytes(data: bytes, key: bytes) -> bytes:
    """对 data 做 XOR 流变换，返回新字节串（自逆运算：再调一次即还原）。"""
    data = bytearray(data)
    ds = len(data)
    ks = len(key)
    mod = ds % 4
    if mod > 0:
        for i in range(mod):
            data[i] ^= key[i % ks]
    for i in range(mod, ds):
        data[i] ^= key[(i - mod) % ks]
    return bytes(data)


def m115_pack(input_data: bytes, key: bytes) -> bytes:
    """在 payload 前拼接 16 字节 key，并做 XOR+反转+XOR 变换，返回 key+payload。"""
    payload = bytearray(input_data)
    payload = bytearray(xor_transform_bytes(bytes(payload), xor_derive_key(key, 4)))
    payload.reverse()
    payload = bytearray(xor_transform_bytes(bytes(payload), XOR_CLIENT_KEY))
    return bytes(key) + bytes(payload)


def m115_unpack(buf: bytes, key: bytes) -> bytes:
    """m115_pack 的逆运算。"""
    payload = bytearray(buf[16:])
    payload = bytearray(xor_transform_bytes(bytes(payload), XOR_CLIENT_KEY))
    payload.reverse()
    payload = bytearray(xor_transform_bytes(bytes(payload), xor_derive_key(key, 4)))
    return bytes(payload)


def rsa_encrypt(input_data: bytes) -> bytes:
    """分块 RSA 公钥加密（PKCS#1 v1.5 type2 填充）。"""
    result = bytearray()
    offset = 0
    while offset < len(input_data):
        slice_size = KEY_LENGTH - 11
        if slice_size > len(input_data) - offset:
            slice_size = len(input_data) - offset
        chunk = input_data[offset:offset + slice_size]
        pad_size = KEY_LENGTH - len(chunk) - 3
        pad_data = os.urandom(pad_size)
        buf = bytearray(KEY_LENGTH)
        buf[0] = 0
        buf[1] = 2
        for i in range(pad_size):
            buf[2 + i] = (pad_data[i] % 0xFF) + 0x01
        buf[pad_size + 2] = 0
        buf[pad_size + 3:] = chunk
        msg = int.from_bytes(bytes(buf), "big")
        ret = pow(msg, RSA_E, RSA_N)
        result.extend(ret.to_bytes(KEY_LENGTH, "big"))
        offset += slice_size
    return bytes(result)


def rsa_decrypt(input_data: bytes) -> bytes:
    """分块 RSA 解密（对应服务端用私钥加密后的数据，客户端用公钥 E 还原）。"""
    result = bytearray()
    offset = 0
    while offset < len(input_data):
        slice_size = min(KEY_LENGTH, len(input_data) - offset)
        chunk = input_data[offset:offset + slice_size]
        msg = int.from_bytes(chunk, "big")
        ret = pow(msg, RSA_E, RSA_N)
        decrypted = ret.to_bytes(KEY_LENGTH, "big")
        for i in range(1, len(decrypted)):
            if decrypted[i] == 0:
                result.extend(decrypted[i + 1:])
                break
        offset += slice_size
    return bytes(result)


def m115_encode(input_data: bytes, key: bytes) -> str:
    """m115 加密：pack 后 RSA 加密再 base64。"""
    packed = m115_pack(input_data, key)
    encrypted = rsa_encrypt(packed)
    return base64.b64encode(encrypted).decode("ascii")


def m115_decode(input_str: str, key: bytes) -> bytes:
    """m115 解密（对应 Go 115driver 库的 Decode，用于解密 115 服务器响应）。

    与 m115_encode 并非直接互逆：
      - encode  是客户端 -> 服务器方向，最后一步用 XOR_CLIENT_KEY。
      - decode 是 服务器 -> 客户端方向，payload 先用 embedded_key 派生的 12 字节
        key 做 XOR（服务器从请求中提取 key 后用它编码响应），再反转，最后用
        调用方传入的 key 派生 4 字节 key 做 XOR。
    """
    data = base64.b64decode(input_str)
    decrypted = rsa_decrypt(data)
    embedded_key = decrypted[:16]
    payload = bytearray(decrypted[16:])
    payload = bytearray(xor_transform_bytes(bytes(payload), xor_derive_key(embedded_key, 12)))
    payload.reverse()
    payload = bytearray(xor_transform_bytes(bytes(payload), xor_derive_key(key, 4)))
    return bytes(payload)


def mask_cookie(cookie: str) -> str:
    """日志安全：只保留前 6 位 + ***"""
    if not cookie:
        return ""
    s = cookie.strip()
    if len(s) <= 6:
        return "***"
    return s[:6] + "***"


def get_proxy_kwargs(proxy_host: str) -> dict:
    """把 MoviePilot 的 PROXY_HOST 转换为 requests 可用参数。"""
    proxy_host = (proxy_host or "").strip()
    if not proxy_host:
        return {}
    return {"proxies": {"http": proxy_host, "https": proxy_host}}


def parse_qrcode_status(payload: dict) -> Tuple[Optional[int], str]:
    """兼容 115 扫码接口的嵌套 data.status 与旧式顶层 status。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("status") is not None:
        return data.get("status"), data.get("msg") or payload.get("msg", "")
    if isinstance(payload, dict):
        return payload.get("status"), payload.get("msg", "")
    return None, ""


def extract_login_cookie(payload: dict) -> Tuple[str, str]:
    """从 115 扫码登录结果提取 Cookie；无完整三件套时拒绝保存。"""
    if not isinstance(payload, dict):
        return "", ""
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    cookie_data = data.get("cookie")
    if not isinstance(cookie_data, dict):
        cookie_data = data
    required = ("UID", "CID", "SEID")
    if not all(cookie_data.get(key) for key in required):
        return "", ""
    ordered_keys = ("UID", "CID", "SEID", "KID")
    cookie = "; ".join(
        f"{key}={cookie_data[key]}" for key in ordered_keys if cookie_data.get(key)
    )
    return cookie, str(cookie_data.get("UID", ""))


def parse_115_folder_items(payload: dict) -> List[Dict[str, str]]:
    """从 115 文件列表响应中提取直属文件夹的 ID 与名称。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = data.get("data") or data.get("list") or data.get("items") or []
    if not isinstance(data, list):
        return []
    result: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # 115 Web API：目录完全没有 fid 键；文件有 fid 键（值即使为空也仍是文件）。
        if "fid" in item:
            continue
        folder_id = item.get("cid") or item.get("file_id") or item.get("id")
        name = item.get("n") or item.get("file_name") or item.get("name")
        if folder_id in (None, "") or not name:
            continue
        result.append({"id": str(folder_id), "name": str(name)})
    return result


def build_115_folder_options(fetch_children, max_folders: int = 2000) -> List[dict]:
    """广度优先构建可搜索的 115 完整路径下拉选项。"""
    options = [{"title": "根目录 /", "value": "0"}]
    queue = [("0", "")]
    seen = {"0"}
    while queue and len(options) <= max_folders:
        parent_id, parent_path = queue.pop(0)
        for folder in fetch_children(parent_id):
            folder_id = str(folder.get("id", ""))
            name = str(folder.get("name", "")).strip()
            if not folder_id or not name or folder_id in seen:
                continue
            seen.add(folder_id)
            path = f"{parent_path}/{name}" if parent_path else f"/{name}"
            options.append({"title": path, "value": folder_id})
            queue.append((folder_id, path))
            if len(options) > max_folders:
                break
    return options


# ============================================================================
# 插件主体
# ============================================================================
UA_115 = "Mozilla/5.0 115Browser/27.0.5.7"


class CloudAutoSearch(_PluginBase):
    # 插件名称
    plugin_name = "115 RSS 离线下载"
    # 插件描述
    plugin_desc = "订阅 RSS 并自动提交种子到 115 离线下载"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.0.9"
    # 插件作者
    plugin_author = "Tsutomu"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudautosearch_"
    # 加载顺序
    plugin_order = 90
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "0 */6 * * *"
    _rss_urls: str = ""
    _include_keywords: str = ""
    _exclude_keywords: str = ""
    _min_size_gb: str = ""
    _max_size_gb: str = ""
    _target_folder_id: str = ""
    _cookie: str = ""
    _user_id: str = ""
    _username: str = ""

    _scheduler = None
    _run_lock = threading.Lock()
    _folder_refresh_lock = threading.Lock()
    _qrcode_token: Dict[str, Any] = {}
    _folder_options_cache: List[dict] = []
    _folder_cache_updated_at: str = ""

    # ------------------------------------------------------------------ init
    def init_plugin(self, config: Optional[dict] = None):
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._cron = config.get("cron") or "0 */6 * * *"
            self._rss_urls = config.get("rss_urls") or ""
            self._include_keywords = config.get("include_keywords") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._min_size_gb = config.get("min_size_gb") or ""
            self._max_size_gb = config.get("max_size_gb") or ""
            self._target_folder_id = config.get("target_folder_id") or ""
            credential = self.get_data("credential") or {}
            self._cookie = credential.get("cookie") or config.get("cookie") or ""
            self._user_id = credential.get("user_id") or config.get("user_id") or ""
            self._username = credential.get("username") or config.get("username") or ""
            if self._cookie and not credential:
                self.save_data("credential", {
                    "cookie": self._cookie, "user_id": self._user_id,
                    "username": self._username,
                })
                self.__update_config()

        folder_cache = self.get_data("folder_options") or {}
        if isinstance(folder_cache, dict):
            items = folder_cache.get("items")
            self._folder_options_cache = items if isinstance(items, list) else []
            self._folder_cache_updated_at = str(folder_cache.get("updated_at") or "")
        else:
            self._folder_options_cache = []
            self._folder_cache_updated_at = ""

        # 目录树可能包含数百个目录，必须后台刷新，绝不能阻塞配置页。
        if self._cookie and self._user_id:
            self._start_folder_refresh()

        self.stop_service()

        if not self._enabled and not self._onlyonce:
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._onlyonce:
            logger.info("115 RSS 离线下载：立即运行一次")
            self._scheduler.add_job(
                name="115 RSS 离线下载-立即",
                func=self._run_task,
                trigger="date",
                run_date=datetime.datetime.now()
                + datetime.timedelta(seconds=3),
            )
            self._onlyonce = False
            self.__update_config()

        if self._enabled and self._cron:
            try:
                self._scheduler.add_job(
                    name="115 RSS 离线下载",
                    func=self._run_task,
                    trigger=CronTrigger.from_crontab(self._cron),
                )
                logger.info(f"115 RSS 离线下载：已按 cron '{self._cron}' 注册定时任务")
            except Exception as err:
                logger.error(f"115 RSS 离线下载：cron 配置错误: {err}")

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "rss_urls": self._rss_urls,
                "include_keywords": self._include_keywords,
                "exclude_keywords": self._exclude_keywords,
                "min_size_gb": self._min_size_gb,
                "max_size_gb": self._max_size_gb,
                "target_folder_id": self._target_folder_id,
            }
        )

    # ------------------------------------------------------------- HTTP helpers
    def _headers(self, with_cookie: bool = True) -> Dict[str, str]:
        h = {"User-Agent": UA_115}
        if with_cookie and self._cookie:
            h["Cookie"] = self._cookie
        return h

    def _http_get(self, url: str, params: Optional[dict] = None, timeout: int = 30):
        if requests is None:
            raise RuntimeError("requests 库不可用")
        return requests.get(
            url, params=params, headers=self._headers(), timeout=timeout,
            **get_proxy_kwargs(getattr(settings, "PROXY_HOST", "")),
        )

    def _http_post(self, url: str, data: Optional[dict] = None,
                   params: Optional[dict] = None, timeout: int = 30):
        if requests is None:
            raise RuntimeError("requests 库不可用")
        return requests.post(
            url, params=params, data=data, headers=self._headers(), timeout=timeout,
            **get_proxy_kwargs(getattr(settings, "PROXY_HOST", "")),
        )

    # ------------------------------------------------------------- 115 login
    def _fetch_qrcode_token(self) -> Optional[Dict[str, Any]]:
        try:
            r = self._http_get(
                "https://qrcodeapi.115.com/api/1.0/web/1.0/token", timeout=15
            )
            j = r.json()
            if j.get("state") and j.get("data"):
                return j["data"]
        except Exception as e:
            logger.error(f"115 RSS 离线下载：获取 115 二维码 token 失败: {e}")
        return None

    def _qrcode_to_data_url(self, content: str) -> str:
        try:
            import qrcode  # type: ignore
            import io

            img = qrcode.make(content)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except Exception:
            return ""

    def _check_login(self) -> bool:
        if not self._cookie:
            return False
        try:
            r = self._http_get(
                "https://passportapi.115.com/app/1.0/web/1.0/check/sso",
                params={"_": int(time.time() * 1000)},
                timeout=15,
            )
            j = r.json()
            if j.get("state") and j.get("data", {}).get("user_id"):
                if not self._user_id:
                    self._user_id = str(j["data"]["user_id"])
                return True
        except Exception as e:
            logger.warning(f"115 RSS 离线下载：校验 115 登录态失败: {e}")
        return False


    def _fetch_folder_children(self, parent_id: str) -> List[Dict[str, str]]:
        """分页获取指定 115 目录下的直属子目录。"""
        folders: List[Dict[str, str]] = []
        offset = 0
        page_size = 1000
        while True:
            r = self._http_get(
                "https://aps.115.com/natsort/files.php",
                params={
                    "aid": 1,
                    "cid": str(parent_id or "0"),
                    "show_dir": 1,
                    "nf": 1,
                    "cur": 1,
                    "limit": page_size,
                    "offset": offset,
                    "fc_mix": 0,
                    "asc": 1,
                },
                timeout=20,
            )
            if r.status_code != 200:
                raise RuntimeError(f"目录接口 HTTP {r.status_code}")
            try:
                payload = r.json()
            except Exception as e:
                content_type = r.headers.get("content-type", "")
                raise RuntimeError(
                    f"目录接口返回非 JSON（{content_type or '未知类型'}）"
                ) from e
            if not payload.get("state"):
                raise RuntimeError(payload.get("error") or payload.get("message") or "读取目录失败")
            page = parse_115_folder_items(payload)
            folders.extend(page)
            raw_data = payload.get("data")
            raw_count = len(raw_data) if isinstance(raw_data, list) else len(page)
            total = payload.get("count")
            offset += raw_count
            if raw_count == 0 or raw_count < page_size:
                break
            if isinstance(total, int) and offset >= total:
                break
        return folders

    def _get_folder_options(self) -> List[dict]:
        """只读本地缓存，绝不在配置页请求 115。"""
        cached = self._folder_options_cache
        if isinstance(cached, list) and cached:
            return list(cached)
        fallback = [{"title": "根目录 /", "value": "0"}]
        if self._target_folder_id and self._target_folder_id != "0":
            fallback.append({
                "title": str(self._target_folder_id),
                "value": str(self._target_folder_id),
            })
        return fallback

    def _resolve_target_folder_id(self, folder: Optional[str] = None) -> str:
        """将下拉值、目录名称或 115 完整路径解析成目录 ID。"""
        raw = str(self._target_folder_id if folder is None else folder).strip()
        if not raw or raw == "/":
            return "0"
        if raw.isdigit():
            return raw

        # 优先命中本地缓存：既支持完整路径，也支持唯一目录名称。
        exact = []
        basename = []
        raw_lower = raw.rstrip("/").lower()
        for option in self._get_folder_options():
            title = str(option.get("title") or "").strip()
            value = str(option.get("value") or "").strip()
            if not title or not value:
                continue
            normalized = title.replace("根目录 ", "").rstrip("/") or "/"
            if normalized.lower() == raw_lower:
                exact.append(value)
            if normalized.rsplit("/", 1)[-1].lower() == raw_lower:
                basename.append(value)
        if exact:
            return exact[0]
        if len(set(basename)) == 1:
            return basename[0]
        if len(set(basename)) > 1:
            raise RuntimeError("存在同名目录，请输入 115 完整路径")

        # 未命中缓存时按完整路径实时解析；裸名称按根目录下的目录处理。
        path = raw if raw.startswith("/") else f"/{raw}"
        r = self._http_get(
            "https://webapi.115.com/files/getid",
            params={"path": path},
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(f"目录路径解析 HTTP {r.status_code}")
        try:
            payload = r.json()
        except Exception as e:
            raise RuntimeError("目录路径解析返回非 JSON") from e
        if not payload.get("state") or payload.get("id") is None:
            raise RuntimeError(
                payload.get("error") or payload.get("message")
                or f"找不到 115 目录：{path}"
            )
        return str(payload.get("id"))

    def _start_folder_refresh(self) -> bool:
        """启动单个后台目录刷新任务；配置页无需等待。"""
        if not self._cookie or not self._user_id or self._folder_refresh_lock.locked():
            return False
        threading.Thread(target=self._refresh_folder_cache, daemon=True).start()
        return True

    def _refresh_folder_cache(self):
        """只缓存根目录建议；子目录可直接输入完整路径，避免递归触发 115 风控。"""
        if not self._folder_refresh_lock.acquire(blocking=False):
            return
        try:
            folders = self._fetch_folder_children("0")
            options = [{"title": "根目录 /", "value": "0"}]
            options.extend(
                {"title": f"/{item['name']}", "value": str(item["id"])}
                for item in folders
                if item.get("id") and item.get("name")
            )
            updated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._folder_options_cache = options
            self._folder_cache_updated_at = updated_at
            self.save_data("folder_options", {
                "items": options,
                "updated_at": updated_at,
            })
            logger.info(
                f"115 RSS 离线下载：已缓存 {len(options)} 个根目录建议；"
                "子目录可直接输入完整路径"
            )
        except Exception as e:
            logger.warning(f"115 RSS 离线下载：后台刷新 115 根目录失败: {e}")
        finally:
            self._folder_refresh_lock.release()

    # ------------------------------------------------------- 115 offline task
    def _submit_offline_task(self, magnet: str) -> bool:
        """通过 m115 加密提交 magnet 到 115 离线下载。成功返回 True。"""
        if not self._cookie or not self._user_id:
            logger.error("115 RSS 离线下载：未登录 115，无法提交离线任务")
            return False
        try:
            key = generate_key()
            target_folder_id = self._resolve_target_folder_id()
            payload = {
                "ac": "add_task_urls",
                "wp_path_id": target_folder_id,
                "app_ver": "27.0.5.7",
                "uid": str(self._user_id),
                "url[0]": magnet,
            }
            import json

            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            encrypted_b64 = m115_encode(raw, key)

            r = self._http_post(
                "https://lixian.115.com/lixianssp/",
                params={"ac": "add_task_urls", "t": int(time.time() * 1000)},
                data={"data": encrypted_b64},
                timeout=30,
            )
            j = r.json()
            encoded_data = j.get("encoded_data") or ""
            if encoded_data:
                decoded = m115_decode(encoded_data, key)
                try:
                    resp = json.loads(decoded.decode("utf-8", errors="replace"))
                    if resp.get("state"):
                        return True
                    logger.warning(f"115 RSS 离线下载：115 返回失败: {resp}")
                except Exception:
                    logger.warning("115 RSS 离线下载：115 响应解密后解析失败")
                    return False
            # 兼容未加密响应
            if j.get("state"):
                return True
            logger.warning(f"115 RSS 离线下载：提交离线任务返回: {j}")
            return False
        except Exception as e:
            logger.error(f"115 RSS 离线下载：提交离线任务异常: {e}")
            return False

    # ----------------------------------------------------------------- runner
    def _parse_size_gb(self, raw: str) -> Optional[float]:
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _resolve_magnet(self, item: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """根据 RSS 条目解析出 (magnet, info_hash)。"""
        url = item.get("download_url") or ""
        if not url:
            return None, None
        if url.startswith("magnet:"):
            return url, extract_info_hash_from_magnet(url)
        # .torrent 链接：下载并解析
        try:
            r = self._http_get(url, timeout=30)
            magnet, _name = torrent_to_magnet(r.content)
            return magnet, extract_info_hash_from_magnet(magnet)
        except Exception as e:
            logger.error(f"115 RSS 离线下载：解析 torrent 失败 {url}: {e}")
            return None, None

    def _run_task(self, manual: bool = False):
        if not self._run_lock.acquire(blocking=False):
            logger.warning("115 RSS 离线下载：已有任务在运行，跳过本次")
            return
        stats = {
            "start_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "manual": manual,
            "rss_count": 0,
            "items_total": 0,
            "filtered": 0,
            "duplicated": 0,
            "submitted": 0,
            "failed": 0,
            "status": "success",
            "error": "",
        }
        try:
            if not self._check_login():
                logger.warning("115 RSS 离线下载：115 未登录或登录态失效，跳过")
                stats["status"] = "skipped"
                stats["error"] = "未登录"
                return

            dedup_history: Dict[str, dict] = self.get_data("dedup_history") or {}

            include_kw = parse_keywords(self._include_keywords)
            exclude_kw = parse_keywords(self._exclude_keywords)
            min_gb = self._parse_size_gb(self._min_size_gb)
            max_gb = self._parse_size_gb(self._max_size_gb)

            rss_urls = [u.strip() for u in (self._rss_urls or "").splitlines() if u.strip()]
            stats["rss_count"] = len(rss_urls)

            for rss_url in rss_urls:
                try:
                    r = self._http_get(rss_url, timeout=30)
                    items = parse_rss_items(r.content)
                except Exception as e:
                    logger.error(f"115 RSS 离线下载：RSS 解析失败 {rss_url}: {e}")
                    continue

                for item in items:
                    stats["items_total"] += 1
                    if not passes_filter(
                        item.get("title", ""),
                        item.get("size_bytes", 0),
                        include_kw,
                        exclude_kw,
                        min_gb,
                        max_gb,
                    ):
                        stats["filtered"] += 1
                        continue

                    magnet, info_hash = self._resolve_magnet(item)
                    if not magnet or not info_hash:
                        stats["failed"] += 1
                        continue

                    if is_deduped(info_hash, dedup_history):
                        stats["duplicated"] += 1
                        continue

                    ok = self._submit_offline_task(magnet)
                    if ok:
                        record_success(info_hash, item.get("title", ""), dedup_history)
                        self.save_data("dedup_history", dedup_history)
                        stats["submitted"] += 1
                        logger.info(
                            f"115 RSS 离线下载：已提交 {item.get('title', '')} "
                            f"({info_hash})"
                        )
                    else:
                        stats["failed"] += 1
                        logger.warning(
                            f"115 RSS 离线下载：提交失败，下轮重试 {item.get('title','')} "
                            f"({info_hash})"
                        )

            logger.info(
                f"115 RSS 离线下载：完成 共{stats['items_total']} 过滤{stats['filtered']} "
                f"重复{stats['duplicated']} 成功{stats['submitted']} 失败{stats['failed']}"
            )
        except Exception as e:
            stats["status"] = "failed"
            stats["error"] = str(e)
            logger.error(f"115 RSS 离线下载：运行异常: {e}\n{traceback.format_exc()}")
        finally:
            stats["end_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                run_log: List[dict] = self.get_data("run_log") or []
                run_log.insert(0, stats)
                self.save_data("run_log", run_log[:20])
            except Exception:
                pass
            self._run_lock.release()

    # ------------------------------------------------------------------ API
    def api_qrcode(self):
        data = self._fetch_qrcode_token()
        if not data:
            return schemas.Response(success=False, message="获取二维码失败")
        uid = data.get("uid", "")
        self._qrcode_token = data
        qr_content = data.get("qrcode", "")
        image = self._qrcode_to_data_url(qr_content)
        return schemas.Response(
            success=True,
            data={
                "uid": uid,
                "qrcode_text": qr_content,
                "image": image,
                "need_manual": not bool(image),
            },
        )

    def api_qrcode_status(self, uid: Optional[str] = None):
        token = self._qrcode_token or {}
        uid = uid or token.get("uid")
        if not uid:
            return schemas.Response(success=False, message="缺少 uid")
        try:
            r = self._http_get(
                "https://qrcodeapi.115.com/get/status/",
                params={
                    "uid": uid,
                    "time": token.get("time", 0),
                    "sign": token.get("sign", ""),
                    "_": int(time.time() * 1000),
                },
                timeout=15,
            )
            j = r.json()
        except Exception as e:
            return schemas.Response(success=False, message=f"查询失败: {e}")

        status, status_message = parse_qrcode_status(j)
        result = {"status": status, "message": status_message}

        if status == 2:
            # 已确认登录，换取 cookie
            try:
                r2 = self._http_post(
                    "https://qrcodeapi.115.com/app/1.0/web/1.0/login/qrcode/",
                    data={"account": uid, "app": "web"},
                    timeout=15,
                )
                j2 = r2.json()
                if j2.get("state"):
                    cookie, user_id = extract_login_cookie(j2)
                    if not cookie:
                        result["message"] = "登录结果未返回完整 Cookie"
                    else:
                        self._cookie = cookie
                        self._user_id = user_id
                        self.save_data("credential", {
                            "cookie": self._cookie, "user_id": self._user_id,
                            "username": self._username,
                        })
                        self.__update_config()
                        self._start_folder_refresh()
                        logger.info(
                            f"115 RSS 离线下载：115 扫码登录成功 cookie={mask_cookie(cookie)}"
                        )
                        result["cookie_saved"] = True
                        result["user_id"] = self._user_id
            except Exception as e:
                result["message"] = f"登录换取 cookie 失败: {e}"
        return schemas.Response(success=True, data=result)

    def api_logout(self):
        self._cookie = ""
        self._user_id = ""
        self._username = ""
        self.del_data("credential")
        self.del_data("folder_options")
        self._folder_options_cache = []
        self._folder_cache_updated_at = ""
        self.__update_config()
        logger.info("115 RSS 离线下载：已退出 115 登录")
        return schemas.Response(success=True, message="已退出登录")

    def api_status(self):
        dedup_history = self.get_data("dedup_history") or {}
        run_log = self.get_data("run_log") or []
        rss_count = len([u for u in (self._rss_urls or "").splitlines() if u.strip()])
        return schemas.Response(
            success=True,
            data={
                "logged_in": bool(self._cookie),
                "username": self._username,
                "user_id": self._user_id,
                "last_run": run_log[0] if run_log else None,
                "total_submitted": len(dedup_history),
                "rss_count": rss_count,
                "folder_count": len(self._folder_options_cache or []),
                "folder_cache_updated_at": self._folder_cache_updated_at,
                "folder_refreshing": self._folder_refresh_lock.locked(),
            },
        )

    def api_run(self):
        threading.Thread(target=self._run_task, kwargs={"manual": True}, daemon=True).start()
        return schemas.Response(success=True, message="已触发手动运行")

    def api_refresh_folders(self):
        if not self._cookie or not self._user_id:
            return schemas.Response(success=False, message="请先登录 115")
        started = self._start_folder_refresh()
        return schemas.Response(
            success=True,
            message="已开始后台刷新目录" if started else "目录刷新已在运行",
        )

    def api_test_rss(self, url: str = ""):
        url = (url or "").strip()
        if not url:
            return schemas.Response(success=False, message="缺少 url")
        try:
            r = self._http_get(url, timeout=30)
            items = parse_rss_items(r.content)
            sample = [
                {"title": it.get("title"), "size_bytes": it.get("size_bytes"),
                 "download_url": it.get("download_url")[:80]}
                for it in items[:5]
            ]
            return schemas.Response(
                success=True, data={"count": len(items), "sample": sample}
            )
        except Exception as e:
            return schemas.Response(success=False, message=f"解析失败: {e}")

    # ------------------------------------------------------------ base hooks
    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/qrcode", "endpoint": self.api_qrcode, "methods": ["GET"],
             "auth": "bear", "summary": "获取 115 登录二维码"},
            {"path": "/qrcode_status", "endpoint": self.api_qrcode_status,
             "methods": ["GET"], "auth": "bear", "summary": "查询扫码状态"},
            {"path": "/logout", "endpoint": self.api_logout, "methods": ["POST"],
             "auth": "bear", "summary": "退出 115 登录"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "插件状态"},
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "手动触发运行"},
            {"path": "/refresh_folders", "endpoint": self.api_refresh_folders,
             "methods": ["POST"], "auth": "bear", "summary": "后台刷新 115 目录缓存"},
            {"path": "/test_rss", "endpoint": self.api_test_rss, "methods": ["POST"],
             "auth": "bear", "summary": "测试 RSS 链接"},
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        folder_options = self._get_folder_options()
        folder_hint = (
            f"可选择已缓存目录，也可直接输入完整路径（如 /下载/动漫）；"
            f"缓存更新时间：{self._folder_cache_updated_at or '后台刷新中'}。"
        )
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "icon": "mdi-information",
                                    "text": "定时轮询多条 RSS，自动过滤并提交到 115 离线下载。仅提交成功的资源会写入去重，失败下轮自动重试。",
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center text-h6 py-3"},
                        "content": [
                            {"component": "VIcon",
                             "props": {"icon": "mdi-cog", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "基础设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "class": "align-center mb-2",
                                "content": [
                                    {"component": "VCol", "props": {"cols": 12, "sm": 4},
                                     "content": [{"component": "VSwitch", "props": {
                                         "model": "enabled", "label": "启用插件",
                                         "color": "primary"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "sm": 4},
                                     "content": [{"component": "VSwitch", "props": {
                                         "model": "onlyonce", "label": "立即运行一次"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "sm": 4},
                                     "content": [{"component": "VCronField", "props": {
                                         "model": "cron", "label": "定时周期",
                                         "placeholder": "0 */6 * * *",
                                         "variant": "outlined"}}]},
                                ],
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center text-h6 py-3"},
                        "content": [
                            {"component": "VIcon",
                             "props": {"icon": "mdi-rss", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "RSS 与过滤"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {"component": "VRow", "class": "mb-2", "content": [
                                {"component": "VCol", "props": {"cols": 12}, "content": [
                                    {"component": "VTextarea", "props": {
                                        "model": "rss_urls", "label": "RSS 地址（每行一个）",
                                        "rows": 4, "placeholder": "https://example.com/rss.xml",
                                        "variant": "outlined"}}]}]},
                            {"component": "VRow", "class": "mb-2", "content": [
                                {"component": "VCol", "props": {"cols": 12, "sm": 6}, "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "include_keywords",
                                        "label": "包含关键词（逗号分隔，空=不过滤）",
                                        "variant": "outlined"}}]},
                                {"component": "VCol", "props": {"cols": 12, "sm": 6}, "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "exclude_keywords",
                                        "label": "排除关键词（逗号分隔）",
                                        "variant": "outlined"}}]},
                            ]},
                            {"component": "VRow", "class": "mb-2", "content": [
                                {"component": "VCol", "props": {"cols": 12, "sm": 6}, "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "min_size_gb", "label": "最小体积 (GB)",
                                        "placeholder": "空=不限制", "variant": "outlined"}}]},
                                {"component": "VCol", "props": {"cols": 12, "sm": 6}, "content": [
                                    {"component": "VTextField", "props": {
                                        "model": "max_size_gb", "label": "最大体积 (GB)",
                                        "placeholder": "空=不限制", "variant": "outlined"}}]},
                            ]},
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center text-h6 py-3"},
                        "content": [
                            {"component": "VIcon",
                             "props": {"icon": "mdi-cloud", "color": "primary", "class": "mr-2"}},
                            {"component": "span", "text": "115 设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {"component": "VRow", "class": "mb-2", "content": [
                                {"component": "VCol", "props": {"cols": 12}, "content": [
                                    {"component": "VCombobox", "props": {
                                        "model": "target_folder_id",
                                        "label": "115 目标目录（可选或输入完整路径）",
                                        "items": folder_options,
                                        "variant": "outlined",
                                        "clearable": False,
                                        "persistent-hint": True,
                                        "hint": folder_hint,
                                        "no-data-text": "可直接输入 /下载/动漫"}}]}]},
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "0 */6 * * *",
            "rss_urls": "",
            "include_keywords": "",
            "exclude_keywords": "",
            "min_size_gb": "",
            "max_size_gb": "",
            "target_folder_id": "",
        }

    def get_page(self) -> List[dict]:
        run_log = self.get_data("run_log") or []
        if not run_log:
            return [
                {"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": "暂无运行记录"}}
            ]
        rows = []
        for r in run_log[:10]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-caption"},
                     "content": [{"component": "span", "text": str(r.get("start_time", ""))}]},
                    {"component": "td", "props": {"class": "text-caption"},
                     "content": [{"component": "span", "text": str(r.get("status", ""))}]},
                    {"component": "td", "props": {"class": "text-caption"},
                     "content": [{"component": "span",
                                  "text": f"提交 {r.get('submitted',0)} / 失败 {r.get('failed',0)} / 重复 {r.get('duplicated',0)}"}]},
                ],
            })
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-h6 py-2"},
                     "content": [{"component": "span", "text": "最近运行记录"}]},
                    {"component": "VTable", "props": {"density": "compact"},
                     "content": [
                         {"component": "thead", "content": [{
                             "component": "tr", "content": [
                                 {"component": "th", "content": [{"component": "span", "text": "时间"}]},
                                 {"component": "th", "content": [{"component": "span", "text": "状态"}]},
                                 {"component": "th", "content": [{"component": "span", "text": "统计"}]},
                             ]}]},
                         {"component": "tbody", "content": rows},
                     ]},
                ],
            }
        ]

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if getattr(self._scheduler, "running", False):
                    self._scheduler.shutdown(wait=False)
            except Exception as e:
                logger.error(f"115 RSS 离线下载：停止定时任务失败: {e}")
            self._scheduler = None
