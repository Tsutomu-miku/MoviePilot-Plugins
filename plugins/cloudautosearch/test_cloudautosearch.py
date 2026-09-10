# -*- coding: utf-8 -*-
"""
CloudAutoSearch 插件单元测试。

设计为可在无 MoviePilot 环境下独立运行：
    cd /home/tsutomu/workspace/MoviePilot-Plugins/plugins/cloudautosearch
    python3 -m pytest test_cloudautosearch.py -v

覆盖：
1. bencode 编解码 & torrent -> magnet（info_hash 计算）
2. 过滤逻辑（包含/排除/大小）
3. 去重（info_hash 小写 key）
4. 失败重试（提交失败不写入去重，下轮可重试）
5. m115 加解密 round-trip（用恒等 RSA 桩验证 XOR 流水线）
6. RSS / Torznab 解析
"""

import base64
import hashlib
import os
import sys

import pytest

# 让本测试在无 MoviePilot 环境下也能 import 到插件模块
PLUGINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGINS_DIR not in sys.path:
    sys.path.insert(0, PLUGINS_DIR)

import cloudautosearch as cas  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助：构造一个最小 torrent 文件字节串
# ---------------------------------------------------------------------------
def _build_minimal_torrent(name=b"Test.Movie.2024.2160p.WEB-DL",
                           length=10 * 1024 ** 3,
                           announce=b"http://tracker.example.com/announce"):
    info = {
        b"name": name,
        b"length": length,
        b"piece length": 16384,
        b"pieces": b"\x00" * 20,
    }
    meta = {b"announce": announce, b"info": info}
    return cas.bencode_encode(meta), info


# ===========================================================================
# 1. torrent -> magnet / info_hash
# ===========================================================================
class TestTorrentToMagnet:
    def test_info_hash_matches_sha1_of_info_dict(self):
        torrent_bytes, info = _build_minimal_torrent()
        magnet, name = cas.torrent_to_magnet(torrent_bytes)

        expected_hash = hashlib.sha1(cas.bencode_encode(info)).hexdigest().upper()
        assert magnet.startswith("magnet:?xt=urn:btih:" + expected_hash)
        assert name == "Test.Movie.2024.2160p.WEB-DL"
        assert "dn=" in magnet
        # tracker 会被 urlencode（: 与 / 被转义）
        assert "tr=http%3A//tracker.example.com/announce" in magnet

    def test_bencode_roundtrip(self):
        obj = {b"a": 1, b"b": [b"x", 2], b"c": {b"d": b"e"}}
        encoded = cas.bencode_encode(obj)
        decoded = cas.bencode_decode(encoded)
        assert decoded == obj

    def test_extract_info_hash_from_magnet(self):
        magnet = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=foo"
        h = cas.extract_info_hash_from_magnet(magnet)
        assert h == "abcdef0123456789abcdef0123456789abcdef01"  # 小写
        assert cas.extract_info_hash_from_magnet("not a magnet") is None
        assert cas.extract_info_hash_from_magnet("") is None


