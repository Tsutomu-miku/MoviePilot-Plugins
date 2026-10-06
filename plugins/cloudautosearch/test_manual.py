"""离线推送的 API、失败重试、互斥、凭据隔离及页面回归测试。"""
import base64
import copy
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloudautosearch as cas
from cloudautosearch.ui import manual_form


HASH_A = "1234567890abcdef1234567890abcdef12345678"
HASH_B = "abcdef1234567890abcdef1234567890abcdef12"
MAGNET_A = f"magnet:?xt=urn:btih:{HASH_A}&dn=Example+A"
MAGNET_B = f"magnet:?xt=urn:btih:{HASH_B}&dn=Example+B"


@pytest.fixture
def plugin(monkeypatch):
    instance = cas.CloudAutoSearch()
    storage = {}
    instance.get_data = lambda key, default=None: copy.deepcopy(storage.get(key, default))
    instance.save_data = lambda key, value: storage.update({key: copy.deepcopy(value)})
    instance._run_lock = threading.Lock()
    instance._manual_state_lock = threading.Lock()
    instance._manual_job = {}
    instance._folder_options_cache = [{"title": "/Downloads", "value": "42"}]
    instance._target_folder_id = "42"
    instance._cookie = "UID=test; CID=test; SEID=test"
    instance._user_id = "123"
    instance._check_login = lambda: True
    instance._submit_offline_task = lambda magnet, folder: True
    instance._storage = storage

    class ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, **unused):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(cas.threading, "Thread", ImmediateThread)
    return instance


def test_base32_encoded_btih_and_exact_hash_validation():
    value = base64.b32encode(bytes.fromhex(HASH_A)).decode().lower()
    assert cas.extract_info_hash_from_magnet(f"magnet:?xt=urn%3Abtih%3A{value}") == HASH_A
    assert cas.extract_info_hash_from_magnet(f"magnet:?xt=urn:btih:{HASH_A}ff") is None
    assert cas.extract_info_hash_from_magnet(f"https://example.com/?xt=urn:btih:{HASH_A}") is None


@pytest.mark.parametrize("raw", [None, "\n ", "a" * 65537, "\n".join([MAGNET_A] * 21)])
def test_invalid_batches_are_rejected_before_start(plugin, raw):
    response = plugin.api_manual_submit({"links": raw})
    assert not response.success
    assert not plugin._run_lock.locked()
    assert not plugin._storage


def test_mixed_batch_continues_after_invalid_link_and_deduplicates(plugin):
    plugin._enabled = False
    plugin._include_keywords = "never-match"
    calls = []
    plugin._submit_offline_task = lambda magnet, folder: calls.append((magnet, folder)) or True
    response = plugin.api_manual_submit({"links": f"{MAGNET_A}\nftp://example.com/a\n{MAGNET_A}\n{MAGNET_B}"})
    assert response.success
    job = plugin.api_manual_status().data
    assert (job["submitted"], job["failed"], job["duplicated"]) == (2, 1, 1)
    assert job["status"] == "partial"
    assert len(calls) == 2 and all(folder == "42" for _, folder in calls)
    assert set(plugin._storage["dedup_history"]) == {HASH_A, HASH_B}
    assert plugin._storage["run_log"][0]["source"] == "manual_links"
    assert not plugin._run_lock.locked()


def test_failed_submission_can_be_retried(plugin):
    plugin._submit_offline_task = lambda *args: False
    plugin._last_submit_error = "115 quota exceeded"
    plugin.api_manual_submit({"links": MAGNET_A})
    assert HASH_A not in plugin._storage.get("dedup_history", {})
    assert "quota" in plugin.api_manual_status().data["results"][0]["message"]
    plugin._submit_offline_task = lambda *args: True
    plugin.api_manual_submit({"links": MAGNET_A})
    assert plugin.api_manual_status().data["submitted"] == 1
    assert HASH_A in plugin._storage["dedup_history"]


def test_explicit_resubmit_still_deduplicates_within_batch(plugin):
    plugin._storage["dedup_history"] = {HASH_A: {"title": "existing"}}
    plugin.api_manual_submit({"links": MAGNET_A})
    assert plugin.api_manual_status().data["duplicated"] == 1
    calls = []
    plugin._submit_offline_task = lambda *args: calls.append(args) or True
    plugin.api_manual_submit({"links": f"{MAGNET_A}\n{MAGNET_A}", "force": True})
    assert len(calls) == 1
    assert plugin.api_manual_status().data["duplicated"] == 1


def test_busy_rejects_manual_and_rss_without_overwriting_job(plugin):
    plugin._manual_job = {"id": "existing", "status": "running"}
    plugin._run_lock.acquire()
    try:
        assert not plugin.api_manual_submit({"links": MAGNET_A}).success
        assert not plugin.api_run().success
        assert plugin.api_manual_status().data["id"] == "existing"
    finally:
        plugin._run_lock.release()


