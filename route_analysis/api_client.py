"""Read-only scheduler HTTP client with bounded automatic reauthentication."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import requests

from route_analysis.errors import ApiError, AuthenticationError, DataContractError
from route_analysis.logging_setup import log_event
from route_analysis.models import PosePoint
from route_analysis.parsing import parse_command_path

LOGGER = logging.getLogger(__name__)


class ResponseLike(Protocol):
    status_code: int

    def json(self) -> object: ...

    def raise_for_status(self) -> None: ...


class SessionLike(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> ResponseLike: ...


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    api_root: str
    tenant: str = "suntae"
    username: str = ""
    password: str = ""
    timeout_seconds: float = 20.0
    verify_tls: bool = True

    def validated_root(self) -> str:
        value = self.api_root.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API 根地址必须是完整的 http:// 或 https:// 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 根地址不能包含凭据、查询参数或片段")
        if self.timeout_seconds <= 0:
            raise ValueError("请求超时必须大于零")
        netloc = parsed.hostname.lower()
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))

    def validate_credentials(self) -> None:
        if not self.tenant.strip() or not self.username.strip() or not self.password:
            raise ValueError("租户、用户名和密码不能为空")


@dataclass(frozen=True, slots=True)
class OrderRecord:
    id: int
    map_id: int | None
    status: str
    created_at: str
    raw: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    order_id: int | None
    vin: str
    map_id: int | None
    status: str
    raw: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    id: int
    task_id: int | None
    vin: str
    map_id: int | None
    status: str
    capability: str
    raw: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    total: int
    page_no: int
    page_size: int


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DataContractError("后端 ID 字段不是整数")
    if not isinstance(value, (str, int, float)):
        raise DataContractError("后端 ID 字段不是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DataContractError("后端 ID 字段不是整数") from exc


def _required_int(value: object) -> int:
    result = _optional_int(value)
    if result is None:
        raise DataContractError("后端记录缺少 ID")
    return result


def _text(value: object) -> str:
    return "" if value is None else str(value)


class SchedulerClient:
    """A deliberately narrow client exposing only required read operations."""

    def __init__(self, settings: ConnectionSettings, session: SessionLike | None = None) -> None:
        self.settings = settings
        self.api_root = settings.validated_root()
        self._session = cast(SessionLike, session or requests.Session())
        self._tenant_id: str | None = None
        self._access_token: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ResponseLike:
        url = f"{self.api_root}{path}"
        request_headers = dict(headers or {})
        started = time.perf_counter()
        log_event(
            LOGGER,
            logging.DEBUG,
            "http_request",
            method=method,
            url=url,
            params=params,
            json_body=json_body,
            headers=request_headers,
            connection={
                "api_root": self.settings.api_root,
                "tenant": self.settings.tenant,
                "username": self.settings.username,
                "password": self.settings.password,
                "timeout_seconds": self.settings.timeout_seconds,
                "verify_tls": self.settings.verify_tls,
            },
        )
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=request_headers,
                timeout=self.settings.timeout_seconds,
                verify=self.settings.verify_tls,
            )
        except requests.RequestException as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "http_request_failed",
                exc_info=True,
                method=method,
                url=url,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise ApiError("无法连接调度后端，请检查地址、网络和证书设置") from exc
        log_event(
            LOGGER,
            logging.INFO,
            "http_request_completed",
            method=method,
            path=path,
            status_code=response.status_code,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return response

    @staticmethod
    def _payload(response: ResponseLike) -> Mapping[str, object]:
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise ApiError("调度后端返回了非 JSON 响应") from exc
        if not isinstance(payload, Mapping):
            raise ApiError("调度后端响应格式无效")
        log_event(
            LOGGER,
            logging.DEBUG,
            "http_response",
            status_code=response.status_code,
            response=payload,
        )
        return payload

    @staticmethod
    def _is_unauthorized(response: ResponseLike, payload: Mapping[str, object]) -> bool:
        raw_code = payload.get("code", 0)
        code = int(raw_code) if isinstance(raw_code, (str, int, float)) else 0
        return response.status_code == 401 or code == 401

    @staticmethod
    def _unwrap(response: ResponseLike, *, authentication: bool = False) -> object:
        payload = SchedulerClient._payload(response)
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            error_type = AuthenticationError if authentication else ApiError
            message = "登录失败，请检查凭据和租户" if authentication else "调度后端请求失败"
            raise error_type(message) from exc
        raw_code = payload.get("code", 0)
        if not isinstance(raw_code, (str, int, float)):
            raise ApiError("调度后端响应 code 无效")
        try:
            code = int(raw_code)
        except ValueError as exc:
            raise ApiError("调度后端响应 code 无效") from exc
        if code != 0:
            if authentication or code == 401:
                raise AuthenticationError("登录状态无效，请检查凭据和租户")
            message = _text(payload.get("msg")) or "调度后端拒绝了查询"
            raise ApiError(message)
        return payload.get("data")

    def login(self) -> None:
        self.settings.validate_credentials()
        if self._tenant_id is None:
            tenant_response = self._request(
                "GET",
                "/system/tenant/get-id-by-name",
                params={"name": self.settings.tenant.strip()},
            )
            tenant_data = self._unwrap(tenant_response, authentication=True)
            if tenant_data is None or isinstance(tenant_data, bool):
                raise AuthenticationError("未找到配置的租户")
            self._tenant_id = str(tenant_data)

        response = self._request(
            "POST",
            "/system/auth/login",
            json_body={
                "username": self.settings.username.strip(),
                "password": self.settings.password,
            },
            headers={"tenant-id": self._tenant_id},
        )
        data = self._unwrap(response, authentication=True)
        if not isinstance(data, Mapping) or not isinstance(data.get("accessToken"), str):
            raise AuthenticationError("登录响应未包含访问令牌")
        self._access_token = data["accessToken"]

    def _get(self, path: str, params: Mapping[str, object]) -> object:
        if self._access_token is None:
            self.login()
        for attempt in range(2):
            assert self._access_token is not None and self._tenant_id is not None
            response = self._request(
                "GET",
                path,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "tenant-id": self._tenant_id,
                },
            )
            payload = self._payload(response)
            if not self._is_unauthorized(response, payload):
                return self._unwrap(response)
            if attempt == 1:
                raise AuthenticationError("重新登录后仍未获得访问权限")
            self._access_token = None
            self.login()
        raise AssertionError("unreachable")

    @staticmethod
    def _page_data(data: object) -> tuple[list[Mapping[str, object]], int]:
        if not isinstance(data, Mapping) or not isinstance(data.get("list"), list):
            raise DataContractError("后端分页响应缺少 list")
        records: list[Mapping[str, object]] = []
        for raw in data["list"]:
            if not isinstance(raw, Mapping):
                raise DataContractError("后端分页记录必须是对象")
            records.append(raw)
        total = _required_int(data.get("total"))
        return records, total

    def list_orders(
        self,
        *,
        page_no: int,
        page_size: int,
        order_id: int | None = None,
    ) -> Page[OrderRecord]:
        params: dict[str, object] = {"pageNo": page_no, "pageSize": page_size}
        if order_id is not None:
            params["orderIds"] = [order_id]
        records, total = self._page_data(self._get("/scheduling/order/page", params))
        items = tuple(
            OrderRecord(
                id=_required_int(raw.get("id")),
                map_id=_optional_int(raw.get("mapId")),
                status=_text(raw.get("statusDescription") or raw.get("status")),
                created_at=_text(raw.get("createTime")),
                raw=dict(raw),
            )
            for raw in records
        )
        return Page(items, total, page_no, page_size)

    def list_tasks(self, *, order_id: int, page_no: int, page_size: int) -> Page[TaskRecord]:
        params: dict[str, object] = {
            "orderId": order_id,
            "pageNo": page_no,
            "pageSize": page_size,
        }
        records, total = self._page_data(self._get("/scheduling/order/detail", params))
        items = tuple(
            TaskRecord(
                id=_required_int(raw.get("id")),
                order_id=_optional_int(raw.get("orderId")),
                vin=_text(raw.get("vin")),
                map_id=_optional_int(raw.get("mapId")),
                status=_text(raw.get("statusDescription") or raw.get("status")),
                raw=dict(raw),
            )
            for raw in records
        )
        return Page(items, total, page_no, page_size)

    def list_commands(self, *, task_id: int) -> tuple[CommandRecord, ...]:
        data = self._get("/scheduling/order-task/work-flow", {"id": task_id})
        if not isinstance(data, list):
            raise DataContractError("命令工作流响应必须是数组")
        commands: list[CommandRecord] = []
        for raw in data:
            if not isinstance(raw, Mapping):
                raise DataContractError("命令工作流记录必须是对象")
            commands.append(
                CommandRecord(
                    id=_required_int(raw.get("id")),
                    task_id=_optional_int(raw.get("orderTaskId")),
                    vin=_text(raw.get("vin")),
                    map_id=_optional_int(raw.get("mapId")),
                    status=_text(raw.get("statusDescription") or raw.get("status")),
                    capability=_text(
                        raw.get("capabilityTypeDescription") or raw.get("capabilityType")
                    ),
                    raw=dict(raw),
                )
            )
        return tuple(commands)

    @staticmethod
    def _path_from_response(data: object) -> tuple[PosePoint, ...]:
        if not isinstance(data, Mapping):
            raise DataContractError("路径响应必须是对象")
        command = data.get("commandStr")
        if command is None or command == "":
            return ()
        return parse_command_path(command)

    def get_dispatched_path(self, *, command_id: int) -> tuple[PosePoint, ...]:
        data = self._get("/scheduling/order-task/commandStr", {"id": command_id})
        return self._path_from_response(data)

    def get_actual_path(self, *, command_id: int, vin: str) -> tuple[PosePoint, ...]:
        data = self._get(
            "/scheduling/order-task/actualPath",
            {"commandId": command_id, "vin": vin},
        )
        return self._path_from_response(data)
