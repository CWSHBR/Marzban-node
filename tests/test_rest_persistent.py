import importlib
import json
import os
import stat
import sys
from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace


def load_rest_service(tmp_path, monkeypatch):
    xray = tmp_path / "xray"
    xray.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"version\" ]; then\n"
        "  echo 'Xray 1.0.0 test'\n"
        "  exit 0\n"
        "fi\n"
        "sleep 60\n"
    )
    xray.chmod(xray.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv("XRAY_EXECUTABLE_PATH", str(xray))
    monkeypatch.setenv("XRAY_ASSETS_PATH", str(tmp_path))
    monkeypatch.setenv("XRAY_PERSISTENT_MODE", "false")
    monkeypatch.setenv("XRAY_RESTORE_LAST_CONFIG", "false")

    for module in ("rest_service", "config", "xray"):
        sys.modules.pop(module, None)

    return importlib.import_module("rest_service")


class FakeCore:
    def __init__(self, version="1.0.0"):
        self.version = version
        self.started_value = False
        self.start_count = 0
        self.stop_count = 0
        self.restart_count = 0

    @property
    def started(self):
        return self.started_value

    def get_version(self):
        return self.version

    @contextmanager
    def get_logs(self):
        yield deque([f"Xray {self.version} started"])

    def start(self, config):
        if self.started_value:
            raise RuntimeError("Xray is started already")
        self.start_count += 1
        self.started_value = True

    def stop(self):
        self.stop_count += 1
        self.started_value = False

    def restart(self, config):
        self.restart_count += 1
        self.stop()
        self.start(config)


def request(ip="10.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def config(extra=None):
    payload = {
        "inbounds": [
            {
                "tag": "VLESS TCP",
                "protocol": "vless",
                "port": 443,
                "settings": {"clients": []},
            }
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def service(rest_service, persistent=True):
    srv = rest_service.Service(persistent_mode=persistent)
    srv.core = FakeCore()
    srv.core_version = srv.core.version
    return srv


def test_connect_does_not_stop_running_xray_in_persistent_mode(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    srv.connected = True
    srv.session_id = rest_service.uuid4()
    srv.client_ip = "10.0.0.1"
    srv.core.started_value = True

    response = srv.connect(request("10.0.0.1"))

    assert response["started"] is True
    assert response["attached"] is True
    assert srv.core.stop_count == 0


def test_disconnect_does_not_stop_running_xray_in_persistent_mode(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    srv.connect(request())
    srv.core.started_value = True

    response = srv.disconnect(session_id=srv.session_id)

    assert response["connected"] is False
    assert response["started"] is True
    assert srv.core.stop_count == 0


def test_stop_stops_xray(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    srv.connect(request())
    srv.core.started_value = True
    srv.panel_config_hash = "hash"

    response = srv.stop(session_id=srv.session_id)

    assert response["started"] is False
    assert srv.core.stop_count == 1
    assert srv.panel_config_hash is None


def test_restart_restarts_xray(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    srv.connect(request())
    srv.core.started_value = True

    response = srv.restart(session_id=srv.session_id, config=config())

    assert response["started"] is True
    assert response["needs_restart"] is False
    assert srv.core.restart_count == 1
    assert srv.core.stop_count == 1
    assert srv.core.start_count == 1
    assert srv.panel_config_hash


def test_start_same_config_attaches_without_restart(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    srv.connect(request())
    body = config()
    first = srv.start(session_id=srv.session_id, config=body)
    second = srv.start(session_id=srv.session_id, config=body)

    assert first["attached"] is False
    assert second["attached"] is True
    assert second["needs_restart"] is False
    assert srv.core.start_count == 1
    assert srv.core.restart_count == 0


def test_start_different_config_reports_needs_restart_without_restart(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    srv.connect(request())
    srv.start(session_id=srv.session_id, config=config())

    response = srv.start(
        session_id=srv.session_id,
        config=config({"outbounds": [{"tag": "blocked", "protocol": "blackhole"}]}),
    )

    assert response["started"] is True
    assert response["attached"] is True
    assert response["needs_restart"] is True
    assert response["reason"] == "config_changed"
    assert srv.core.start_count == 1
    assert srv.core.restart_count == 0


def test_panel_ip_change_reports_needs_restart_without_restart(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=True)
    body = config()
    srv.connect(request("10.0.0.1"))
    srv.start(session_id=srv.session_id, config=body)
    srv.connect(request("10.0.0.2"))

    response = srv.start(session_id=srv.session_id, config=body)

    assert response["started"] is True
    assert response["needs_restart"] is True
    assert response["reason"] == "panel_ip_changed"
    assert srv.core.stop_count == 0
    assert srv.core.restart_count == 0


def test_non_persistent_mode_keeps_stop_on_reconnect_and_disconnect(tmp_path, monkeypatch):
    rest_service = load_rest_service(tmp_path, monkeypatch)
    srv = service(rest_service, persistent=False)
    srv.connected = True
    srv.session_id = rest_service.uuid4()
    srv.client_ip = "10.0.0.1"
    srv.core.started_value = True

    srv.connect(request("10.0.0.2"))
    assert srv.core.stop_count == 1

    srv.core.started_value = True
    srv.disconnect()
    assert srv.core.stop_count == 2