@pytest.mark.parametrize("failure", ["login", "folder"])
def test_login_or_directory_failure_never_submits_and_releases_lock(plugin, failure):
    def must_not_submit(*args):
        pytest.fail("must not submit when login or directory resolution fails")
    plugin._submit_offline_task = must_not_submit
    if failure == "login":
        plugin._check_login = lambda: False
    else:
        plugin._resolve_target_folder_id = lambda *args: (_ for _ in ()).throw(RuntimeError("目录不存在"))
    plugin.api_manual_submit({"links": MAGNET_A})
    job = plugin.api_manual_status().data
    assert job["status"] == "failed" and job["failed"] == 1
    assert not plugin._run_lock.locked()


def test_target_directory_is_snapshotted_before_worker_starts(plugin, monkeypatch):
    pending = []
    class DeferredThread:
        def __init__(self, target, args=(), **kwargs):
            pending.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(cas.threading, "Thread", DeferredThread)
    calls = []
    plugin._submit_offline_task = lambda magnet, folder: calls.append(folder) or True
    plugin.api_manual_submit({"links": MAGNET_A})
    assert plugin._run_lock.locked()
    plugin._target_folder_id = "999"
    target, args = pending[0]
    target(*args)
    assert calls == ["42"]


def test_download_link_is_converted_to_magnet(plugin):
    torrent = cas.bencode_encode({b"info": {b"name": b"Downloaded", b"length": 1,
                                         b"piece length": 16384, b"pieces": b"0" * 20}})
    class Response:
        content = torrent
        def raise_for_status(self):
            pass
    plugin._http_get = lambda *args, **kwargs: Response()
    calls = []
    plugin._submit_offline_task = lambda magnet, folder: calls.append(magnet) or True
    url = "https://tracker.example.com/download.php?id=123&passkey=private"
    plugin.api_manual_submit({"links": url})
    assert calls[0].startswith("magnet:?xt=urn:btih:")
    assert plugin.api_manual_status().data["results"][0]["title"] == "Downloaded"
    assert "passkey" not in str(plugin._storage)


def test_cookie_is_only_sent_to_115_domains(plugin):
    assert "Cookie" in plugin._headers_for_url("https://webapi.115.com/files")
    for url in ["https://tracker.example.com/rss", "https://115.com.attacker.example/torrent"]:
        assert "Cookie" not in plugin._headers_for_url(url)


def test_page_is_available_without_history_and_does_not_access_network(plugin):
    plugin._http_get = lambda *args, **kwargs: pytest.fail("page must not access network")
    page = str(plugin.get_page())
    assert "手动推送" in page and "暂无运行记录" in page and "/Downloads" in page
    assert "Cookie" not in page and "UID=test" not in page


def test_untrusted_titles_and_errors_are_escaped():
    html = manual_form({"results": [{"status": "failed", "title": '<img src=x onerror="alert(1)">',
                                      "message": "<script>bad()</script>"}]})
    assert "<img src=x" not in html and "<script>bad()" not in html
    assert "&lt;img" in html and "&lt;script&gt;bad()" in html


def test_progress_snapshot_cannot_mutate_worker_state(plugin):
    plugin.api_manual_submit({"links": MAGNET_A})
    snapshot = plugin.api_manual_status().data
    snapshot["results"][0]["title"] = "changed"
    assert plugin.api_manual_status().data["results"][0]["title"] == "Example A"


def test_thread_start_failure_releases_lock(plugin, monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("cannot start")
    monkeypatch.setattr(cas.threading, "Thread", BrokenThread)
    assert not plugin.api_manual_submit({"links": MAGNET_A}).success
    assert not plugin._run_lock.locked()
    assert plugin.api_manual_status().data["status"] == "failed"


def test_interrupted_job_is_marked_for_review_after_reload(plugin):
    plugin._storage["manual_job"] = {"status": "running", "results": [
        {"status": "success"}, {"status": "pending"}
    ]}
    plugin._start_folder_refresh = lambda: False
    plugin.init_plugin({"enabled": False})
    job = plugin.api_manual_status().data
    assert job["status"] == "failed"
    assert job["results"][0]["status"] == "success"
    assert job["results"][1]["status"] == "failed"


def test_encrypted_rejection_is_not_overridden_by_outer_state(plugin, monkeypatch):
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"state": True, "encoded_data": "encoded"}
    plugin._http_post = lambda *args, **kwargs: Response()
    monkeypatch.setattr(cas, "m115_decode", lambda *args: b'{"state":false,"error_msg":"quota"}')
    assert not cas.CloudAutoSearch._submit_offline_task(plugin, MAGNET_A, "42")
    assert plugin._last_submit_error == "quota"
