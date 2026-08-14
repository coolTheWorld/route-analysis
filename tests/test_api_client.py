from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from route_analysis.api_client import ConnectionSettings, SchedulerClient
from route_analysis.errors import AuthenticationError
from route_analysis.logging_setup import configure_logging


@dataclass
class FakeResponse:
    payload: object
    status_code: int = 200

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def ok(data: object) -> FakeResponse:
    return FakeResponse({"code": 0, "data": data, "msg": ""})


def settings() -> ConnectionSettings:
    return ConnectionSettings(
        api_root="http://example.test/admin-api/",
        tenant="suntae",
        username="operator",
        password="secret",
    )


def test_order_query_logs_in_and_uses_exact_id_with_server_pagination() -> None:
    session = FakeSession(
        [
            ok(7),
            ok({"accessToken": "token-value"}),
            ok({"list": [{"id": 929, "mapId": 5, "status": "running"}], "total": 1}),
        ]
    )
    client = SchedulerClient(settings(), session=session)

    page = client.list_orders(page_no=2, page_size=20, order_id=929)

    assert page.total == 1
    assert page.items[0].id == 929
    assert session.calls[0]["url"].endswith("/system/tenant/get-id-by-name")
    assert session.calls[0]["params"] == {"name": "suntae"}
    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["json"] == {"username": "operator", "password": "secret"}
    assert session.calls[1]["headers"] == {"tenant-id": "7"}
    assert session.calls[2]["method"] == "GET"
    assert session.calls[2]["params"] == {
        "pageNo": 2,
        "pageSize": 20,
        "orderIds": [929],
    }
    assert session.calls[2]["headers"] == {
        "Authorization": "Bearer token-value",
        "tenant-id": "7",
    }


def test_debug_log_keeps_complete_credentials_headers_and_response(tmp_path: Path) -> None:
    manager = configure_logging(tmp_path / "log", level="DEBUG")
    session = FakeSession(
        [
            ok(7),
            ok({"accessToken": "token-value"}),
            ok({"list": [{"id": 929, "path": [[1, 2, 0.3]]}], "total": 1}),
        ]
    )

    SchedulerClient(settings(), session=session).list_orders(page_no=1, page_size=20)
    manager.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "log" / "route-analysis.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(record.get("connection", {}).get("password") == "secret" for record in records)
    assert any(
        record.get("headers", {}).get("Authorization") == "Bearer token-value"
        for record in records
    )
    assert sum(record.get("event") == "http_response" for record in records) == 3
    assert any(
        isinstance(record.get("response"), dict)
        and isinstance(record["response"].get("data"), dict)
        and record["response"]["data"].get("total") == 1
        for record in records
    )


def test_client_uses_only_expected_read_endpoints_for_navigation_and_paths() -> None:
    session = FakeSession(
        [
            ok(7),
            ok({"accessToken": "token"}),
            ok({"list": [{"id": 11, "orderId": 9, "vin": "V1", "mapId": 3}], "total": 1}),
            ok([{"id": 22, "orderTaskId": 11, "vin": "V1", "mapId": 3}]),
            ok({"commandStr": '{"positionList":[{"x":1,"y":2,"yaw":0.1}]}'}),
            ok({"commandStr": '{"positionList":[{"x":2,"y":3,"yaw":0.2}]}'}),
        ]
    )
    client = SchedulerClient(settings(), session=session)

    task_page = client.list_tasks(order_id=9, page_no=1, page_size=50)
    commands = client.list_commands(task_id=11)
    dispatched = client.get_dispatched_path(command_id=22)
    actual = client.get_actual_path(command_id=22, vin="V1")

    assert task_page.items[0].vin == "V1"
    assert commands[0].id == 22
    assert dispatched[0].x == 1
    assert actual[0].x == 2
    business_calls = session.calls[2:]
    assert [call["method"] for call in business_calls] == ["GET"] * 4
    assert [call["url"].split("/admin-api")[-1] for call in business_calls] == [
        "/scheduling/order/detail",
        "/scheduling/order-task/work-flow",
        "/scheduling/order-task/commandStr",
        "/scheduling/order-task/actualPath",
    ]


def test_expired_token_relogs_once_and_replays_read_request() -> None:
    session = FakeSession(
        [
            ok(7),
            ok({"accessToken": "old"}),
            FakeResponse({"code": 401, "data": None, "msg": "expired"}, 401),
            ok({"accessToken": "new"}),
            ok({"list": [], "total": 0}),
        ]
    )
    client = SchedulerClient(settings(), session=session)

    result = client.list_orders(page_no=1, page_size=10)

    assert result.total == 0
    assert [call["url"].endswith("/system/auth/login") for call in session.calls].count(True) == 2
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer new"


def test_second_auth_failure_is_not_retried_and_error_hides_secrets() -> None:
    session = FakeSession(
        [
            ok(7),
            ok({"accessToken": "old"}),
            FakeResponse({"code": 401}, 401),
            ok({"accessToken": "new"}),
            FakeResponse({"code": 401}, 401),
        ]
    )
    client = SchedulerClient(settings(), session=session)

    with pytest.raises(AuthenticationError) as caught:
        client.list_orders(page_no=1, page_size=10)

    message = str(caught.value)
    assert "secret" not in message
    assert "old" not in message
    assert "new" not in message
    assert len(session.calls) == 5


@pytest.mark.parametrize(
    "url",
    ["", "example.test/admin-api", "ftp://example.test/api", "http://user:pw@example.test/api"],
)
def test_connection_rejects_unsafe_or_incomplete_api_roots(url: str) -> None:
    with pytest.raises(ValueError):
        ConnectionSettings(url, "suntae", "operator", "secret").validated_root()
