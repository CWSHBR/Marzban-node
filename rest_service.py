import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import (APIRouter, Body, FastAPI, HTTPException, Request,
                     WebSocket, status)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from config import (
    AUTO_RESTART_STALE_NODE,
    XRAY_ASSETS_PATH,
    XRAY_EXECUTABLE_PATH,
    XRAY_LAST_CONFIG_PATH,
    XRAY_PERSISTENT_MODE,
    XRAY_RESTORE_LAST_CONFIG,
)
from logger import logger
from xray import XRayConfig, XRayCore

app = FastAPI()


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = {}
    for error in exc.errors():
        details[error["loc"][-1]] = error.get("msg")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": details}),
    )


class Service(object):
    def __init__(
        self,
        persistent_mode: bool = XRAY_PERSISTENT_MODE,
        restore_last_config: bool = XRAY_RESTORE_LAST_CONFIG,
        last_config_path: str = XRAY_LAST_CONFIG_PATH,
        auto_restart_stale_node: bool = AUTO_RESTART_STALE_NODE,
    ):
        self.router = APIRouter()

        self.persistent_mode = persistent_mode
        self.restore_last_config = restore_last_config
        self.last_config_path = last_config_path
        self.auto_restart_stale_node = auto_restart_stale_node

        self.connected = False
        self.client_ip = None
        self.session_id = None
        self.core = XRayCore(
            executable_path=XRAY_EXECUTABLE_PATH,
            assets_path=XRAY_ASSETS_PATH
        )
        self.core_version = self.core.get_version()
        self.config = None
        self.panel_config_hash = None
        self.running_panel_ip = None
        self.running_config_started_at = None

        self.router.add_api_route("/", self.base, methods=["POST"])
        self.router.add_api_route("/ping", self.ping, methods=["POST"])
        self.router.add_api_route("/connect", self.connect, methods=["POST"])
        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])
        self.router.add_api_route("/start", self.start, methods=["POST"])
        self.router.add_api_route("/stop", self.stop, methods=["POST"])
        self.router.add_api_route("/restart", self.restart, methods=["POST"])

        self.router.add_websocket_route("/logs", self.logs)

        if self.persistent_mode and self.restore_last_config:
            self.restore_runtime_config()

    def match_session_id(self, session_id: UUID):
        if session_id != self.session_id:
            raise HTTPException(
                status_code=403,
                detail="Session ID mismatch."
            )
        return True

    def response(self, **kwargs):
        return {
            "connected": self.connected,
            "started": self.core.started,
            "core_version": self.core_version,
            "persistent_mode": self.persistent_mode,
            "running_config_hash": self.panel_config_hash,
            "running_panel_ip": self.running_panel_ip,
            "running_config_started_at": self.running_config_started_at,
            **kwargs
        }

    @staticmethod
    def get_config_hash(config: str):
        data = json.loads(config)
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _build_config(self, config: str, client_ip: str):
        try:
            panel_config_hash = self.get_config_hash(config)
            xray_config = XRayConfig(config, client_ip)
        except json.decoder.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "config": f'Failed to decode config: {exc}'
                }
            )
        return xray_config, panel_config_hash

    def _mark_running(self, panel_config_hash: str, panel_ip: str):
        self.panel_config_hash = panel_config_hash
        self.running_panel_ip = panel_ip
        self.running_config_started_at = datetime.now(timezone.utc).isoformat()

    def _clear_running(self):
        self.panel_config_hash = None
        self.running_panel_ip = None
        self.running_config_started_at = None

    def _stale_reason(self, panel_config_hash: str, panel_ip: str):
        if self.panel_config_hash and self.panel_config_hash != panel_config_hash:
            return "config_changed"
        if self.running_panel_ip and self.running_panel_ip != panel_ip:
            return "panel_ip_changed"
        return None

    def save_runtime_config(self, config: XRayConfig):
        if not (self.persistent_mode and self.restore_last_config):
            return

        directory = os.path.dirname(self.last_config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(self.last_config_path, "w") as file:
            file.write(config.to_json())

        os.chmod(self.last_config_path, 0o600)

    def restore_runtime_config(self):
        if not os.path.isfile(self.last_config_path):
            return

        with open(self.last_config_path) as file:
            config = file.read()

        xray_config, panel_config_hash = self._build_config(config, "127.0.0.1")
        try:
            self.core.start(xray_config)
            self._mark_running(panel_config_hash, "127.0.0.1")
            logger.info("Restored Xray core from last persisted config.")
        except Exception as exc:
            logger.error(f"Failed to restore Xray core from last persisted config: {exc}")

    def wait_for_started_log(self, logs):
        start_time = time.time()
        end_time = start_time + 3
        last_log = ''
        while time.time() < end_time:
            while logs:
                log = logs.popleft()
                if log:
                    last_log = log
                if f'Xray {self.core_version} started' in log:
                    return last_log
            time.sleep(0.1)
        return last_log

    def base(self):
        return self.response()

    def connect(self, request: Request):
        self.session_id = uuid4()
        self.client_ip = request.client.host

        if self.connected:
            logger.warning(
                f'New connection from {self.client_ip}, Core control access was taken away from previous client.')
            if self.core.started and not self.persistent_mode:
                try:
                    self.core.stop()
                except RuntimeError:
                    pass
                self._clear_running()

        self.connected = True
        logger.info(f'{self.client_ip} connected, Session ID = "{self.session_id}".')

        reason = "panel_ip_changed" if (
            self.persistent_mode
            and self.core.started
            and self.running_panel_ip
            and self.running_panel_ip != self.client_ip
        ) else None

        return self.response(
            session_id=self.session_id,
            attached=self.persistent_mode and self.core.started,
            needs_restart=bool(reason),
            reason=reason,
        )

    def disconnect(self, session_id: UUID = Body(None, embed=True)):
        if self.persistent_mode:
            self.match_session_id(session_id)

        if self.connected:
            logger.info(f'{self.client_ip} disconnected, Session ID = "{self.session_id}".')

        self.session_id = None
        self.client_ip = None
        self.connected = False

        if self.core.started and not self.persistent_mode:
            try:
                self.core.stop()
            except RuntimeError:
                pass
            self._clear_running()

        return self.response()

    def ping(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return {}

    def start(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        self.match_session_id(session_id)

        panel_config = config
        config, panel_config_hash = self._build_config(panel_config, self.client_ip)

        if self.persistent_mode and self.core.started:
            reason = self._stale_reason(panel_config_hash, self.client_ip)
            if reason:
                if self.auto_restart_stale_node:
                    return self.restart(session_id=session_id, config=panel_config)
                return self.response(
                    attached=True,
                    needs_restart=True,
                    reason=reason,
                )

            return self.response(
                attached=True,
                needs_restart=False,
                reason=None,
            )

        with self.core.get_logs() as logs:
            try:
                self.core.start(config)
                last_log = self.wait_for_started_log(logs)

            except Exception as exc:
                logger.error(f"Failed to start core: {exc}")
                raise HTTPException(
                    status_code=503,
                    detail=str(exc)
                )

        if not self.core.started:
            raise HTTPException(
                status_code=503,
                detail=last_log
            )

        self._mark_running(panel_config_hash, self.client_ip)
        self.save_runtime_config(config)

        return self.response(
            attached=False,
            needs_restart=False,
            reason=None,
        )

    def stop(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)

        try:
            self.core.stop()

        except RuntimeError:
            pass

        self._clear_running()

        return self.response()

    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        self.match_session_id(session_id)

        config, panel_config_hash = self._build_config(config, self.client_ip)

        try:
            with self.core.get_logs() as logs:
                self.core.restart(config)
                last_log = self.wait_for_started_log(logs)

        except Exception as exc:
            logger.error(f"Failed to restart core: {exc}")
            raise HTTPException(
                status_code=503,
                detail=str(exc)
            )

        if not self.core.started:
            raise HTTPException(
                status_code=503,
                detail=last_log
            )

        self._mark_running(panel_config_hash, self.client_ip)
        self.save_runtime_config(config)

        return self.response(
            attached=False,
            needs_restart=False,
            reason=None,
        )

    async def logs(self, websocket: WebSocket):
        session_id = websocket.query_params.get('session_id')
        interval = websocket.query_params.get('interval')

        try:
            session_id = UUID(session_id)
            if session_id != self.session_id:
                return await websocket.close(reason="Session ID mismatch.", code=4403)

        except ValueError:
            return await websocket.close(reason="session_id should be a valid UUID.", code=4400)

        if interval:
            try:
                interval = float(interval)

            except ValueError:
                return await websocket.close(reason="Invalid interval value.", code=4400)

            if interval > 10:
                return await websocket.close(reason="Interval must be more than 0 and at most 10 seconds.", code=4400)

        await websocket.accept()

        cache = ''
        last_sent_ts = 0
        with self.core.get_logs() as logs:
            while session_id == self.session_id:
                if interval and time.time() - last_sent_ts >= interval and cache:
                    try:
                        await websocket.send_text(cache)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    cache = ''
                    last_sent_ts = time.time()

                if not logs:
                    try:
                        await asyncio.wait_for(websocket.receive(), timeout=0.2)
                        continue
                    except asyncio.TimeoutError:
                        continue
                    except (WebSocketDisconnect, RuntimeError):
                        break

                log = logs.popleft()

                if interval:
                    cache += f'{log}\n'
                    continue

                try:
                    await websocket.send_text(log)
                except (WebSocketDisconnect, RuntimeError):
                    break

        await websocket.close()


service = Service()
app.include_router(service.router)
