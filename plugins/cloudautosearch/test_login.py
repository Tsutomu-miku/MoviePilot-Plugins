"""离线登录与网页登录分离，以及扫码凭据生命周期的回归测试。"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloudautosearch as cas


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


@pytest.fixture
def plugin():
    instance = cas.CloudAutoSearch()
    storage = {"credential": {"cookie": "original", "user_id": "123"}}
    instance.get_data = lambda key, default=None: copy.deepcopy(storage.get(key, default))
    instance.save_data = lambda key, value: storage.update({key: copy.deepcopy(value)})
    instance.del_data = lambda key: storage.pop(key, None)
    instance.update_config = lambda config: None
    instance._cookie = "original"
    instance._user_id = "123"
    instance._manual_job = {}
    instance._qrcode_token = {"uid": "qr-1", "time": 1234, "sign": "sign"}
    instance._start_folder_refresh = lambda: True
    instance._storage = storage
    return instance


def test_offline_success_is_not_blocked_by_a_logged_out_browser(plugin):
    def get(url, **kwargs):
        assert url == "https://115.com/web/lixian/"
        return Response({"state": True, "errcode": 0, "tasks": []})

    plugin._http_get = get
    assert plugin._check_login()
    assert plugin.api_status().data["login_status"]["state"] == "valid"
    assert plugin._storage["credential"]["cookie"] == "original"


def test_explicit_offline_expiry_preserves_credentials_and_offers_relogin(plugin):
    plugin._http_get = lambda *args, **kwargs: Response(
        {"state": False, "errcode": 99, "error_msg": "请重新登录"}
    )
    response = plugin.api_check_login()
    assert response.success
    assert response.data["login_status"]["state"] == "invalid"
    assert not response.data["logged_in"]
    assert response.data["credential_saved"]
    assert plugin._storage["credential"]["cookie"] == "original"
    assert "重新扫码登录" in str(plugin.get_page())


@pytest.mark.parametrize("kind", ["timeout", "captcha"])
def test_network_or_business_errors_are_not_reported_as_logged_out(plugin, kind):
    def get(*args, **kwargs):
        if kind == "timeout":
            raise TimeoutError("private transport details")
        return Response({"state": False, "errcode": 911, "error_msg": "请验证账号"})

    plugin._http_get = get
    assert not plugin._check_login()
    login = plugin.api_status().data["login_status"]
    assert login["state"] == "error"
    assert "未登录" not in login["message"]
    assert "private transport details" not in login["message"]


def test_reading_pages_does_not_access_115_and_exposes_login_in_both_views(plugin):
    plugin._http_get = lambda *args, **kwargs: pytest.fail("page must not access 115")
    for tree in (plugin.get_form()[0], plugin.get_page()):
        assert "data-login-scan" in str(tree)
        assert "data-login-check" in str(tree)
    assert plugin.api_status().data["login_status"]["state"] == "unchecked"


def test_refreshing_qr_does_not_clear_a_working_login(plugin):
    plugin._fetch_qrcode_token = lambda: {"uid": "qr-2", "time": 1234, "sign": "s"}
    plugin._qrcode_to_data_url = lambda content: "data:image/png;base64,example"
    assert plugin.api_qrcode().success
    assert plugin._storage["credential"]["cookie"] == "original"
    assert not plugin.api_qrcode_status(uid="qr-1").success


def test_successful_scan_is_saved_once_with_numeric_account_id(plugin):
    plugin._http_get = lambda *args, **kwargs: Response({"data": {"status": 2}})
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return Response({"state": True, "data": {"uname": "User", "cookie": {
            "UID": "456_A1_1", "CID": "cid", "SEID": "seid",
        }}})

    plugin._http_post = post
    assert plugin.api_qrcode_status("qr-1").data["cookie_saved"]
    assert plugin.api_qrcode_status("qr-1").data["cookie_saved"]
    assert len(calls) == 1
    assert plugin._user_id == "456"
    assert plugin._storage["credential"]["user_id"] == "456"
    assert plugin._storage["login_status"]["state"] == "unchecked"
    assert "SEID" not in str(plugin.api_status().data)


@pytest.mark.parametrize("payload", [
    {"state": False},
    {"state": True, "data": {"cookie": {"UID": "456", "CID": "cid"}}},
])
def test_failed_exchange_keeps_the_previous_credentials(plugin, payload):
    plugin._http_get = lambda *args, **kwargs: Response({"data": {"status": 2}})
    plugin._http_post = lambda *args, **kwargs: Response(payload)
    result = plugin.api_qrcode_status("qr-1")
    assert not result.data.get("cookie_saved")
    assert plugin._storage["credential"]["cookie"] == "original"


def test_a_refreshed_qr_cannot_commit_an_older_inflight_login(plugin):
    plugin._http_get = lambda *args, **kwargs: Response({"data": {"status": 2}})

    def post(*args, **kwargs):
        plugin._qrcode_token = {"uid": "qr-2", "time": 4567, "sign": "new"}
        return Response({"state": True, "data": {"cookie": {
            "UID": "456", "CID": "cid", "SEID": "seid",
        }}})

    plugin._http_post = post
    assert not plugin.api_qrcode_status("qr-1").success
    assert plugin._storage["credential"]["cookie"] == "original"