# ===========================================================================
# 2. 过滤逻辑
# ===========================================================================
class TestPassesFilter:
    def test_empty_include_passes_all(self):
        assert cas.passes_filter("任意标题", 100, [], [], None, None) is True

    def test_include_keyword_match(self):
        assert cas.passes_filter("复仇者联盟4", 100, ["复仇者"], [], None, None) is True
        assert cas.passes_filter("蜘蛛侠", 100, ["复仇者"], [], None, None) is False

    def test_include_keyword_case_insensitive(self):
        assert cas.passes_filter("Marvel Movie", 100, ["marvel"], [], None, None) is True

    def test_exclude_keyword(self):
        assert cas.passes_filter("WebRip 1080p", 100, [], ["WebRip"], None, None) is False
        assert cas.passes_filter("BluRay 1080p", 100, [], ["WebRip"], None, None) is True

    def test_min_size_gb(self):
        size_5gb = 5 * 1024 ** 3
        # 要求最小 10GB，5GB 不通过
        assert cas.passes_filter("x", size_5gb, [], [], 10.0, None) is False
        # 15GB 通过
        assert cas.passes_filter("x", 15 * 1024 ** 3, [], [], 10.0, None) is True

    def test_max_size_gb(self):
        size_20gb = 20 * 1024 ** 3
        assert cas.passes_filter("x", size_20gb, [], [], None, 10.0) is False
        assert cas.passes_filter("x", 5 * 1024 ** 3, [], [], None, 10.0) is True

    def test_unknown_size_zero_is_ignored_for_size_filter(self):
        # size_bytes=0 视为未知，大小过滤不生效
        assert cas.passes_filter("x", 0, [], [], 10.0, None) is True
        assert cas.passes_filter("x", 0, [], [], None, 10.0) is True

    def test_parse_keywords(self):
        assert cas.parse_keywords("a, b，c\n d") == ["a", "b", "c", "d"]
        assert cas.parse_keywords("") == []
        assert cas.parse_keywords(None) == []


# ===========================================================================
# 3. 去重
# ===========================================================================
class TestDedup:
    def test_is_deduped_case_insensitive(self):
        history = {"abcdef0123456789abcdef0123456789abcdef01": {"title": "x"}}
        assert cas.is_deduped("ABCDEF0123456789ABCDEF0123456789ABCDEF01", history) is True
        assert cas.is_deduped("0000000000000000000000000000000000000000", history) is False
        assert cas.is_deduped("", history) is False
        assert cas.is_deduped("abc", {}) is False

    def test_record_success_writes_lowercase(self):
        history = {}
        cas.record_success("ABCDEF0123456789ABCDEF0123456789ABCDEF01", "电影A", history)
        assert "abcdef0123456789abcdef0123456789abcdef01" in history
        assert history["abcdef0123456789abcdef0123456789abcdef01"]["title"] == "电影A"
        assert history["abcdef0123456789abcdef0123456789abcdef01"]["status"] == "success"


# ===========================================================================
# 4. 失败重试（模拟提交流程）
# ===========================================================================
class TestFailedRetry:
    def _simulate_run(self, dedup_history, submit_callable, items):
        """模拟插件 _run_task 中对每个条目的提交-去重逻辑。"""
        submitted = []
        for it in items:
            ih = it["info_hash"]
            if cas.is_deduped(ih, dedup_history):
                continue
            ok = submit_callable(it)
            if ok:
                cas.record_success(ih, it["title"], dedup_history)
                submitted.append(ih)
        return submitted

    def test_failure_not_recorded_allows_retry(self):
        history = {}
        items = [
            {"title": "电影A", "info_hash": "1111111111111111111111111111111111111111"},
            {"title": "电影B", "info_hash": "2222222222222222222222222222222222222222"},
        ]
        call_log = []

        # 第一轮：A 提交失败，B 提交成功
        def submit(it):
            call_log.append(it["title"])
            return it["title"] == "电影B"

        submitted = self._simulate_run(history, submit, items)
        assert submitted == ["2222222222222222222222222222222222222222"]
        # 失败的 A 没有写入去重
        assert cas.is_deduped("1111111111111111111111111111111111111111", history) is False
        # 成功的 B 已写入
        assert cas.is_deduped("2222222222222222222222222222222222222222", history) is True

        # 第二轮：A 这次成功
        submitted2 = self._simulate_run(history, lambda it: True, items)
        assert submitted2 == ["1111111111111111111111111111111111111111"]
        # B 已被去重跳过
        assert cas.is_deduped("1111111111111111111111111111111111111111", history) is True

    def test_duplicate_skipped_next_round(self):
        history = {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {"title": "old"}}
        items = [{"title": "电影A", "info_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
        submitted = self._simulate_run(history, lambda it: True, items)
        assert submitted == []  # 已去重，不会再提交


# ===========================================================================
# 5. m115 加解密 round-trip
# ===========================================================================
class TestM115:
    def test_encode_output_format(self, monkeypatch):
        """m115_encode 输出应为 base64，RSA 块长度固定（128 字节/块）。"""
        monkeypatch.setattr(cas, "rsa_encrypt", lambda x: x)
        key = cas.generate_key()
        payload = b'{"state":true,"url[0]":"magnet:?xt=urn:btih:abc"}'
        encoded = cas.m115_encode(payload, key)
        # base64 可解码
        raw = base64.b64decode(encoded)
        # 前 16 字节应为我们写入的 key
        assert raw[:16] == key
        # payload 确实被变换过（不等于原始数据）
        assert raw[16:] != payload

    def test_decode_server_response(self, monkeypatch):
        """模拟 115 服务器响应并用 m115_decode 还原。

        服务器端编码（m115_decode 的逆过程）：
            P ^= derive(key, 4) -> reverse -> P ^= derive(embedded_key, 12)
        然后在前面拼接 embedded_key，再（此处用恒等 RSA）base64。
        """
        monkeypatch.setattr(cas, "rsa_encrypt", lambda x: x)
        monkeypatch.setattr(cas, "rsa_decrypt", lambda x: x)

        key = cas.generate_key()
        embedded_key = key  # 服务器从请求中提取到的 key，原样回显
        for original in [
            b"",
            b"a",
            b'{"state":true,"result":[{"info_hash":"abc"}]}',
            bytes(range(256)) * 3,
        ]:
            # 服务器端正向编码
            payload = bytearray(original)
            payload = bytearray(cas.xor_transform_bytes(bytes(payload), cas.xor_derive_key(key, 4)))
            payload.reverse()
            payload = bytearray(cas.xor_transform_bytes(bytes(payload), cas.xor_derive_key(embedded_key, 12)))
            blob = bytes(embedded_key) + bytes(payload)
            encoded_str = base64.b64encode(blob).decode("ascii")

            decoded = cas.m115_decode(encoded_str, key)
            assert decoded == original, f"server-response decode failed for len={len(original)}"

    def test_pack_unpack_is_inverse(self, monkeypatch):
        """pack/unpack 是自实现的真正互逆 XOR 流水线（用于内部测试）。"""
        monkeypatch.setattr(cas, "rsa_encrypt", lambda x: x)
        monkeypatch.setattr(cas, "rsa_decrypt", lambda x: x)
        key = cas.generate_key()
        payload = b"hello 115 offline task"
        packed = cas.m115_pack(payload, key)
        # 前 16 字节是 key
        assert packed[:16] == key
        assert packed[16:] != payload  # 确实被变换过
        assert cas.m115_unpack(packed, key) == payload

    def test_rsa_functions_produce_fixed_length_blocks(self):
        # 真实 RSA 加密每块 128 字节
        out = cas.rsa_encrypt(b"short data")
        assert len(out) == cas.KEY_LENGTH  # 128
        assert cas.KEY_LENGTH == 128

    def test_mask_cookie_does_not_leak(self):
        assert cas.mask_cookie("UID=abcdef; CID=xyz") == "UID=ab***"
        assert cas.mask_cookie("") == ""
        assert cas.mask_cookie("12") == "***"


# ===========================================================================
# 6. RSS / Torznab 解析
# ===========================================================================
class TestRssParse:
    def _rss_xml(self):
        return b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Test RSS</title>
    <item>
      <title>Test.Movie.2024.2160p.WEB-DL</title>
      <link>https://example.com/dl/123</link>
      <guid>guid-123</guid>
      <pubDate>Wed, 09 Sep 2026 10:00:00 +0800</pubDate>
      <enclosure url="https://example.com/dl/123.torrent" length="10737418240" type="application/x-bittorrent"/>
      <torznab:attr name="size" value="10737418240"/>
      <torznab:attr name="seeders" value="42"/>
    </item>
    <item>
      <title>Another.Movie.2020</title>
      <guid>guid-456</guid>
      <enclosure url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111"/>
    </item>
  </channel>
</rss>"""

    def test_parse_items_count_and_fields(self):
        items = cas.parse_rss_items(self._rss_xml())
        assert len(items) == 2

        it0 = items[0]
        assert it0["title"] == "Test.Movie.2024.2160p.WEB-DL"
        # download_url 优先 enclosure.url
        assert it0["download_url"] == "https://example.com/dl/123.torrent"
        assert it0["size_bytes"] == 10 * 1024 ** 3
        assert it0["seeders"] == 42

        it1 = items[1]
        assert it1["title"] == "Another.Movie.2020"
        assert it1["download_url"].startswith("magnet:")
        assert cas.extract_info_hash_from_magnet(it1["download_url"]) == \
            "1111111111111111111111111111111111111111"

    def test_parse_empty_rss(self):
        xml = b"""<?xml version="1.0"?><rss><channel><title>x</title></channel></rss>"""
        assert cas.parse_rss_items(xml) == []

    def test_end_to_end_filter_and_dedup_pipeline(self):
        """综合：解析 RSS -> 过滤 -> 去重 -> 提交成功写入。"""
        items = cas.parse_rss_items(self._rss_xml())
        include_kw = ["2024"]
        history = {}
        kept = []
        for it in items:
            if not cas.passes_filter(it["title"], it["size_bytes"],
                                     include_kw, [], None, None):
                continue
            ih = cas.extract_info_hash_from_magnet(it["download_url"]) or "torrent-hash"
            if cas.is_deduped(ih, history):
                continue
            # 模拟提交成功
            cas.record_success(ih, it["title"], history)
            kept.append(it["title"])
        # 只有包含 2024 的条目通过
        assert kept == ["Test.Movie.2024.2160p.WEB-DL"]
        assert len(history) == 1


# ===========================================================================
# 7. 115 扫码状态与代理
# ===========================================================================
class TestQrcodeCompatibility:
    def test_nested_status_is_parsed(self):
        assert cas.parse_qrcode_status(
            {"state": True, "data": {"status": 2}, "msg": "ok"}
        ) == (2, "ok")

    def test_top_level_status_remains_compatible(self):
        assert cas.parse_qrcode_status(
            {"status": 1, "msg": "scanned"}
        ) == (1, "scanned")

    def test_moviepilot_proxy_is_applied_to_both_schemes(self):
        proxy = "http://mihomo:7890"
        assert cas.get_proxy_kwargs(proxy) == {
            "proxies": {"http": proxy, "https": proxy}
        }
        assert cas.get_proxy_kwargs("") == {}


class TestLoginCookieExtraction:
    def test_nested_cookie_dict_is_extracted(self):
        cookie, user_id = cas.extract_login_cookie({
            "state": True,
            "data": {"cookie": {
                "UID": "123_A1_1", "CID": "cid", "SEID": "seid", "KID": "kid"
            }},
        })
        assert cookie == "UID=123_A1_1; CID=cid; SEID=seid; KID=kid"
        assert user_id == "123_A1_1"

    def test_incomplete_cookie_is_rejected(self):
        assert cas.extract_login_cookie({
            "state": True, "data": {"cookie": {"UID": None, "CID": None}}
        }) == ("", "")

    def test_legacy_flat_cookie_remains_compatible(self):
        cookie, _ = cas.extract_login_cookie({
            "state": True,
            "data": {"UID": "u", "CID": "c", "SEID": "s"},
        })
        assert cookie == "UID=u; CID=c; SEID=s"


# ===========================================================================
# 8. 115 目录选择
# ===========================================================================
class TestFolderSelector:
    def test_parse_folder_items_skips_files(self):
        payload = {"state": True, "data": [
            {"cid": "11", "n": "动漫"},
            {"fid": "99", "cid": "11", "n": "episode.mkv"},
            {"fid": "", "cid": "11", "n": "empty-fid-file.mp4"},
            {"cid": "12", "n": "电影"},
        ]}
        assert cas.parse_115_folder_items(payload) == [
            {"id": "11", "name": "动漫"},
            {"id": "12", "name": "电影"},
        ]

    def test_parse_nested_folder_items(self):
        payload = {"state": True, "data": {"list": [{"cid": 7, "n": "云下载"}]}}
        assert cas.parse_115_folder_items(payload) == [{"id": "7", "name": "云下载"}]

    def test_build_folder_options_uses_full_paths(self):
        tree = {
            "0": [{"id": "1", "name": "下载"}, {"id": "2", "name": "影视"}],
            "1": [{"id": "3", "name": "动漫"}],
            "2": [],
            "3": [],
        }
        options = cas.build_115_folder_options(lambda parent: tree[parent])
        assert options == [
            {"title": "根目录 /", "value": "0"},
            {"title": "/下载", "value": "1"},
            {"title": "/影视", "value": "2"},
            {"title": "/下载/动漫", "value": "3"},
        ]

    def test_build_folder_options_stops_cycles(self):
        tree = {"0": [{"id": "1", "name": "A"}], "1": [{"id": "1", "name": "A"}]}
        assert cas.build_115_folder_options(lambda parent: tree[parent]) == [
            {"title": "根目录 /", "value": "0"},
            {"title": "/A", "value": "1"},
        ]


class TestNonBlockingFolderForm:
    def test_get_form_reads_cache_without_network(self):
        plugin = cas.CloudAutoSearch()
        plugin._cookie = "UID=u; CID=c; SEID=s"
        plugin._user_id = "u"
        plugin._folder_options_cache = [
            {"title": "根目录 /", "value": "0"},
            {"title": "/动漫", "value": "11"},
        ]
        plugin._folder_cache_updated_at = "2026-09-11 01:00:00"
        plugin._fetch_folder_children = lambda _parent: (_ for _ in ()).throw(
            AssertionError("get_form must not access network")
        )
        form, defaults = plugin.get_form()
        assert defaults["target_folder_id"] == plugin._target_folder_id
        assert "VCombobox" in str(form)
        assert "/动漫" in str(form)

    def test_empty_cache_returns_immediately_with_root(self):
        plugin = cas.CloudAutoSearch()
        plugin._folder_options_cache = []
        plugin._target_folder_id = ""
        assert plugin._get_folder_options() == [
            {"title": "根目录 /", "value": "0"}
        ]

class TestFolderPathResolution:
    def test_numeric_id_never_accesses_network(self):
        plugin = cas.CloudAutoSearch()
        plugin._http_get = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("numeric id must not access network")
        )
        assert plugin._resolve_target_folder_id("12345") == "12345"
        assert plugin._resolve_target_folder_id("/") == "0"

    def test_unique_name_resolves_from_cache(self):
        plugin = cas.CloudAutoSearch()
        plugin._folder_options_cache = [
            {"title": "根目录 /", "value": "0"},
            {"title": "/下载/动漫", "value": "11"},
        ]
        plugin._http_get = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("cache hit must not access network")
        )
        assert plugin._resolve_target_folder_id("动漫") == "11"
        assert plugin._resolve_target_folder_id("/下载/动漫") == "11"

    def test_duplicate_name_requires_full_path(self):
        plugin = cas.CloudAutoSearch()
        plugin._folder_options_cache = [
            {"title": "/下载/动漫", "value": "11"},
            {"title": "/归档/动漫", "value": "22"},
        ]
        with pytest.raises(RuntimeError, match="同名目录"):
            plugin._resolve_target_folder_id("动漫")
        assert plugin._resolve_target_folder_id("/归档/动漫") == "22"

    def test_full_path_uses_getid_api(self):
        class Response:
            status_code = 200
            @staticmethod
            def json():
                return {"state": True, "id": 7788}

        plugin = cas.CloudAutoSearch()
        calls = []
        plugin._http_get = lambda url, params=None, timeout=0: (
            calls.append((url, params, timeout)) or Response()
        )
        assert plugin._resolve_target_folder_id("/下载/动漫") == "7788"
        assert calls == [(
            "https://webapi.115.com/files/getid",
            {"path": "/下载/动漫"},
            20,
        )]

    def test_root_only_refresh_does_not_recurse(self):
        plugin = cas.CloudAutoSearch()
        plugin._folder_options_cache = []
        plugin._folder_cache_updated_at = ""
        saved = {}
        calls = []
        plugin._fetch_folder_children = lambda parent: (
            calls.append(parent) or [{"id": "11", "name": "动漫"}]
        )
        plugin.save_data = lambda key, value: saved.update({key: value})
        plugin._refresh_folder_cache()
        assert calls == ["0"]
        assert plugin._folder_options_cache == [
            {"title": "根目录 /", "value": "0"},
            {"title": "/动漫", "value": "11"},
        ]
        assert saved["folder_options"]["items"] == plugin._folder_options_cache

class TestCheckLogin:
    class _Resp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    def test_state_zero_with_user_id_is_logged_in(self):
        """实测 check/sso 会在有效会话下返回 state=0，但带 user_id。"""
        plugin = cas.CloudAutoSearch()
        plugin._cookie = "UID=u; CID=c; SEID=s"
        plugin._user_id = ""
        plugin._http_get = lambda *a, **k: self._Resp(
            {"state": 0, "data": {"user_id": 4242}}
        )
        assert plugin._check_login() is True
        assert plugin._user_id == "4242"

    def test_falls_back_to_file_api_when_sso_has_no_user_id(self):
        plugin = cas.CloudAutoSearch()
        plugin._cookie = "UID=u; CID=c; SEID=s"
        plugin._user_id = "999"
        seen = []

        def fake_get(url, params=None, timeout=0):
            seen.append(url)
            if "check/sso" in url:
                return self._Resp({"state": 0, "data": {}})
            return self._Resp({"state": True, "id": 0})

        plugin._http_get = fake_get
        assert plugin._check_login() is True
        assert any("files/getid" in u for u in seen)

    def test_returns_false_when_session_really_invalid(self):
        plugin = cas.CloudAutoSearch()
        plugin._cookie = "UID=u; CID=c; SEID=s"
        plugin._user_id = "999"
        plugin._http_get = lambda *a, **k: self._Resp(
            {"state": False, "data": {}}
        )
        assert plugin._check_login() is False

    def test_no_cookie_short_circuits(self):
        plugin = cas.CloudAutoSearch()
        plugin._cookie = ""
        plugin._http_get = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not access network without cookie")
        )
        assert plugin._check_login() is False

class TestComboboxObjectValue:
    def test_selected_option_object_resolves_to_its_id(self):
        """VCombobox 选中候选项会回传 {title, value}，必须取到真实目录 ID。"""
        plugin = cas.CloudAutoSearch()
        plugin._http_get = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("object with id must not access network")
        )
        selected = {"title": "/云下载", "value": "3401155856685858217"}
        assert plugin._resolve_target_folder_id(selected) == "3401155856685858217"

    def test_object_without_value_falls_back_to_title_path(self):
        plugin = cas.CloudAutoSearch()
        plugin._folder_options_cache = [{"title": "/云下载", "value": "42"}]
        plugin._http_get = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("cache hit must not access network")
        )
        assert plugin._resolve_target_folder_id({"title": "/云下载"}) == "42"

    def test_init_plugin_normalizes_object_config(self):
        plugin = cas.CloudAutoSearch()
        assert plugin._normalize_folder_input(
            {"title": "/云下载", "value": "777"}
        ) == "777"
        assert plugin._normalize_folder_input("/下载/动漫") == "/下载/动漫"
        assert plugin._normalize_folder_input(None) == ""

    def test_object_value_is_not_silently_downgraded_to_root(self):
        """回归：旧版本会把对象退化成根目录 0，导致下载到错误位置。"""
        plugin = cas.CloudAutoSearch()
        selected = {"title": "/云下载", "value": "3401155856685858217"}
        assert plugin._resolve_target_folder_id(selected) != "0"
