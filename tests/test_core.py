from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import SecretBox, hash_password, token_hash, verify_password
from app.upstream.client import TraeClient, TraeEvent
from app.upstream.device import device_id_for, market_user_id_for, session_id_for
from app.upstream.sse import SSEDecoder, extract_usage


def test_device_vectors_match_reference():
    assert device_id_for("1234567890123456") == "413174708280782"
    assert (
        session_id_for("1234567890123456")
        == "fbabf7aa1e173b90385c623e1ec49157860cc07a4b01f53f7ca141b17d876eae"
    )
    assert market_user_id_for("1234567890123456") == "746608f9-7f37-4960-b4c7-8553cec6d366"
    assert device_id_for("9876543210") == "630512735296035"


def test_password_and_secret_box():
    stored = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    box = SecretBox("unit-test-secret")
    encrypted = box.encrypt("refresh-token-value")
    assert encrypted != "refresh-token-value"
    assert box.decrypt(encrypted) == "refresh-token-value"


def test_sse_decoder_and_usage():
    decoder = SSEDecoder()
    assert decoder.feed_line("event: output") is None
    assert decoder.feed_line('data: {"response":"你"}') is None
    frame = decoder.feed_line("")
    assert frame is not None and frame.event == "output"
    assert json.loads(frame.data)["response"] == "你"
    assert extract_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})["total_tokens"] == 15
    nested = extract_usage({"usage": {"prompt_tokens": 3, "completion_tokens": 4}})
    assert nested["total_tokens"] == 7


def test_parse_models_filters_internal_and_parses_rates():
    client = TraeClient.__new__(TraeClient)
    root = {
        "config_info_list": [
            {
                "config_name": "DeepSeek-V4-Flash",
                "config_source": 1,
                "display_config": {"display_name": "DeepSeek V4 Flash", "is_custom_model": False},
                "model_detail_list": [{"model_name": "DeepSeek-V4-Flash", "max_input_tokens": 65536}],
                "display_contact_config": json.dumps(
                    {
                        "consumption_rate": {"data": {"rate": 0.8}},
                        "activity_discount": {
                            "data": {
                                "current": {"consumption_rate": 0.08, "before_consumption_rate": 0.8},
                                "member": {"after_consumption_rate": 0.04},
                            }
                        },
                    }
                ),
            },
            {
                "config_name": "summary",
                "config_source": 1,
                "display_config": {"display_name": "summary", "is_custom_model": False},
            },
            {
                "config_name": "third-party",
                "config_source": 3,
                "display_config": {"display_name": "third", "is_custom_model": True},
            },
        ]
    }
    models = client.parse_models(root, "solo_work_remote")
    assert len(models) == 1
    model = models[0]
    assert model["id"] == "DeepSeek-V4-Flash"
    assert model["context_length"] == 65536
    assert model["rate_activity"] == 0.08
    assert model["rate_activity_member"] == 0.04


@pytest.fixture()
def app_client(tmp_path: Path):
    cfg = Settings(data_dir=tmp_path, admin_password="TestPass123!", disable_scheduler=True)
    app = create_app(cfg)
    with TestClient(app) as client:
        yield app, client


def test_admin_login_and_pages(app_client):
    app, client = app_client
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    redirect = client.get("/admin/accounts", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/admin/platforms"
    for path in ["/admin", "/admin/platforms", "/admin/logs", "/admin/settings", "/admin/checkins"]:
        page = client.get(path)
        assert page.status_code == 200
        assert "AIAll API" in page.text
    settings_page = client.get("/admin/settings")
    assert "可观测与告警" in settings_page.text
    assert "Prometheus /metrics" in settings_page.text
    assert 'name="checkin_time"' not in settings_page.text
    assert 'name="auto_checkin"' not in settings_page.text


def test_docs_endpoints_disabled(app_client):
    """自动文档与 OpenAPI schema 关闭，避免公开部署暴露接口清单。"""
    app, client = app_client
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, follow_redirects=False).status_code == 404, path
    assert client.get("/health").status_code == 200


def test_openai_chat_stream_and_non_stream(app_client, monkeypatch):
    app, client = app_client
    ctx = app.state.ctx
    ctx.accounts._upsert(
        uid="1234567890123456",
        name="Test Account",
        auth_type="manual",
        access_token="test-access-token",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={"UserID": "1234567890123456", "ScreenName": "Test Account"},
        device_id="413174708280782",
        machine_id="1234567890123456",
    )

    async def fake_models(account, force=False):
        return [
            {
                "id": "DeepSeek-V4-Flash",
                "display_name": "DeepSeek V4 Flash",
                "model_name": "DeepSeek-V4-Flash",
                "ide_function": "solo_work_remote",
                "context_length": 65536,
                "rate_base": 0.8,
                "rate_activity": 0.08,
            }
        ]

    async def fake_chat_stream(account, body):
        yield TraeEvent("output", {"response": "你好"})
        yield TraeEvent("output", {"reasoning_content": "思考"})
        yield TraeEvent("token_usage", {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
        yield TraeEvent("done", {"finish_reason": "stop", "_done": True})

    async def fake_credits(account):
        return 99.5

    monkeypatch.setattr(ctx.accounts, "models", fake_models)
    monkeypatch.setattr(ctx.client, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(ctx.client, "get_credits", fake_credits)

    key_row = ctx.db.query_one("SELECT key_encrypted FROM api_keys")
    api_key = ctx.box.decrypt(key_row["key_encrypted"])
    headers = {"Authorization": "Bearer " + api_key}

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "DeepSeek-V4-Flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "你好"
    assert data["choices"][0]["message"]["reasoning_content"] == "思考"
    assert data["usage"]["total_tokens"] == 12

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "DeepSeek-V4-Flash", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    assert "你好" in response.text
    assert "reasoning_content" in response.text


def test_login_url_contains_pkce_and_session(app_client):
    app, client = app_client
    ctx = app.state.ctx
    url, trace, device, machine = ctx.accounts.start_login("http://127.0.0.1:2999/trae/callback")
    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(url).query)
    callback = params["auth_callback_url"][0]
    assert callback.startswith("http://127.0.0.1:57321/authorize?state=")
    state = callback.split("state=", 1)[1]
    session = ctx.db.query_one("SELECT state FROM login_sessions WHERE trace_id=?", (trace,))
    assert session["state"] == state and len(state) >= 16
    assert params["x_device_type"] == ["PC"]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) >= 40
    assert params["login_trace_id"] == [trace]
    assert params["device_id"] == [device]
    assert params["machine_id"] == [machine]
    row = ctx.db.query_one("SELECT * FROM login_sessions WHERE trace_id=?", (trace,))
    assert row is not None
    assert row["status"] == "pending"


def test_custom_openai_provider_and_virtual_group(app_client):
    app, client = app_client
    ctx = app.state.ctx
    provider = ctx.providers.create_custom(
        name="Mock OpenAI",
        base_url="https://mock.local/v1",
        protocol="openai",
        api_key="sk-mock",
        models_text="mock-model",
    )
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("stream"):
            chunk = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "mock-model",
                "choices": [{"index": 0, "delta": {"content": "mock-ok"}, "finish_reason": None}],
            }
            content = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 1,
                "model": "mock-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock-ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    ctx.router.openai.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    key_row = ctx.db.query_one("SELECT key_encrypted FROM api_keys")
    api_key = ctx.box.decrypt(key_row["key_encrypted"])
    headers = {"Authorization": "Bearer " + api_key}

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "mock-ok"

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert "mock-ok" in response.text
    assert "data: [DONE]" in response.text

    group_id = ctx.virtuals.create("fast", "Fast")
    ctx.virtuals.add_member(group_id, provider["id"], "mock-model", 1)
    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "mock-ok"


def test_custom_anthropic_provider_translation(app_client):
    app, client = app_client
    ctx = app.state.ctx
    ctx.providers.create_custom(
        name="Mock Anthropic",
        base_url="https://anthropic.mock/v1",
        protocol="anthropic",
        api_key="sk-ant-mock",
        models_text="claude-mock",
    )
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": "claude-mock",
                "content": [{"type": "text", "text": "anthropic-ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    ctx.router.anthropic.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    key_row = ctx.db.query_one("SELECT key_encrypted FROM api_keys")
    api_key = ctx.box.decrypt(key_row["key_encrypted"])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + api_key},
        json={"model": "claude-mock", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "anthropic-ok"
    assert body["usage"]["prompt_tokens"] == 4
    assert body["usage"]["completion_tokens"] == 2


def test_no_account_returns_503(app_client):
    app, client = app_client
    ctx = app.state.ctx
    key_row = ctx.db.query_one("SELECT key_encrypted FROM api_keys")
    api_key = ctx.box.decrypt(key_row["key_encrypted"])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + api_key},
        json={"model": "DeepSeek-V4-Flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_available_account"


def test_platforms_seeded_and_admin_pages(app_client):
    app, client = app_client
    ids = {p["id"] for p in app.state.ctx.providers.list_providers()}
    assert {
        "trae-cn",
        "trae-global",
        "workbuddy",
        "codebuddy",
        "lobster",
        "qoder-cn",
        "qoder-global",
    } <= ids
    providers = {p["id"]: p for p in app.state.ctx.providers.list_providers()}
    assert providers["qoder-cn"]["config"]["openapi"] == "https://openapi.qoder.com.cn"
    assert providers["qoder-global"]["config"]["openapi"] == "https://openapi.qoder.sh"
    assert app.state.ctx.db.get_setting("qoder_checkin_time", "") == ""
    assert app.state.ctx.db.get_setting("qoder_auto_checkin", "") == ""
    assert app.state.ctx.db.get_setting("checkin_time") == "10:05"
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    for path in ["/admin/platforms", "/admin/virtuals"]:
        page = client.get(path)
        assert page.status_code == 200
        assert "_csrf" in page.text
    assert client.get("/admin/models", follow_redirects=False).status_code == 303
    assert client.get("/admin/providers", follow_redirects=False).status_code == 303


def test_workbuddy_prepare_body_adds_system_and_stream():
    from app.providers.workbuddy import WorkBuddyProvider

    body = WorkBuddyProvider.prepare_body({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert body["stream"] is True
    assert body["cache_retention"] == "long"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "hi"


def test_qoder_cosy_encode_roundtrip():
    from app.providers.qoder import decode_body, encode_body

    raw = json.dumps({"model": "qwen3.7-plus", "messages": [{"role": "user", "content": "你好"}]}).encode()
    encoded = encode_body(raw)
    assert "=" not in encoded
    assert decode_body(encoded) == raw


def test_qoder_checkin_claims_campaign():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            seen["auth"] = request.headers.get("authorization")
            seen["client_type"] = request.headers.get("cosy-clienttype")
            return httpx.Response(
                200,
                json={
                    "campaigns": [
                        {
                            "campaignId": "campaign-1",
                            "actionType": "CLAIM_BENEFIT",
                            "claimStatus": "CLAIMABLE",
                            "benefit": {"amount": 100, "validity": {"days": 30}},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns/campaign-1/claim"):
            seen["claim"] = True
            return httpx.Response(
                200,
                json={"status": "CLAIMED", "replayed": False, "benefit": {"amount": 100, "validity": {"days": 30}}},
            )
        return httpx.Response(404)

    provider = {"region": "cn", "config": {"openapi": "https://openapi.qoder.com.cn"}}
    account = {"credential": {"uid": "u1", "access_token": "dt-test"}}
    adapter = QoderProvider(Settings(data_dir=Path(tempfile.mkdtemp())), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(adapter.checkin(provider, account))
    assert seen == {"auth": "Bearer dt-test", "client_type": "10", "claim": True}
    assert result["status"] == "success"
    assert result["credits"] == 100
    assert "30 天有效" in result["message"]


def test_qoder_checkin_global_uses_openapi_sh_and_fingerprint():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            seen["url"] = str(request.url.copy_with(query=None))
            seen["force_refresh"] = request.url.params.get("forceRefresh")
            seen["headers"] = {key.lower(): value for key, value in request.headers.items()}
            return httpx.Response(
                200,
                json={
                    "campaigns": [
                        {
                            "campaignId": "global-campaign",
                            "actionType": "CLAIM_BENEFIT",
                            "claimStatus": "CLAIMABLE",
                            "benefit": {"amount": 50, "validity": {"days": 7}},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns/global-campaign/claim"):
            seen["claim_origin"] = request.headers.get("origin")
            seen["claim_body"] = request.content.decode()
            return httpx.Response(
                200,
                json={"status": "CLAIMED", "benefit": {"amount": 50, "validity": {"days": 7}}},
            )
        return httpx.Response(404)

    provider = {"region": "global", "config": {}}
    account = {
        "credential": {
            "uid": "u-global",
            "access_token": "dt-global",
            "checkin_fingerprint": {
                "version": "9.9.9",
                "machine_id": "mid-1",
                "machine_token": "mt-1",
                "machine_type": "mac",
                "machine_code": "mc-1",
                "hostname": "host-1",
                "machine_os": "macos",
            },
        }
    }
    adapter = QoderProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(adapter.checkin(provider, account))
    assert str(seen["url"]) == "https://openapi.qoder.sh/sash/api/v1/me/campaigns"
    assert seen["force_refresh"] == "true"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer dt-global"
    assert headers["cosy-clienttype"] == "10"
    assert headers["cosy-version"] == "9.9.9"
    assert headers["cosy-machineid"] == "mid-1"
    assert headers["cosy-machinetoken"] == "mt-1"
    assert headers["cosy-machinetype"] == "mac"
    assert headers["cosy-machinecode"] == "mc-1"
    assert headers["cosy-machinehostname"] == "host-1"
    assert headers["cosy-machineos"] == "macos"
    assert seen["claim_origin"] == "https://openapi.qoder.sh"
    assert json.loads(str(seen["claim_body"])) == {}
    assert result["status"] == "success"
    assert result["credits"] == 50.0
    assert result["message"] == "签到成功 +50 积分（7 天有效）"
    assert result["valid_until"] > 0
    assert result["packs"][0]["name"] == "每日签到奖励"
    assert result["packs"][0]["expire_at"] == result["valid_until"]


def test_qoder_checkin_already_claimed_variants():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    def run(case: str) -> tuple[dict, int]:
        claim_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal claim_calls
            if request.url.path.endswith("/sash/api/v1/me/campaigns"):
                if case == "campaign_claimed":
                    return httpx.Response(
                        200,
                        json={
                            "campaigns": [
                                {"campaignId": "c1", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMED"}
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "campaigns": [
                            {"campaignId": "c1", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE"}
                        ]
                    },
                )
            if request.url.path.endswith("/sash/api/v1/me/campaigns/c1/claim"):
                claim_calls += 1
                if case == "http_409":
                    return httpx.Response(409, json={"message": "already"})
                if case == "result":
                    return httpx.Response(200, json={"result": "ALREADY_CLAIMED"})
                if case == "replayed":
                    return httpx.Response(200, json={"replayed": True})
                return httpx.Response(200, json={"status": "CLAIMED"})
            return httpx.Response(404)

        adapter = QoderProvider(
            Settings(data_dir=Path(tempfile.mkdtemp())),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        provider = {"region": "cn", "config": {"openapi": "https://openapi.qoder.com.cn"}}
        account = {"credential": {"uid": "u1", "access_token": "dt-test"}}
        return asyncio.run(adapter.checkin(provider, account)), claim_calls

    for case in ("campaign_claimed", "http_409", "result", "replayed"):
        result, claim_calls = run(case)
        assert result["status"] == "already", case
        assert result["message"] == "今天已签到", case
        assert claim_calls == (0 if case == "campaign_claimed" else 1), case


def test_qoder_checkin_pat_exchanges_for_job_token():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/jobToken/exchange"):
            seen["exchange_body"] = json.loads(request.content.decode())
            seen["exchange_client_type"] = request.headers.get("cosy-clienttype")
            return httpx.Response(200, json={"accessToken": "jt-exchanged"})
        if request.url.path.endswith("/sash/api/v1/me/campaigns"):
            seen["campaign_auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "campaigns": [
                        {"campaignId": "c-pat", "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE"}
                    ]
                },
            )
        if request.url.path.endswith("/sash/api/v1/me/campaigns/c-pat/claim"):
            seen["claim_auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"status": "CLAIMED", "benefit": {"amount": 10}})
        return httpx.Response(404)

    adapter = QoderProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = {"region": "cn", "config": {"openapi": "https://openapi.qoder.com.cn"}}
    account = {"credential": {"uid": "u-pat", "access_token": "pt-personal"}}
    result = asyncio.run(adapter.checkin(provider, account))
    assert seen["exchange_body"] == {"personal_token": "pt-personal"}
    assert seen["exchange_client_type"] == "10"
    assert seen["campaign_auth"] == "Bearer jt-exchanged"
    assert seen["claim_auth"] == "Bearer jt-exchanged"
    assert result["status"] == "success"
    assert result["credits"] == 10


def test_qoder_international_no_campaign_hint():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openapi.qoder.sh"
        return httpx.Response(200, json={"campaigns": []})

    adapter = QoderProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.checkin(
            {"region": "global", "config": {}},
            {"credential": {"uid": "u-global", "access_token": "dt-global"}},
        )
    )
    assert result["status"] == "unavailable"
    assert "需要先在官方客户端签到一次或配置真实设备指纹" in result["message"]


def test_unified_scheduler_runs_all_platforms_once(app_client, monkeypatch):
    import asyncio

    app, client = app_client
    ctx = app.state.ctx
    platform_calls: list[object] = []
    trae_calls = 0

    async def fake_platform_checkin_all(kinds=None):
        platform_calls.append(kinds)
        return [{"account": "all/test", "status": "success", "message": "ok"}]

    async def fake_trae_checkin_all():
        nonlocal trae_calls
        trae_calls += 1
        return [{"account": "Trae/test", "status": "success", "message": "ok"}]

    monkeypatch.setattr(ctx.platforms, "checkin_all", fake_platform_checkin_all)
    monkeypatch.setattr(ctx.accounts, "checkin_all", fake_trae_checkin_all)
    ctx.db.set_setting("auto_checkin", "1")
    ctx.db.set_setting("checkin_time", "00:00")
    ctx.db.set_setting("last_checkin_run", "")

    asyncio.run(ctx.scheduler._tick())
    assert trae_calls == 1
    assert platform_calls == [None]
    row = ctx.db.query_one("SELECT * FROM task_runs WHERE task='platform_checkin'")
    assert row is not None and row["success"] == 1 and row["all_failed"] == 0
    assert ctx.db.query_one("SELECT * FROM task_runs WHERE task='qoder_checkin'") is None

    asyncio.run(ctx.scheduler._tick())
    assert trae_calls == 1
    assert platform_calls == [None]

    ctx.db.set_setting("auto_checkin", "0")
    ctx.db.set_setting("last_checkin_run", "")
    asyncio.run(ctx.scheduler._tick())
    assert trae_calls == 1
    assert platform_calls == [None]

def test_lobster_checkin_available_claims_and_reads_credits():
    import asyncio
    import time

    import httpx

    from app.providers.lobster import LobsterProvider

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/client-activities/slot"):
            seen["slot_params"] = dict(request.url.params)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "slotState": "available",
                        "activity": {"activityCode": "lobster-act", "configRevision": 123},
                    },
                },
            )
        if request.url.path.endswith("/api/client-activities/lobster-act/context"):
            seen["context_rev"] = request.url.params.get("configRevision")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"state": {"claimedToday": False}, "actions": ["check_in"]}},
            )
        if request.url.path.endswith("/api/client-activities/lobster-act/actions/check_in"):
            seen["claim_body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={"code": 0, "data": {"result": {"creditsGranted": 100}}},
            )
        return httpx.Response(404)

    provider = {"base_url": "https://lobster.test", "config": {"client_version": "2026.11.6"}}
    account = {
        "credential": {
            "access_token": "lobster-token",
            "expires_at": int(time.time()) + 3600,
            "uid": "lu1",
        }
    }
    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(adapter.checkin(provider, account))
    assert seen["auth"] == "Bearer lobster-token"
    assert seen["slot_params"] == {
        "placement": "desktop_sidebar",
        "clientVersion": "2026.11.6",
        "containerApiVersion": "2",
        "platform": "win32",
    }
    assert seen["context_rev"] == "123"
    body = seen["claim_body"]
    assert body["configRevision"] == 123
    assert body["payload"] == {}
    assert body["idempotencyKey"]
    assert result == {"status": "success", "credits": 100.0, "message": "签到成功 +100 积分"}


def test_lobster_checkin_state_variants():
    import asyncio
    import time

    import httpx

    from app.providers.lobster import LobsterProvider

    def run(case: str) -> dict:
        claim_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal claim_calls
            if request.url.path.endswith("/api/client-activities/slot"):
                if case == "no_slot":
                    return httpx.Response(200, json={"code": 0, "data": {"slotState": "closed"}})
                if case == "no_activity":
                    return httpx.Response(200, json={"code": 0, "data": {"slotState": "available", "activity": {}}})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "slotState": "available",
                            "activity": {"activityCode": "a1", "configRevision": 1},
                        },
                    },
                )
            if request.url.path.endswith("/api/client-activities/a1/context"):
                if case == "claimed":
                    return httpx.Response(
                        200, json={"code": 0, "data": {"state": {"claimedToday": True}, "actions": ["check_in"]}}
                    )
                return httpx.Response(
                    200, json={"code": 0, "data": {"state": {"claimedToday": False}, "actions": ["view"]}}
                )
            if request.url.path.endswith("/api/client-activities/a1/actions/check_in"):
                claim_calls += 1
                return httpx.Response(200, json={"code": 0, "data": {"result": {"credits": 5}}})
            return httpx.Response(404)

        adapter = LobsterProvider(
            Settings(data_dir=Path(tempfile.mkdtemp())),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result = asyncio.run(
            adapter.checkin(
                {"base_url": "https://lobster.test", "config": {}},
                {"credential": {"access_token": "t", "expires_at": int(time.time()) + 3600}},
            )
        )
        assert claim_calls == 0, case
        return result

    assert run("no_slot") == {"status": "unavailable", "message": "当前无签到槽: closed"}
    assert run("no_activity") == {"status": "unavailable", "message": "签到活动未开放"}
    assert run("claimed") == {"status": "already", "message": "今天已签到"}
    assert run("no_action") == {"status": "unavailable", "message": "签到动作不可用"}


def test_lobster_checkin_refreshes_expiring_token():
    import asyncio
    import time

    import httpx

    from app.providers.lobster import LobsterProvider

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/auth/refresh"):
            seen["refresh_body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={"code": 0, "data": {"accessToken": "lobster-new", "refreshToken": "r2", "expiresIn": 3600}},
            )
        if request.url.path.endswith("/api/client-activities/slot"):
            seen["slot_auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"code": 0, "data": {"slotState": "closed"}})
        return httpx.Response(404)

    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    saved: dict[str, object] = {}
    adapter._credential_saver = lambda account_id, cred, expires: saved.update(
        {"account_id": account_id, "token": cred.get("access_token"), "expires": expires}
    )
    result = asyncio.run(
        adapter.checkin(
            {"base_url": "https://lobster.test", "config": {}},
            {
                "id": 7,
                "credential": {
                    "access_token": "lobster-old",
                    "refresh_token": "r1",
                    "expires_at": int(time.time()) + 60,
                },
            },
        )
    )
    assert seen["refresh_body"]["refreshToken"] == "r1"
    assert seen["slot_auth"] == "Bearer lobster-new"
    assert saved["account_id"] == 7
    assert saved["token"] == "lobster-new"
    assert result["status"] == "unavailable"


def test_lobster_checkin_401_maps_to_reauth():
    import asyncio
    import time

    import httpx
    import pytest

    from app.providers.base import ProviderError
    from app.providers.lobster import LobsterProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "msg": "unauthorized"})

    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as exc:
        asyncio.run(
            adapter.checkin(
                {"base_url": "https://lobster.test", "config": {}},
                {"credential": {"access_token": "expired", "expires_at": int(time.time()) + 3600}},
            )
        )
    assert "重新授权" in str(exc.value)


def test_lobster_platform_checkin_and_scheduler_coverage(app_client, monkeypatch):
    import asyncio

    from app.providers.lobster import LobsterProvider

    app, client = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "lobster", "lobster-test", {"access_token": "lobster-token", "uid": "lu1"}, uid="lu1"
    )

    async def fake_checkin(self, provider, account):
        return {"status": "success", "credits": 100, "message": "签到成功 +100 积分"}

    monkeypatch.setattr(LobsterProvider, "checkin", fake_checkin)
    results = asyncio.run(ctx.platforms.checkin_all(kinds=("lobster",)))
    assert results and results[0]["status"] == "success"
    row = ctx.db.query_one("SELECT status, credits FROM platform_checkins WHERE account_name='lobster-test'")
    assert row is not None and row["status"] == "success" and row["credits"] == 100

    platform_calls: list[object] = []

    async def fake_platform_checkin_all(kinds=None):
        platform_calls.append(kinds)
        return [{"account": "LobsterAI/lobster-test", "status": "success", "message": "ok"}]

    async def fake_trae_checkin_all():
        return []

    monkeypatch.setattr(ctx.platforms, "checkin_all", fake_platform_checkin_all)
    monkeypatch.setattr(ctx.accounts, "checkin_all", fake_trae_checkin_all)
    ctx.db.set_setting("auto_checkin", "1")
    ctx.db.set_setting("checkin_time", "00:00")
    ctx.db.set_setting("last_checkin_run", "")
    asyncio.run(ctx.scheduler._tick())
    assert platform_calls == [None]

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    platforms_page = client.get("/admin/platforms")
    assert platforms_page.status_code == 200
    assert f"/admin/platforms/accounts/{account_id}/checkin" in platforms_page.text
    assert "/admin/platforms/lobster/checkin-all" in platforms_page.text


def test_platform_checkin_all_includes_qoder(app_client, monkeypatch):
    import asyncio

    from app.providers.qoder import QoderProvider

    app, client = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "qoder-cn", "qoder-test", {"access_token": "dt-test", "uid": "qu1"}, uid="qu1"
    )

    async def fake_checkin(self, provider, account):
        return {"status": "success", "credits": 100, "message": "签到成功 +100 积分"}

    monkeypatch.setattr(QoderProvider, "checkin", fake_checkin)
    results = asyncio.run(ctx.platforms.checkin_all(kinds=("qoder",)))
    assert len(results) == 1
    assert results[0]["status"] == "success"
    row = ctx.db.query_one("SELECT status FROM platform_checkins WHERE account_name='qoder-test'")
    assert row is not None and row["status"] == "success"

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    platforms_page = client.get("/admin/platforms")
    assert platforms_page.status_code == 200
    assert f"/admin/platforms/accounts/{account_id}/checkin" in platforms_page.text
    assert "/admin/platforms/qoder-cn/checkin-all" in platforms_page.text
    import re

    csrf = re.search(r'name="_csrf" value="([^"]+)"', platforms_page.text).group(1)
    response = client.post(
        "/admin/platforms/qoder-cn/checkin-all",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        f"/admin/platforms/accounts/{account_id}/checkin",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    checkins_page = client.get("/admin/checkins")
    assert checkins_page.status_code == 200
    assert "每日签到时间" in checkins_page.text
    assert "今日是否执行" in checkins_page.text
    assert "已连续签到" in checkins_page.text


def test_virtual_group_routes_to_platform(app_client):
    app, client = app_client
    ctx = app.state.ctx
    import httpx

    ctx.providers.add_credential_account(
        "workbuddy",
        "wb-test",
        {"access_token": "tok", "refresh_token": "ref", "expires_at": 9999999999, "uid": "u1"},
        uid="u1",
    )
    group_id = ctx.virtuals.create("fast", "Fast")
    ctx.virtuals.add_member(group_id, "workbuddy", "deepseek-v4.1-flash", 1)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        body = json.loads(request.content.decode())
        seen["model"] = body.get("model")
        frame = {
            "id": "chatcmpl-wb",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "delta": {"content": "wb-ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
        content = "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})

    ctx.providers.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    key = ctx.box.decrypt(ctx.db.query_one("SELECT key_encrypted FROM api_keys")["key_encrypted"])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + key},
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "wb-ok"
    assert seen["path"] == "/v2/chat/completions"
    assert seen["auth"] == "Bearer tok"
    assert seen["model"] == "deepseek-v4.1-flash"


def test_models_endpoint_only_lists_virtual_groups(app_client):
    app, client = app_client
    ctx = app.state.ctx
    ctx.virtuals.create("only-group", "Only Group")
    key = ctx.box.decrypt(ctx.db.query_one("SELECT key_encrypted FROM api_keys")["key_encrypted"])
    response = client.get("/v1/models", headers={"Authorization": "Bearer " + key})
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["data"]] == ["only-group"]


def test_trae_token_exchange_page(app_client, monkeypatch):
    import re

    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    redirect = client.get("/admin/platforms/trae/login?region=global", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/admin/trae/token?region=global"
    page = client.get("/admin/trae/token?region=global")
    assert page.status_code == 200
    assert "Trae 国际版 Cookie 登录" in page.text
    assert "Cookie 登录并保存账号" in page.text
    assert "备用方式：Refresh Token 登录" in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    seen = {}

    async def fake_exchange(refresh_token, uid="", **kwargs):
        seen["refresh_token"] = refresh_token
        seen["uid"] = uid
        seen["oauth_host"] = kwargs.get("oauth_host")
        return {
            "access_token": "access-token-value",
            "refresh_token": "new-refresh-token-value",
            "expires_at": 1999999999,
        }

    async def fake_info(access_token, **kwargs):
        seen["info_token"] = access_token
        return {"UserID": "user-1", "ScreenName": "Tester"}

    monkeypatch.setattr(app.state.ctx.client, "exchange_token", fake_exchange)
    monkeypatch.setattr(app.state.ctx.client, "get_user_info", fake_info)
    response = client.post(
        "/admin/trae/token",
        data={
            "_csrf": csrf,
            "region": "global",
            "refresh_token": "old-refresh-token",
            "uid": "",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "access-token-value" in response.text
    assert "new-refresh-token-value" in response.text
    assert "Tester" in response.text
    assert "账号已保存" in response.text
    account = app.state.ctx.db.query_one("SELECT * FROM accounts WHERE uid='user-1'")
    assert account is not None and account["region"] == "global"
    assert seen == {
        "refresh_token": "old-refresh-token",
        "uid": "",
        "oauth_host": "https://www.trae.ai",
        "info_token": "access-token-value",
    }


def test_trae_global_cookie_login_saves_session(app_client, monkeypatch):
    import re

    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/trae/token?region=global")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    seen = {}

    async def fake_session(cookie, **kwargs):
        seen["cookie"] = cookie
        seen["oauth_host"] = kwargs.get("oauth_host")
        seen["console_host"] = kwargs.get("console_host")
        return {"access_token": "cookie-access", "expires_at": 1999999999}

    async def fake_info(access_token, **kwargs):
        seen["info_token"] = access_token
        seen["info_host"] = kwargs.get("oauth_host")
        return {"UserID": "cookie-user", "ScreenName": "Cookie Tester"}

    monkeypatch.setattr(app.state.ctx.client, "get_token_from_session", fake_session)
    monkeypatch.setattr(app.state.ctx.client, "get_user_info", fake_info)
    response = client.post(
        "/admin/trae/cookie",
        data={
            "_csrf": csrf,
            "region": "global",
            "session_cookie": "X-Cloudide-Session=cookie-value",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/platforms?msg=")
    assert seen == {
        "cookie": "cookie-value",
        "oauth_host": "https://api.trae.ai",
        "console_host": "https://www.trae.ai",
        "info_token": "cookie-access",
        "info_host": "https://api.trae.ai",
    }
    account = app.state.ctx.db.query_one("SELECT * FROM accounts WHERE uid='cookie-user'")
    assert account is not None
    assert account["region"] == "global"
    assert account["auth_type"] == "session"


def test_checkin_settings_page_and_save(app_client):
    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/checkins")
    assert page.status_code == 200
    assert "自动签到" in page.text
    import re

    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/admin/checkins/settings",
        data={
            "_csrf": csrf,
            "auto_checkin": "1",
            "checkin_time": "01:00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert app.state.ctx.db.get_setting("auto_checkin") == "1"
    assert app.state.ctx.db.get_setting("checkin_time") == "01:00"
    assert app.state.ctx.db.get_setting("qoder_auto_checkin", "") == ""
    assert app.state.ctx.db.get_setting("qoder_checkin_time", "") == ""
    response = client.post(
        "/admin/checkins/run-all",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_pending_login_expires(app_client):
    app, client = app_client
    ctx = app.state.ctx
    now = int(__import__("time").time())
    ctx.db.execute(
        "INSERT INTO platform_logins(id, provider_id, state, auth_url, extra_json, created_at, expires_at, status, message) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("expired-login", "workbuddy", "s", "https://example.com", "{}", now - 1000, now - 1, "pending", ""),
    )
    login = ctx.platforms.get_login("expired-login")
    assert login is not None
    assert login["status"] == "expired"
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms")
    assert page.status_code == 200
    assert "expired-login" not in page.text


def test_trae_credit_detail_parser():
    from app.upstream.client import TraeClient

    detail = TraeClient._credit_detail_from_usage(
        {"usage_summary": {"remaining_amount": 70, "consumed_amount": 30, "total_amount": 100}}
    )
    assert {k: detail[k] for k in ("remaining", "used", "total")} == {
        "remaining": 70.0,
        "used": 30.0,
        "total": 100.0,
    }
    assert detail["packs"][0]["name"] == "总积分"
    assert detail["packs"][0]["remain"] == 70.0
    packs = TraeClient._credit_detail_from_usage(
        {
            "user_entitlement_pack_list": [
                {
                    "entitlement_base_info": {"quota": {"credits_limit": 100}},
                    "usage": {"credits_amount": 25},
                }
            ]
        }
    )
    assert {k: packs[k] for k in ("remaining", "used", "total")} == {
        "remaining": 75.0,
        "used": 25.0,
        "total": 100.0,
    }
    assert packs["packs"][0]["remain"] == 75.0
    assert packs["packs"][0]["total"] == 100.0
    assert packs["packs"][0]["expire_at"] == 0
    derived = TraeClient._credit_detail_from_usage(
        {"usage_summary": {"consumed_amount": 0.09, "total_amount": 5400}}
    )
    assert derived["remaining"] == 5399.91
    assert derived["used"] == 0.09
    assert derived["total"] == 5400.0
    derived = TraeClient._credit_detail_from_usage(
        {"usage_summary": {"consumed_amount": 0.09, "total_amount": 5400}}
    )
    assert derived["remaining"] == 5399.91
    assert derived["used"] == 0.09
    assert derived["total"] == 5400.0


def test_platform_detail_page(app_client):
    app, client = app_client
    ctx = app.state.ctx
    account = ctx.accounts._upsert(
        uid="1234567890123456",
        name="Detail Account",
        auth_type="manual",
        access_token="token",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={},
        device_id="413174708280782",
        machine_id="1234567890123456",
    )
    ctx.db.execute(
        "UPDATE accounts SET credits=70, credits_used=30, credits_total=100 WHERE id=?",
        (account["id"],),
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms/trae-cn")
    assert page.status_code == 200
    assert "Detail Account" in page.text
    assert "剩余积分" in page.text
    assert "已用积分" in page.text


def test_platform_detail_refresh_returns_json_without_redirect(app_client):
    app, client = app_client
    ctx = app.state.ctx
    account = ctx.accounts._upsert(
        uid="9876543210123456",
        name="JSON Account",
        auth_type="manual",
        access_token="token",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={},
        device_id="413174708280783",
        machine_id="9876543210123456",
    )
    ctx.db.execute(
        "UPDATE accounts SET credits=12.5, credits_used=7.5, credits_total=20 WHERE id=?",
        (account["id"],),
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms/trae-cn")
    assert page.status_code == 200
    assert "js-refresh" in page.text
    assert 'data-account-id="%d"' % account["id"] in page.text
    import re

    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/admin/platforms/trae-cn/quota-all",
        data={"_csrf": csrf},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    row = next(a for a in payload["accounts"] if a["id"] == account["id"])
    assert row["credits"] == 12.5
    assert row["credits_used"] == 7.5
    assert row["credits_total"] == 20.0
    assert payload["totals"]["remaining"] == 12.5


def test_account_pool_weighted_cooldown_and_session(app_client):
    app, _ = app_client
    ctx = app.state.ctx
    low = ctx.providers.add_credential_account(
        "workbuddy", "low", {"access_token": "t1", "uid": "u-low"}, uid="u-low"
    )
    high = ctx.providers.add_credential_account(
        "workbuddy", "high", {"access_token": "t2", "uid": "u-high"}, uid="u-high"
    )
    low_id = int(low)
    high_id = int(high)
    ctx.db.execute("UPDATE provider_accounts SET credits=10, last_used_at=0 WHERE id=?", (low_id,))
    ctx.db.execute("UPDATE provider_accounts SET credits=1000, last_used_at=0 WHERE id=?", (high_id,))

    accounts = ctx.providers.accounts("workbuddy", include_disabled=False)
    order = ctx.pool.candidates("workbuddy", "m", set(), "", accounts)
    assert order, "pool should return candidates"
    # 高积分账号在加权抽签中应显著靠前
    assert int(order[0]["id"]) in {low_id, high_id}

    # 429 → 软冷却，立即被排除在健康候选之外
    result = ctx.pool.mark_failure(high_id, "workbuddy", "m", 429, "rate limited")
    assert result["kind"] == "soft_rate"
    row = ctx.db.query_one("SELECT cooldown_until FROM provider_accounts WHERE id=?", (high_id,))
    assert int(row["cooldown_until"]) > int(__import__("time").time())

    ctx.pool.mark_success(high_id, "workbuddy", "m", 0, 0)
    row = ctx.db.query_one(
        "SELECT cooldown_until, fail_streak FROM provider_accounts WHERE id=?", (high_id,)
    )
    assert int(row["cooldown_until"]) == 0
    assert int(row["fail_streak"]) == 0

    # 会话粘性：绑定后第一个候选固定为该账号
    ctx.pool.bind_session("conv:abc", "workbuddy", low_id)
    accounts = ctx.providers.accounts("workbuddy", include_disabled=False)
    sticky = ctx.pool.candidates("workbuddy", "m", set(), "conv:abc", accounts)
    assert int(sticky[0]["id"]) == low_id


def test_model_cost_ledger(app_client):
    app, _ = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "workbuddy", "cost-account", {"access_token": "t", "uid": "u-cost"}, uid="u-cost"
    )
    ctx.pool.note_model_cost(int(account_id), "workbuddy", "deepseek-v4.1-flash", 1.0, 1000)
    ctx.pool.note_model_cost(int(account_id), "workbuddy", "deepseek-v4.1-flash", 2.0, 1000)
    cost = ctx.pool.model_cost("workbuddy", "deepseek-v4.1-flash")
    assert cost["samples"] == 2
    assert 1.0 <= float(cost["cost_per_1k"]) <= 2.0
    tier, _ = ctx.pool.cost_tier(int(account_id), "deepseek-v4.1-flash")
    assert tier == 2
    # 免费观测（credits=0）应进入 tier 0
    ctx.pool.note_model_cost(int(account_id), "workbuddy", "free-model", 0.0, 1000)
    assert ctx.pool.cost_tier(int(account_id), "free-model")[0] == 0


def test_cost_tier_prefers_free_and_explores_unknown(app_client):
    app, _ = app_client
    ctx = app.state.ctx
    free_id = int(
        ctx.providers.add_credential_account(
            "workbuddy", "free", {"access_token": "t1", "uid": "u-free"}, uid="u-free"
        )
    )
    unknown_id = int(
        ctx.providers.add_credential_account(
            "workbuddy", "unknown", {"access_token": "t2", "uid": "u-unknown"}, uid="u-unknown"
        )
    )
    ctx.db.execute("UPDATE provider_accounts SET credits=100 WHERE id=?", (free_id,))
    ctx.db.execute("UPDATE provider_accounts SET credits=100 WHERE id=?", (unknown_id,))
    ctx.pool.note_model_cost(free_id, "workbuddy", "m", 0.0, 1000)

    accounts = ctx.providers.accounts("workbuddy", include_disabled=False)
    # 关闭条件探索时，只选实测免费层
    ctx.db.set_setting("pool_cost_explore_interval", "0")
    order = ctx.pool.candidates("workbuddy", "m", set(), "", accounts)
    assert int(order[0]["id"]) == free_id

    # 间隔设为 0 时关闭探索；设为 1 秒并等待后触发探索，未知号进入候选
    ctx.db.set_setting("pool_cost_explore_interval", "1")
    ctx.db.execute("DELETE FROM cost_explore")
    import time as _t

    _t.sleep(1.1)
    order = ctx.pool.candidates("workbuddy", "m", set(), "", accounts)
    ids = [int(r["id"]) for r in order[:2]]
    assert unknown_id in ids
    report = ctx.pool.explore_report()
    assert any(r["model"] == "m" for r in report)


def test_pool_lru_fallback_covers_all(app_client):
    app, _ = app_client
    ctx = app.state.ctx
    ids = [
        int(ctx.providers.add_credential_account("workbuddy", f"a{i}", {"access_token": f"t{i}", "uid": f"u{i}"}, uid=f"u{i}"))
        for i in range(3)
    ]
    ctx.db.set_setting("pool_min_pick_gap_ms", "5000")
    now = int(__import__("time").time())
    for offset, account_id in enumerate(ids):
        ctx.db.execute(
            "UPDATE provider_accounts SET credits=100, last_picked_at=? WHERE id=?",
            (now - offset, account_id),
        )
    accounts = ctx.providers.accounts("workbuddy", include_disabled=False)
    order = ctx.pool.candidates("workbuddy", "m", set(), "", accounts)
    # 全部刚被选过时按 LRU 兜底，最早被选中的排最前
    assert int(order[0]["id"]) == ids[2]


def test_dashboard_overview_has_all_platforms(app_client):
    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin")
    assert page.status_code == 200
    assert "平台概览" in page.text
    overview = app.state.ctx.stats.overview()
    ids = {row["id"] for row in overview["platform_rows"]}
    assert {"trae-cn", "trae-global", "workbuddy", "codebuddy", "lobster", "qoder-cn", "qoder-global"} <= ids


def test_manual_disable_excludes_from_pool_and_revive_clears(app_client):
    app, client = app_client
    ctx = app.state.ctx
    account_id = int(
        ctx.providers.add_credential_account(
            "workbuddy", "manual", {"access_token": "t", "uid": "u-manual"}, uid="u-manual"
        )
    )
    accounts = ctx.providers.accounts("workbuddy", include_disabled=False)
    assert any(int(a["id"]) == account_id for a in ctx.pool.candidates("workbuddy", "m", set(), "", accounts))

    ctx.pool.set_manual_disabled(account_id, True, "观察中")
    accounts = ctx.providers.accounts("workbuddy", include_disabled=False)
    assert not any(int(a["id"]) == account_id for a in ctx.pool.candidates("workbuddy", "m", set(), "", accounts))
    row = ctx.db.query_one("SELECT manual_disabled, manual_reason FROM provider_accounts WHERE id=?", (account_id,))
    assert int(row["manual_disabled"]) == 1
    assert row["manual_reason"] == "观察中"

    # 手动停用不影响签到选择（签到只看 enabled）
    enabled_rows = ctx.db.query(
        "SELECT id FROM provider_accounts WHERE provider_id='workbuddy' AND enabled=1"
    )
    assert any(int(r["id"]) == account_id for r in enabled_rows)

    ctx.pool.mark_failure(account_id, "workbuddy", "m", 429, "rate limited")
    ctx.pool.set_manual_disabled(account_id, False)
    ctx.pool.clear_cooldown(account_id)
    row = ctx.db.query_one(
        "SELECT manual_disabled, cooldown_until, fail_streak FROM provider_accounts WHERE id=?", (account_id,)
    )
    assert int(row["manual_disabled"]) == 0
    assert int(row["cooldown_until"]) == 0
    assert int(row["fail_streak"]) == 0


def test_daily_budget_gate_blocks_before_chat(app_client):
    app, client = app_client
    ctx = app.state.ctx
    key = ctx.box.decrypt(ctx.db.query_one("SELECT key_encrypted FROM api_keys")["key_encrypted"])
    ctx.db.set_setting("daily_credit_limit", "1")
    now = int(__import__("time").time())
    ctx.db.execute(
        "INSERT INTO usage_logs(request_id, api_key_id, api_key_name, account_id, account_name, endpoint, model, "
        "stream, prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens, credits_before, "
        "credits_after, credits_delta, credits_estimated, latency_ms, status, error, ip, provider_id, virtual_model, "
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "test-budget",
            1,
            "default",
            0,
            "test",
            "/v1/chat/completions",
            "m",
            0,
            1,
            1,
            2,
            0,
            0,
            -1,
            -1,
            2.0,
            0,
            10,
            "ok",
            "",
            "127.0.0.1",
            "trae-cn",
            "",
            now,
        ),
    )
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + key},
        json={"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "daily_budget_exceeded"
    status = client.get("/v1/status", headers={"Authorization": "Bearer " + key}).json()
    assert status["daily_budget"]["limit"] == 1.0
    assert status["daily_budget"]["rejected"] >= 1
    assert status["daily_budget"]["enabled"] is True


def test_status_exposes_rate_limited_models_and_pool_state(app_client):
    app, client = app_client
    ctx = app.state.ctx
    key = ctx.box.decrypt(ctx.db.query_one("SELECT key_encrypted FROM api_keys")["key_encrypted"])
    account_id = int(
        ctx.providers.add_credential_account(
            "workbuddy", "rl", {"access_token": "t", "uid": "u-rl"}, uid="u-rl"
        )
    )
    ctx.pool.mark_failure(account_id, "workbuddy", "model-x", 400, "6004 该模型使用量超限")
    status = client.get("/v1/status", headers={"Authorization": "Bearer " + key}).json()
    models = {row["model"] for row in status["rate_limited_models"]}
    assert "model-x" in models
    assert "pool" in status and status["pool"]["max_in_flight"] >= 1
    state_file = ctx.settings.data_dir / "pool_state.json"
    ctx.pool.persist_state(force=True)
    assert state_file.exists()
    assert '"accounts"' in state_file.read_text(encoding="utf-8")


def _login_admin(client, password: str = "TestPass123!") -> str:
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("aiall_session") or client.cookies.get("traeapi_session")


def test_metrics_disabled_by_default_and_enabled_with_api_key(app_client):
    app, client = app_client
    ctx = app.state.ctx
    key = ctx.box.decrypt(ctx.db.query_one("SELECT key_encrypted FROM api_keys")["key_encrypted"])

    response = client.get("/metrics", headers={"Authorization": "Bearer " + key})
    assert response.status_code == 404

    ctx.db.set_setting("metrics_enabled", "1")
    unauth = client.get("/metrics")
    assert unauth.status_code == 401

    account = ctx.accounts._upsert(
        uid="1234567890123456",
        name="Metrics Account",
        auth_type="manual",
        access_token="test-access-token",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={"UserID": "1234567890123456", "ScreenName": "Metrics Account"},
        device_id="413174708280782",
        machine_id="1234567890123456",
    )
    assert account
    ctx.usage.record(
        request_id="req-metrics",
        api_key_id=1,
        api_key_name="default",
        account_id=1,
        account_name="Metrics Account",
        endpoint="/v1/chat/completions",
        model="metrics-model",
        stream=True,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        cached_tokens=30,
        cache_read_tokens=30,
        cache_miss_tokens=70,
        credits_delta=1.5,
        latency_ms=2000,
        ttft_ms=500,
        completion_ms=1500,
        status="ok",
        ip="127.0.0.1",
        provider_id="trae-cn",
    )
    ctx.db.execute(
        "INSERT INTO task_runs(task, started_at, finished_at, total, success, failed, skipped, all_failed) "
        "VALUES('trae_checkin', 100, 200, 3, 2, 1, 0, 0)"
    )
    first = client.get("/metrics", headers={"Authorization": "Bearer " + key})
    second = client.get("/metrics", headers={"Authorization": "Bearer " + key})
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/plain")
    assert first.text == second.text
    assert 'aiall_pool_accounts{realm="cn",status="total"} 1' in first.text
    assert 'aiall_task_last_run_success{task="trae_checkin"} 2' in first.text
    # 兼容别名默认开启
    assert 'aiall_pool_accounts{realm="cn",status="total"} 1' in first.text
    assert 'aiall_task_last_run_success{task="trae_checkin"} 2' in first.text
    # 兼容别名默认开启
    assert 'traeapi_pool_accounts{realm="cn",status="total"} 1' in first.text
    assert 'traeapi_pool_accounts{realm="global",status="total"} 0' in first.text
    assert 'traeapi_model_requests_total{model="metrics-model",status="success"} 1' in first.text
    assert 'traeapi_model_cache_tokens_total{model="metrics-model",type="hit"} 30' in first.text
    assert 'traeapi_model_cache_tokens_total{model="metrics-model",type="miss"} 70' in first.text
    assert 'traeapi_model_ttft_seconds_avg{model="metrics-model"} 0.5' in first.text
    assert 'wb2api_task_last_run_success{task="trae_checkin"} 2' in first.text
    assert "1234567890123456" not in first.text
    status = client.get("/v1/status", headers={"Authorization": "Bearer " + key}).json()
    assert status["pool_health"]["realms"]["cn"]["total"] == 1
    assert status["pool_health"]["waf_blocked"] is False


def test_admin_audit_records_authenticated_manual_action(app_client, tmp_path: Path):
    app, client = app_client
    ctx = app.state.ctx
    token = _login_admin(client)
    session = ctx.db.query_one(
        "SELECT csrf FROM admin_sessions WHERE token_hash=?", (token_hash(token),)
    )
    account = ctx.accounts._upsert(
        uid="1234567890123456",
        name="Audit Account",
        auth_type="manual",
        access_token="test-access-token",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={"UserID": "1234567890123456", "ScreenName": "Audit Account"},
        device_id="413174708280782",
        machine_id="1234567890123456",
    )
    audit_path = tmp_path / "admin_audit.log"
    ctx.db.set_setting("admin_audit_enabled", "1")
    ctx.db.set_setting("admin_audit_file", str(audit_path))
    response = client.post(
        f"/admin/accounts/{account['id']}/disable",
        data={"_csrf": session["csrf"], "reason": "测试摘除原因"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "account.disable"
    assert entry["object"] == "1234567890123456"
    assert entry["reason"] == "测试摘除原因"
    assert entry["ok"] is True
    assert entry["api_key_fp"]


def test_alerting_edge_trigger_and_hysteresis(app_client):
    app, client = app_client
    ctx = app.state.ctx
    ctx.db.set_setting("alerting_enabled", "1")
    ctx.db.set_setting("alerting_startup_grace_seconds", "0")
    ctx.db.set_setting("alerting_min_healthy_cn", "1")
    ctx.db.set_setting("alerting_min_healthy_global", "0")
    ctx.db.set_setting("alerting_for_ticks", "2")
    ctx.db.set_setting("alerting_clear_ticks", "2")
    ctx.db.set_setting("alerting_send_resolve", "1")
    events = []
    ctx.alerting._emit = lambda event, rule: events.append((event, rule["rule"]))

    ctx.alerting.evaluate_once()
    assert events == []
    ctx.alerting.evaluate_once()
    assert events == [("alert", "min_healthy_accounts")]
    ctx.alerting.evaluate_once()
    assert len(events) == 1

    ctx.accounts._upsert(
        uid="1234567890123456",
        name="Healthy Account",
        auth_type="manual",
        access_token="test-access-token",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={"UserID": "1234567890123456", "ScreenName": "Healthy Account"},
        device_id="413174708280782",
        machine_id="1234567890123456",
    )
    ctx.alerting.evaluate_once()
    assert len(events) == 1
    ctx.alerting.evaluate_once()
    assert events[-1] == ("resolve", "min_healthy_accounts")


def test_checkin_streak_and_today_status(app_client):
    from datetime import datetime, timedelta

    from app.services.platforms import _today

    app, client = app_client
    ctx = app.state.ctx
    today = _today()
    start = datetime.strptime(today, "%Y-%m-%d").date()
    for offset in range(3):
        day = (start - timedelta(days=offset)).isoformat()
        ctx.db.execute(
            "INSERT INTO platform_checkins(provider_id, account_id, account_name, day, status, credits, message, created_at) "
            "VALUES('lobster', 999, 'streak', ?, 'success', 10, 'ok', ?)",
            (day, int(datetime.now().timestamp())),
        )
    assert ctx.platforms.checkin_streak(today) == 3
    ctx.db.set_setting("last_checkin_run", today)
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/checkins")
    assert page.status_code == 200
    assert "今日是否执行" in page.text
    assert "已执行" in page.text
    assert "3 天" in page.text


def test_workbuddy_and_codebuddy_are_in_auto_checkin_scope(app_client, monkeypatch):
    import asyncio

    from app.providers.workbuddy import WorkBuddyProvider

    app, client = app_client
    ctx = app.state.ctx
    ctx.providers.add_credential_account(
        "workbuddy", "wb-auto", {"access_token": "wb-token", "uid": "wb-u"}, uid="wb-u"
    )
    ctx.providers.add_credential_account(
        "codebuddy", "cb-auto", {"access_token": "cb-token", "uid": "cb-u"}, uid="cb-u"
    )
    seen: list[str] = []

    async def fake_checkin(self, provider, account):
        seen.append(provider["id"])
        return {"status": "success", "credits": 10, "message": "ok"}

    monkeypatch.setattr(WorkBuddyProvider, "checkin", fake_checkin)
    results = asyncio.run(ctx.platforms.checkin_all())
    assert {item["status"] for item in results} == {"success"}
    assert set(seen) == {"workbuddy", "codebuddy"}


def test_workbuddy_quota_pack_details():
    import asyncio
    import time

    import httpx

    from app.providers.workbuddy import WorkBuddyProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/meter/get-user-resource"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "Response": {
                            "Data": {
                                "Accounts": [
                                    {
                                        "PackageName": "Bonus Pack",
                                        "CapacityRemain": 30,
                                        "CapacityUsed": 70,
                                        "CapacitySize": 100,
                                        "CycleCapacityRemain": 30,
                                        "CycleCapacityUsed": 70,
                                        "CycleCapacitySize": 100,
                                        "CycleEndTime": "2026-12-01 00:00:00",
                                    }
                                ]
                            }
                        }
                    },
                },
            )
        return httpx.Response(404)

    adapter = WorkBuddyProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.quota(
            {"region": "global", "config": {}},
            {"credential": {"access_token": "t", "expires_at": int(time.time()) + 3600, "uid": "u"}},
        )
    )
    pack = result["packs"][0]
    assert pack["name"] == "Bonus Pack"
    assert pack["remain"] == 30.0
    assert pack["total"] == 100.0
    assert pack["used"] == 70.0
    assert pack["expire_at"] > 0


def test_lobster_quota_pack_details():
    import asyncio
    import time

    import httpx

    from app.providers.lobster import LobsterProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/user/profile-summary"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "totalCreditsRemaining": 50,
                        "totalCreditsUsed": 10,
                        "totalCredits": 60,
                        "creditItems": [
                            {
                                "type": "campaign",
                                "label": "签到返还",
                                "creditsRemaining": 50,
                                "expiresAt": "2026-12-01T00:00:00Z",
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.quota(
            {"base_url": "https://lobster.test", "config": {}},
            {"credential": {"access_token": "t", "expires_at": int(time.time()) + 3600}},
        )
    )
    pack = result["packs"][0]
    assert pack["name"] == "签到返还"
    assert pack["remain"] == 50.0
    assert pack["expire_at"] > 0


def test_qoder_quota_pack_details():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v2/quota/usage"):
            return httpx.Response(
                200,
                json={
                    "userQuota": {"total": 100, "used": 20, "remaining": 80},
                    "addOnQuota": {"total": 50, "used": 10, "remaining": 40},
                    "expiresAt": 1796000000000,
                },
            )
        return httpx.Response(404)

    adapter = QoderProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.quota(
            {"region": "cn", "config": {"openapi": "https://openapi.qoder.com.cn"}},
            {"credential": {"access_token": "dt-test", "uid": "u"}},
        )
    )
    assert result["credits"] == 120.0
    assert result["used"] == 30.0
    assert result["total"] == 150.0
    packs = {pack["name"]: pack for pack in result["packs"]}
    assert packs["订阅额度"]["remain"] == 80.0
    assert packs["订阅额度"]["total"] == 100.0
    assert packs["附加/签到积分"]["remain"] == 40.0
    assert packs["附加/签到积分"]["used"] == 10.0


def test_platform_checkin_stores_valid_until_and_pack(app_client, monkeypatch):
    import asyncio
    import time

    from app.providers.qoder import QoderProvider

    app, client = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "qoder-cn", "qoder-validity", {"access_token": "dt-test", "uid": "qu"}, uid="qu"
    )
    valid_until = int(time.time()) + 30 * 86400

    async def fake_checkin(self, provider, account):
        return {
            "status": "success",
            "credits": 100,
            "valid_until": valid_until,
            "packs": [
                {
                    "name": "每日签到奖励",
                    "remain": 100,
                    "total": 100,
                    "used": 0,
                    "expire_at": valid_until,
                    "source": "qoder-checkin",
                }
            ],
            "message": "签到成功 +100 积分（30 天有效）",
        }

    monkeypatch.setattr(QoderProvider, "checkin", fake_checkin)
    result = asyncio.run(ctx.platforms.checkin(account_id))
    assert result["valid_until"] == valid_until
    row = ctx.db.query_one(
        "SELECT valid_until, credits FROM platform_checkins WHERE account_id=?", (account_id,)
    )
    assert row["valid_until"] == valid_until and row["credits"] == 100
    account = ctx.providers.get_account(account_id)
    assert any(pack["name"] == "每日签到奖励" for pack in account["packs"])


def test_platform_detail_shows_credit_pack_details(app_client):
    from app.credit_packs import dumps as packs_dumps

    app, client = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "lobster", "pack-detail", {"access_token": "t", "uid": "u"}, uid="u"
    )
    ctx.db.execute(
        "UPDATE provider_accounts SET credit_packs_json=?, credits_expiring=? WHERE id=?",
        (
            packs_dumps(
                [
                    {
                        "name": "签到返还",
                        "remain": 50,
                        "total": 50,
                        "used": 0,
                        "expire_at": 1796000000,
                        "source": "lobster",
                    }
                ]
            ),
            50,
            account_id,
        ),
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms/lobster")
    assert page.status_code == 200
    assert "积分明细" in page.text
    assert "签到返还" in page.text
    assert "1 个积分包" in page.text


def test_workbuddy_checkin_handles_already_and_unavailable():
    import asyncio
    import time

    import httpx

    from app.providers.workbuddy import WorkBuddyProvider

    def run(status_code: int, payload: dict) -> dict:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/daily-checkin"):
                return httpx.Response(status_code, json=payload)
            return httpx.Response(404)

        adapter = WorkBuddyProvider(
            Settings(data_dir=Path(tempfile.mkdtemp())),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return asyncio.run(
            adapter.checkin(
                {"id": "codebuddy", "region": "cn", "config": {}},
                {"credential": {"access_token": "t", "expires_at": int(time.time()) + 3600, "uid": "u"}},
            )
        )

    already = run(400, {"code": 10001, "msg": "今天已签到，请明天再来"})
    assert already["status"] == "already"
    unavailable = run(400, {"code": 10001, "msg": "签到活动未开启或已过期"})
    assert unavailable["status"] == "unavailable"


def test_request_prompt_modes_and_fingerprint_sanitization(app_client):
    from app.request_policy import apply_request_policy

    app, _ = app_client
    db = app.state.ctx.db
    payload = {
        "messages": [
            {"role": "system", "content": "client system"},
            {"role": "developer", "content": "client developer"},
            {"role": "user", "content": "hello"},
        ],
        "metadata": {"user_id": "u1", "device_id": "device-1"},
        "device_id": "top-device",
        "machine_id": "machine-1",
    }

    db.set_setting("prompt.mode", "passthrough")
    db.set_setting("privacy.sanitize_fingerprint", "1")
    passthrough = apply_request_policy(payload, db)
    assert passthrough["messages"] == payload["messages"]
    assert "device_id" not in passthrough
    assert "machine_id" not in passthrough
    assert "device_id" not in passthrough["metadata"]
    assert passthrough["metadata"]["user_id"] == "u1"

    db.set_setting("prompt.mode", "custom")
    custom = apply_request_policy(payload, db)
    assert [m["role"] for m in custom["messages"]] == ["system", "user"]
    assert custom["messages"][0]["content"] == "You are a helpful assistant."

    db.set_setting("prompt.mode", "append")
    appended = apply_request_policy(payload, db)
    assert [m["role"] for m in appended["messages"]] == [
        "system",
        "developer",
        "system",
        "user",
    ]
    assert appended["messages"][2]["content"] == "You are a helpful assistant."


def test_request_prompt_file(app_client, tmp_path):
    from app.request_policy import apply_request_policy

    app, _ = app_client
    db = app.state.ctx.db
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("gateway prompt from file", encoding="utf-8")
    db.set_setting("prompt.mode", "custom")
    db.set_setting("prompt.file", str(prompt_file))
    result = apply_request_policy(
        {"messages": [{"role": "system", "content": "client"}, {"role": "user", "content": "hi"}]},
        db,
    )
    assert result["messages"][0]["content"] == "gateway prompt from file"


def test_scheduler_jitter_and_ledger_file(app_client, tmp_path):
    from datetime import datetime

    app, _ = app_client
    ctx = app.state.ctx
    ctx.db.set_setting("schedule.jitter_minutes", "30")
    base = datetime(2026, 11, 12, 10, 0)
    first = ctx.scheduler._scheduled_at(base, "10:00", "checkin")
    second = ctx.scheduler._scheduled_at(base, "10:00", "checkin")
    assert first == second
    assert 0 <= int((first - base).total_seconds()) <= 30 * 60

    ledger = tmp_path / "task_ledger.jsonl"
    ctx.db.set_setting("schedule.ledger_file", str(ledger))
    started = 1794430000
    ctx.scheduler.record_task_run(
        "trae_checkin",
        started,
        [{"status": "success"}, {"status": "failed"}],
        source="manual",
    )
    rows = ctx.scheduler.task_ledger()
    assert rows[0]["task"] == "trae_checkin"
    assert rows[0]["source"] == "manual"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source"] == "manual"
    assert payload["success"] == 1 and payload["failed"] == 1


def test_manual_task_endpoint(app_client, monkeypatch):
    import re
    import time

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/checkins")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)

    async def fake_checkin_all():
        return [{"account": "Trae/test", "status": "success", "message": "ok"}]

    monkeypatch.setattr(ctx.accounts, "checkin_all", fake_checkin_all)
    response = client.post(
        "/admin/tasks/trae_checkin/run",
        data={"_csrf": csrf},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 202
    assert response.json()["task"] == "trae_checkin"
    for _ in range(20):
        row = ctx.db.query_one("SELECT source FROM task_runs WHERE task='trae_checkin'")
        if row and row["source"] == "manual":
            break
        time.sleep(0.05)
    assert row is not None and row["source"] == "manual"

    invalid = client.post(
        "/admin/tasks/not-implemented/run",
        data={"_csrf": csrf},
        headers={"Accept": "application/json"},
    )
    assert invalid.status_code == 400


def test_trae_global_entitlement_parser():
    from app.upstream.client import TraeClient

    detail = TraeClient._global_credit_detail_from_entitlements(
        {
            "is_dollar_usage_billing": True,
            "user_entitlement_pack_list": [
                {
                    "display_desc": "Free plan",
                    "entitlement_base_info": {
                        "end_time": 1796083199,
                        "quota": {"basic_usage_limit": 1, "bonus_usage_limit": 0},
                    },
                    "usage": {"basic_usage_amount": 0.25, "bonus_usage_amount": 0},
                }
            ],
        }
    )
    assert detail["remaining"] == 0.75
    assert detail["used"] == 0.25
    assert detail["total"] == 1.0
    assert detail["packs"][0]["name"] == "Free plan"
    assert detail["packs"][0]["unit"] == "USD"
    assert detail["packs"][0]["expire_at"] == 1796083199
    assert TraeClient._global_quota_host(
        {"raw_json": '{"AIRegion":"Singapore-Central"}', "oauth_host": "https://www.trae.ai"}
    ) == "https://api-sg-central.trae.ai"
    assert TraeClient._global_quota_host(
        {"raw_json": '{"AIRegion":"US"}', "oauth_host": "https://api-us-east.trae.ai"}
    ) == "https://api-us-east.trae.ai"


def test_deepseek_thinking_injection(app_client):
    from app.request_policy import apply_request_policy

    app, _ = app_client
    db = app.state.ctx.db
    db.set_setting("prompt.mode", "passthrough")
    result = apply_request_policy(
        {
            "model": "deepseek-v4.1-flash",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello", "reasoning": "trace"},
            ],
        },
        db,
    )
    assert result["thinking"]["type"] == "enabled"
    assert result["reasoning_effort"] == "high"
    assert result["messages"][1]["reasoning_content"] == "trace"


def test_workbuddy_conversation_headers():
    from app.providers.workbuddy import WorkBuddyProvider

    adapter = WorkBuddyProvider(Settings(data_dir=Path(tempfile.mkdtemp())), None)
    headers = adapter._chat_headers(
        {"region": "cn", "config": {}},
        {"access_token": "t", "uid": "u", "domain": "codebuddy.cn"},
        conversation_id="conv-1",
        request_id="a" * 32,
    )
    assert headers["X-Conversation-ID"] == "conv-1"
    assert headers["X-Conversation-Request-ID"] == "a" * 32
    assert headers["X-Root-Request-ID"] == "a" * 32
    assert headers["X-B3-TraceId"] == "a" * 32
    assert len(headers["X-B3-SpanId"]) == 16
    assert headers["X-B3-Sampled"] == "1"


def test_workbuddy_travel_endpoints(app_client):
    import asyncio
    import httpx

    from app.services.workbuddy_growth import WorkbuddyGrowthService

    app, _ = app_client
    ctx = app.state.ctx
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/activity/growth/buddy/info"):
            return httpx.Response(200, json={"code": 0, "data": {"buddy": {"id": 1}}})
        if request.url.path.endswith("/activity/growth/buddy/travel/status"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"state": "idle", "daily_limit_reached": False}},
            )
        if request.url.path.endswith("/activity/growth/buddy/travel/depart"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    class Adapter:
        @staticmethod
        def _identity(provider, cred):
            return {"Authorization": "Bearer t", "X-User-Id": "u"}

    service = WorkbuddyGrowthService(
        app.state.ctx.settings,
        ctx.db,
        ctx.providers,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        service._travel_one(
            {"region": "cn", "config": {}},
            {"credential": {"access_token": "t", "uid": "u"}},
            Adapter(),
            {"token": "t", "uid": "u"},
        )
    )
    assert result["status"] == "success"
    assert ("POST", "/activity/growth/buddy/travel/depart") in seen


def test_growth_task_manual_dispatch(app_client, monkeypatch):
    import asyncio

    app, _ = app_client
    ctx = app.state.ctx
    calls: list[str] = []

    class Growth:
        async def run_activity(self):
            calls.append("activity")
            return [{"account": "test", "status": "success", "message": "ok"}]

    monkeypatch.setattr(ctx.scheduler, "growth", Growth())
    asyncio.run(ctx.scheduler._run_growth_task("activity"))
    assert calls == ["activity"]
    row = ctx.db.query_one("SELECT * FROM task_runs WHERE task='activity'")
    assert row is not None and row["success"] == 1


def test_schedule_retry_state(app_client):
    from datetime import datetime

    app, _ = app_client
    ctx = app.state.ctx
    ctx.db.set_setting("schedule.retry_delay_minutes", "5")
    ctx.db.set_setting("schedule.retry_max_per_day", "1")
    started = int(datetime.now().timestamp())
    ctx.scheduler.record_task_run(
        "activity",
        started,
        [{"status": "error"}],
        source="schedule",
    )
    at = int(ctx.db.get_setting("retry_activity_at", "0"))
    assert at > started
    assert ctx.db.get_setting("retry_activity_count") == "1"

    ctx.db.set_setting("retry_activity_at", str(int(datetime.now().timestamp()) - 1))
    assert ctx.scheduler._retry_due("activity", datetime.now()) is True
    assert ctx.db.get_setting("retry_activity_at") == "0"

    ctx.scheduler.record_task_run(
        "activity",
        started,
        [{"status": "error"}],
        source="manual",
    )
    assert ctx.db.get_setting("retry_activity_at") == "0"


def test_workbuddy_activity_endpoints(app_client):
    import asyncio
    import httpx

    from app.services.workbuddy_growth import WorkbuddyGrowthService

    app, _ = app_client
    ctx = app.state.ctx
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v2/report":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path == "/activity/growth/streak":
            return httpx.Response(200, json={"code": 0, "data": {"streak": {"days": 7}, "redemption_status": {}}})
        if request.url.path == "/activity/growth/redeem":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path == "/activity/growth/lottery/chances":
            return httpx.Response(200, json={"code": 0, "data": {"balance": 1}})
        if request.url.path == "/activity/growth/lottery/draw":
            return httpx.Response(200, json={"code": 0, "data": {"prize_name": "6积分"}})
        if request.url.path.startswith("/billing/meter/claim-"):
            return httpx.Response(200, json={"code": 0, "data": {"credit": 1}})
        return httpx.Response(404)

    class Adapter:
        @staticmethod
        def _identity(provider, cred):
            return {"Authorization": "Bearer t", "X-User-Id": "u"}

        @staticmethod
        def origin(provider):
            return "https://www.codebuddy.cn"

    service = WorkbuddyGrowthService(
        ctx.settings,
        ctx.db,
        ctx.providers,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        service._activity_one(
            {"region": "cn", "config": {}},
            {"credential": {"access_token": "t", "uid": "u"}},
            Adapter(),
            {"token": "t", "uid": "u"},
        )
    )
    assert result["status"] == "success"
    assert seen.count("/v2/report") == 5
    assert "/activity/growth/streak" in seen
    assert "/activity/growth/lottery/draw" in seen
    assert "/billing/meter/claim-gift" in seen


def test_task_settings_page_lists_workbuddy_tasks(app_client):
    import re

    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/tasks")
    assert page.status_code == 200
    assert "WorkBuddy 任务" in page.text
    for label in ("活跃地图", "猫猫旅行", "开学季", "夜猫子"):
        assert label in page.text, label
    assert "/activity/growth/streak" in page.text
    assert "travel/depart" in page.text and "travel/claim" in page.text
    assert "school_open_day_2026.py" in page.text
    assert "--only black_cat" in page.text
    assert 'action="/admin/tasks/settings"' in page.text
    assert 'action="/admin/tasks/schedule"' in page.text
    for name in ("activity", "travel", "school", "cat"):
        assert f'action="/admin/tasks/{name}/run"' in page.text
    assert "/admin/tasks" in client.get("/admin").text

    settings_page = client.get("/admin/settings")
    for field in ("activity_enabled", "travel_enabled", "school_enabled", "cat_enabled"):
        assert f'name="{field}"' not in settings_page.text
    assert 'name="schedule_jitter_minutes"' not in settings_page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    assert csrf


def test_task_settings_save_switch_and_time(app_client):
    import re

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/tasks")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/admin/tasks/settings",
        data={
            "_csrf": csrf,
            "activity_enabled": "1",
            "activity_time": "10:30",
            "travel_enabled": "1",
            "travel_time": "09:15",
            "school_enabled": "0",
            "school_time": "12:45",
            "cat_enabled": "1",
            "cat_time": "01:20",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ctx.db.get_setting("schedule.activity_enabled") == "1"
    assert ctx.db.get_setting("schedule.activity_time") == "10:30"
    assert ctx.db.get_setting("schedule.travel_enabled") == "1"
    assert ctx.db.get_setting("schedule.travel_time") == "09:15"
    assert ctx.db.get_setting("schedule.school_enabled") == "0"
    assert ctx.db.get_setting("schedule.school_time") == "12:45"
    assert ctx.db.get_setting("schedule.cat_enabled") == "1"
    assert ctx.db.get_setting("schedule.cat_time") == "01:20"

    bad = client.post(
        "/admin/tasks/settings",
        data={"_csrf": csrf, "activity_enabled": "1", "activity_time": "25:99"},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "ok=0" in bad.headers["location"]
    assert ctx.db.get_setting("schedule.activity_time") == "10:30"


def test_task_schedule_settings_save(app_client):
    import re

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/tasks")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/admin/tasks/schedule",
        data={
            "_csrf": csrf,
            "schedule_jitter_minutes": "15",
            "schedule_retry_delay_minutes": "10",
            "schedule_retry_max_per_day": "2",
            "schedule_ledger_file": "./data/task_ledger.jsonl",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ctx.db.get_setting("schedule.jitter_minutes") == "15"
    assert ctx.db.get_setting("schedule.retry_delay_minutes") == "10"
    assert ctx.db.get_setting("schedule.retry_max_per_day") == "2"
    assert ctx.db.get_setting("schedule.ledger_file") == "./data/task_ledger.jsonl"


def test_task_run_form_redirects_back_to_tasks_page(app_client, monkeypatch):
    import re
    import time

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/tasks")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)

    calls: list[str] = []

    class Growth:
        async def run_activity(self):
            calls.append("activity")
            return [{"account": "test", "status": "success", "message": "ok"}]

    monkeypatch.setattr(ctx.scheduler, "growth", Growth())
    response = client.post(
        "/admin/tasks/activity/run",
        data={"_csrf": csrf, "redirect": "/admin/tasks"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/tasks?msg=")
    for _ in range(40):
        row = ctx.db.query_one("SELECT source, success FROM task_runs WHERE task='activity'")
        if row is not None:
            break
        time.sleep(0.05)
    assert row is not None and row["source"] == "manual" and row["success"] == 1
    assert calls == ["activity"]


def test_models_auto_refresh_default_on_and_once_per_day(app_client, monkeypatch):
    import asyncio
    from datetime import datetime

    app, _ = app_client
    ctx = app.state.ctx
    assert ctx.db.get_setting("models_auto_refresh") == "1"
    assert ctx.db.get_setting("models_refresh_time") == "10:10"
    assert ctx.db.get_setting("last_models_refresh", "") == ""

    calls = {"trae": 0, "providers": []}

    async def fake_all_models(force=False):
        calls["trae"] += 1
        assert force is True
        return [{"id": "m1"}, {"id": "m2"}]

    async def fake_sync(provider_id):
        calls["providers"].append(provider_id)
        if provider_id == "lobster":
            raise ValueError("该平台没有启用账号")
        return [{"id": f"{provider_id}-model"}]

    monkeypatch.setattr(ctx.accounts, "all_models", fake_all_models)
    monkeypatch.setattr(ctx.providers, "sync_models", fake_sync)
    ctx.db.set_setting("last_models_refresh", "")

    asyncio.run(ctx.scheduler._refresh_models(datetime(2026, 1, 1, 10, 9), "2026-01-01"))
    assert calls["trae"] == 0
    assert calls["providers"] == []

    asyncio.run(ctx.scheduler._refresh_models(datetime(2026, 1, 1, 10, 10), "2026-01-01"))
    assert calls["trae"] == 1
    assert calls["providers"], "平台模型应逐个同步"
    assert ctx.db.get_setting("last_models_refresh") == "2026-01-01"
    row = ctx.db.query_one("SELECT * FROM task_runs WHERE task='model_refresh'")
    assert row is not None and row["total"] == len(calls["providers"]) + 1
    assert row["success"] >= 1

    asyncio.run(ctx.scheduler._refresh_models(datetime(2026, 1, 1, 12, 0), "2026-01-01"))
    assert calls["trae"] == 1, "同一天只同步一次"

    ctx.db.set_setting("models_auto_refresh", "0")
    ctx.db.set_setting("last_models_refresh", "")
    asyncio.run(ctx.scheduler._refresh_models(datetime(2026, 1, 2, 12, 0), "2026-01-02"))
    assert calls["trae"] == 1, "开关关闭时不同步"


def test_settings_models_auto_refresh_switch_saves(app_client):
    import re

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert 'name="models_auto_refresh"' in page.text
    assert 'name="models_refresh_time"' in page.text
    assert "每天自动更新模型" in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)

    response = client.post(
        "/admin/settings",
        data={
            "_csrf": csrf,
            "default_model": "DeepSeek-V4-Flash",
            "models_auto_refresh": "0",
            "models_refresh_time": "08:30",
            "prompt_mode": "passthrough",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert ctx.db.get_setting("models_auto_refresh") == "0"
    assert ctx.db.get_setting("models_refresh_time") == "08:30"

    bad = client.post(
        "/admin/settings",
        data={
            "_csrf": csrf,
            "default_model": "DeepSeek-V4-Flash",
            "models_auto_refresh": "1",
            "models_refresh_time": "99:99",
            "prompt_mode": "passthrough",
        },
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "ok=0" in bad.headers["location"]
    assert ctx.db.get_setting("models_refresh_time") == "08:30"


def test_api_key_page_and_actions(app_client):
    import re

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/api-keys")
    assert page.status_code == 200
    assert "重新生成主 Key" in page.text
    assert "/v1/chat/completions" in page.text
    assert "/admin/api-keys" in page.text or "API Key" in page.text
    keys = ctx.api_keys.list_keys()
    assert keys and keys[0]["name"] in page.text

    settings_page = client.get("/admin/settings")
    assert "重新生成主 Key" not in settings_page.text
    assert 'action="/admin/settings/api-key"' not in settings_page.text
    assert "/admin/api-keys" in client.get("/admin").text

    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    added = client.post(
        "/admin/settings/api-key/add",
        data={"_csrf": csrf, "name": "chatbox"},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert added.headers["location"].startswith("/admin/api-keys?msg=")
    names = [k["name"] for k in ctx.api_keys.list_keys() if k["enabled"]]
    assert "chatbox" in names

    new_key = [k for k in ctx.api_keys.list_keys() if k["name"] == "chatbox"][0]
    revoked = client.post(
        f"/admin/settings/api-key/{new_key['id']}/revoke",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    assert revoked.headers["location"].startswith("/admin/api-keys?msg=")
    assert not [k for k in ctx.api_keys.list_keys() if k["id"] == new_key["id"] and k["enabled"]]

    regenerated = client.post(
        "/admin/settings/api-key",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert regenerated.status_code == 303
    assert regenerated.headers["location"].startswith("/admin/api-keys?msg=")
    enabled = [k for k in ctx.api_keys.list_keys() if k["enabled"]]
    assert len(enabled) == 1 and enabled[0]["name"] == "default"


def test_platform_card_trae_buttons_are_merged(app_client):
    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms")
    assert page.status_code == 200
    text = page.text
    assert text.count('href="/admin/platforms/trae/login?region=cn"') == 1
    assert text.count('href="/admin/platforms/trae/login?region=global"') == 1
    assert "添加 Trae 国内版账号" in text
    assert "Cookie 登录国际版" in text
    assert "获取 Token" not in text
    assert ">登录 Trae<" not in text


def test_platform_disable_reason_recorded_and_displayed(app_client):
    import re

    app, client = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "lobster", "reason-test", {"access_token": "t", "uid": "ru"}, uid="ru"
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    assert 'placeholder="摘除原因（可选）"' in page.text

    response = client.post(
        f"/admin/platforms/accounts/{account_id}/disable",
        data={"_csrf": csrf, "reason": "风控限流，先停一天"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = ctx.db.query_one(
        "SELECT manual_disabled, manual_reason FROM provider_accounts WHERE id=?", (account_id,)
    )
    assert row["manual_disabled"] == 1
    assert row["manual_reason"] == "风控限流，先停一天"

    page = client.get("/admin/platforms")
    assert "已摘除流量：风控限流，先停一天" in page.text

    restored = client.post(
        f"/admin/platforms/accounts/{account_id}/enable",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert restored.status_code == 303
    row = ctx.db.query_one(
        "SELECT manual_disabled, manual_reason FROM provider_accounts WHERE id=?", (account_id,)
    )
    assert row["manual_disabled"] == 0 and row["manual_reason"] == ""


def test_branding_and_compatibility_defaults(app_client):
    app, client = app_client
    ctx = app.state.ctx
    assert ctx.settings.app_name == "AIAll API"
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    assert client.cookies.get("aiall_session")
    page = client.get("/admin")
    assert page.status_code == 200
    assert "AIAll API" in page.text
    assert "· AIAll API" in page.text
    login_page = client.get("/admin/login")
    assert "AIAll API" in login_page.text
    # 旧前缀环境变量仍然生效，AIALL_ 优先
    from app.config import _env
    import os

    os.environ.pop("AIALL_HOST", None)
    os.environ["TRAEAPI_HOST"] = "1.2.3.4"
    try:
        assert _env("TRAEAPI_HOST", "") == "1.2.3.4"
        os.environ["AIALL_HOST"] = "5.6.7.8"
        assert _env("TRAEAPI_HOST", "") == "5.6.7.8"
    finally:
        os.environ.pop("AIALL_HOST", None)
        os.environ.pop("TRAEAPI_HOST", None)


def test_trae_oauth_failure_returns_friendly_error():
    import asyncio

    import httpx
    import pytest

    from app.config import Settings
    from app.upstream.client import TraeClient, TraeError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    client = TraeClient(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TraeError) as exc:
        asyncio.run(
            client.exchange_code(
                "code-1",
                "verifier-1",
                {"DeviceID": "d1"},
                oauth_host="https://api.trae.cn",
            )
        )
    message = str(exc.value)
    assert "授权码已失效或不被接受" in message
    assert "NameError" not in message


def test_manual_model_refresh_runs_even_when_switch_off(app_client, monkeypatch):
    import asyncio

    app, _ = app_client
    ctx = app.state.ctx
    calls: list[int] = []

    async def fake_all_models(force=False):
        calls.append(1)
        return [{"id": "m"}]

    async def fake_sync(provider_id):
        return [{"id": "p"}]

    monkeypatch.setattr(ctx.accounts, "all_models", fake_all_models)
    monkeypatch.setattr(ctx.providers, "sync_models", fake_sync)
    ctx.db.set_setting("models_auto_refresh", "0")

    async def run_manual():
        ctx.scheduler._manual_tasks.pop("model_refresh", None)
        await ctx.scheduler._run_manual("model_refresh")

    asyncio.run(run_manual())
    assert calls, "人工补跑不应受自动开关限制"
    row = ctx.db.query_one("SELECT * FROM task_runs WHERE task='model_refresh'")
    assert row is not None and row["success"] >= 1


def test_all_admin_pages_render(app_client):
    app, client = app_client
    ctx = app.state.ctx
    ctx.providers.add_credential_account(
        "lobster", "render-test", {"access_token": "t", "uid": "ru"}, uid="ru"
    )
    ctx.accounts._upsert(
        uid="9999999999999999",
        name="Render Trae",
        auth_type="manual",
        access_token="tok",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={"UserID": "9999999999999999", "ScreenName": "Render Trae"},
        device_id="1",
        machine_id="1",
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    paths = [
        "/admin",
        "/admin/platforms",
        "/admin/platforms/lobster",
        "/admin/platforms/trae-cn",
        "/admin/checkins",
        "/admin/tasks",
        "/admin/api-keys",
        "/admin/virtuals",
        "/admin/logs",
        "/admin/settings",
        "/admin/accounts",
        "/admin/models",
        "/admin/accounts/login-url",
        "/admin/trae/token?region=global",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:400])


def test_virtual_model_name_collision_check(app_client):
    import json
    import pytest

    app, _ = app_client
    ctx = app.state.ctx
    ctx.db.execute(
        "UPDATE providers SET models_json=? WHERE id='lobster'",
        (json.dumps([{"id": "lobster-flash", "display_name": "Lobster Flash"}]),),
    )
    with pytest.raises(ValueError) as exc:
        ctx.virtuals.create("lobster-flash")
    assert "已被模型映射使用" in str(exc.value)
    # display_name 只是展示名，不构成路由冲突，应允许创建（名称本身不能含空格）
    display_alias = ctx.virtuals.create("LobsterFlash")
    assert display_alias > 0
    group_id = ctx.virtuals.create("my-virtual")
    assert group_id > 0
    with pytest.raises(ValueError):
        ctx.virtuals.create("MY-VIRTUAL")


def test_trae_callback_requires_matching_state(app_client, monkeypatch):
    import asyncio

    import pytest

    app, _ = app_client
    ctx = app.state.ctx
    ctx.db.execute("DELETE FROM login_sessions")
    _, trace1, _, _ = ctx.accounts.start_login("", "cn")
    _, trace2, _, _ = ctx.accounts.start_login("", "cn")
    rows = {
        row["trace_id"]: row["state"]
        for row in ctx.db.query("SELECT trace_id, state FROM login_sessions WHERE status='pending'")
    }
    assert rows[trace1] and rows[trace2] and rows[trace1] != rows[trace2]

    with pytest.raises(ValueError) as exc:
        asyncio.run(
            ctx.accounts.add_from_callback("http://127.0.0.1:57321/authorize?AuthCode=code-1")
        )
    assert "state" in str(exc.value)

    with pytest.raises(ValueError):
        asyncio.run(
            ctx.accounts.add_from_callback(
                "http://127.0.0.1:57321/authorize?AuthCode=code-1&state=not-a-real-state"
            )
        )

    async def fake_exchange(code, verifier, device, **kwargs):
        return {"access_token": "at", "refresh_token": "rt", "expires_at": 0}

    async def fake_info(token, **kwargs):
        return {"UserID": "state-user", "ScreenName": "State User"}

    monkeypatch.setattr(ctx.client, "exchange_code", fake_exchange)
    monkeypatch.setattr(ctx.client, "get_user_info", fake_info)
    account = asyncio.run(
        ctx.accounts.add_from_callback(
            f"http://127.0.0.1:57321/authorize?AuthCode=code-1&state={rows[trace1]}"
        )
    )
    assert account["uid"] == "state-user"
    used = ctx.db.query_one("SELECT status, state FROM login_sessions WHERE trace_id=?", (trace1,))
    assert used["status"] == "ok" and used["state"] == ""
    other = ctx.db.query_one("SELECT status FROM login_sessions WHERE trace_id=?", (trace2,))
    assert other["status"] == "pending"


def test_trae_callback_rate_limited(app_client):
    from app.api import admin as admin_module

    app, client = app_client
    ctx = app.state.ctx
    ctx.db.execute("DELETE FROM login_sessions")
    admin_module.callback_limiter._attempts.clear()
    try:
        for _ in range(20):
            response = client.get("/admin/trae/callback?AuthCode=x")
            assert response.status_code == 400
        blocked = client.get("/admin/trae/callback?AuthCode=x")
        assert blocked.status_code == 429
        assert "尝试过于频繁" in blocked.text
    finally:
        admin_module.callback_limiter._attempts.clear()


def test_legacy_models_table_is_dropped(app_client):
    app, _ = app_client
    ctx = app.state.ctx
    row = ctx.db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='models'")
    assert row is None


def test_legacy_models_table_migrates_to_virtual(app_client):
    app, _ = app_client
    ctx = app.state.ctx
    db = ctx.db
    db.execute(
        "CREATE TABLE IF NOT EXISTS models ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, provider_id TEXT, "
        "upstream_model TEXT, enabled INTEGER)"
    )
    db.execute(
        "INSERT INTO models(name, provider_id, upstream_model, enabled) "
        "VALUES('legacy-alias','trae-cn','deepseek-v4.1-flash',1)"
    )
    db.init_schema()
    assert db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='models'") is None
    group = db.query_one("SELECT * FROM virtual_models WHERE name='legacy-alias'")
    assert group is not None and int(group["enabled"]) == 1
    member = db.query_one("SELECT * FROM virtual_members WHERE virtual_id=?", (group["id"],))
    assert member is not None
    assert member["provider_id"] == "trae-cn"
    assert member["model"] == "deepseek-v4.1-flash"


def test_task_ledger_details_on_tasks_and_logs_pages(app_client):
    app, client = app_client
    ctx = app.state.ctx
    ctx.scheduler.record_task_run(
        "activity",
        1794430000,
        [
            {"account": "wb-1", "status": "success", "message": "签到成功"},
            {"account": "cb-1", "status": "skipped", "message": "今日已派出"},
        ],
        source="manual",
    )
    ledger = ctx.scheduler.task_ledger(10)
    assert ledger and ledger[0]["task"] == "activity"
    assert ledger[0]["details"][0]["account"] == "wb-1"

    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    tasks_page = client.get("/admin/tasks")
    assert tasks_page.status_code == 200
    assert "wb-1" in tasks_page.text and "今日已派出" in tasks_page.text

    logs_page = client.get("/admin/logs")
    assert logs_page.status_code == 200
    assert "任务台账" in logs_page.text
    assert "wb-1" in logs_page.text
    assert "/admin/tasks" in logs_page.text


def test_deploy_restart_script_exists():
    from pathlib import Path

    script = Path("deploy/restart.sh")
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "scripts/preflight.py" in text
    assert "SKIP_PREFLIGHT" in text


def test_qoder_quota_parses_org_resource_package():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v2/quota/usage"):
            return httpx.Response(
                200,
                json={
                    "userType": "personal_pro",
                    "usageType": "credits",
                    "isQuotaExceeded": False,
                    "userQuota": {"total": 1000, "used": 200, "remaining": 800, "unit": "credits"},
                    "addOnQuota": {"total": 500, "used": 100, "remaining": 400, "unit": "credits"},
                    "orgResourcePackage": {"cap": 300, "used": 50, "remaining": 250, "unit": "credits"},
                },
            )
        return httpx.Response(404)

    adapter = QoderProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.quota(
            {"region": "global", "config": {"openapi": "https://openapi.qoder.sh"}},
            {"credential": {"access_token": "dt-test", "uid": "u"}},
        )
    )
    packs = {p["name"]: p for p in result["packs"]}
    assert packs["订阅额度"]["remain"] == 800.0
    assert packs["附加/签到积分"]["remain"] == 400.0
    assert packs["组织共享额度"]["remain"] == 250.0
    assert packs["组织共享额度"]["total"] == 300.0
    assert result["credits"] == 1450.0
    assert result["total"] == 1800.0


def test_qoder_zero_quota_reports_plan():
    import asyncio

    import httpx

    from app.providers.qoder import QoderProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v2/quota/usage"):
            return httpx.Response(
                200,
                json={
                    "userType": "personal_standard",
                    "usageType": "credits",
                    "isQuotaExceeded": True,
                    "userQuota": {"total": 0, "used": 0, "remaining": 0, "unit": "credits"},
                },
            )
        if request.url.path.endswith("/api/v2/user/plan"):
            return httpx.Response(
                200, json={"user_type": "personal_standard", "plan_tier_name": "Free", "is_paid_plan": False}
            )
        return httpx.Response(404)

    adapter = QoderProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.quota(
            {"region": "global", "config": {"openapi": "https://openapi.qoder.sh"}},
            {"credential": {"access_token": "dt-test", "uid": "u"}},
        )
    )
    assert result["credits"] == 0.0 and result["packs"] == []
    assert "Free 套餐无可用额度" in result["message"]


def test_platforms_page_shows_usd_unit_for_trae_global(app_client):
    from app.credit_packs import dumps as packs_dumps

    app, client = app_client
    ctx = app.state.ctx
    ctx.accounts._upsert(
        uid="8888888888888888",
        name="Global USD",
        auth_type="session",
        access_token="tok",
        refresh_token="",
        session_cookie="c",
        expires_at=0,
        raw={"UserID": "8888888888888888"},
        device_id="1",
        machine_id="1",
        region="global",
    )
    row = ctx.db.query_one("SELECT id FROM accounts WHERE uid='8888888888888888'")
    ctx.db.execute(
        "UPDATE accounts SET credits=1.0, credits_used=0.0, credits_total=1.0, credit_packs_json=? WHERE id=?",
        (
            packs_dumps(
                [
                    {
                        "name": "Free plan",
                        "remain": 1.0,
                        "total": 1.0,
                        "used": 0.0,
                        "expire_at": 1796083199,
                        "source": "trae-global",
                        "unit": "USD",
                    }
                ]
            ),
            row["id"],
        ),
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms")
    assert page.status_code == 200
    assert "$1" in page.text
    detail = client.get("/admin/platforms/trae-global")
    assert detail.status_code == 200
    assert "$1" in detail.text


def test_platform_detail_zero_quota_callout(app_client):
    app, client = app_client
    ctx = app.state.ctx
    account_id = ctx.providers.add_credential_account(
        "qoder-global", "zero-quota", {"access_token": "dt-x", "uid": "zq"}, uid="zq"
    )
    ctx.db.execute(
        "UPDATE provider_accounts SET credits=0, credits_used=0, credits_total=0, credit_packs_json='[]' WHERE id=?",
        (account_id,),
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms/qoder-global")
    assert page.status_code == 200
    assert "额度为 0" in page.text


def test_lobster_checkin_empty_slot_is_not_claimed():
    import asyncio
    import time

    import httpx

    from app.providers.lobster import LobsterProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/client-activities/slot"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"slotState": "empty", "activity": None}},
            )
        return httpx.Response(404)

    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(
        adapter.checkin(
            {"base_url": "https://lobster.test", "config": {}},
            {"credential": {"access_token": "t", "expires_at": int(time.time()) + 3600}},
        )
    )
    assert result["status"] == "unavailable"
    assert "没有可参与的签到活动" in result["message"]
    assert "已签到" in result["message"] and "不是已签到" in result["message"]


def test_lobster_client_version_from_update_api_and_cached():
    import asyncio

    import httpx

    from app.providers.lobster import LobsterProvider

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api-overmind.youdao.com":
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "value": {
                            "windowsX64": {
                                "url": "https://x/LobsterAI-Setup-x64-2026.11.6-official.exe"
                            }
                        }
                    }
                },
            )
        return httpx.Response(404)

    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    first = asyncio.run(adapter.client_version({"config": {}}))
    second = asyncio.run(adapter.client_version({"config": {}}))
    assert first == "2026.11.6"
    assert second == first
    assert len(calls) == 1, "版本解析应缓存"

    override = asyncio.run(adapter.client_version({"config": {"client_version": "2026.12.1"}}))
    assert override == "2026.12.1"


def test_lobster_checkin_uses_resolved_client_version():
    import asyncio
    import time

    import httpx

    from app.providers.lobster import LobsterProvider

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/client-activities/slot"):
            seen["params"] = dict(request.url.params)
            seen["ua"] = request.headers.get("user-agent")
            seen["client_version_header"] = request.headers.get("x-lobsterai-client-version")
            return httpx.Response(200, json={"code": 0, "data": {"slotState": "empty"}})
        return httpx.Response(404)

    adapter = LobsterProvider(
        Settings(data_dir=Path(tempfile.mkdtemp())),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(
        adapter.checkin(
            {"base_url": "https://lobster.test", "config": {"client_version": "2026.11.6"}},
            {"credential": {"access_token": "t", "expires_at": int(time.time()) + 3600}},
        )
    )
    assert seen["params"]["clientVersion"] == "2026.11.6"
    assert seen["ua"] == "LobsterAI/2026.11.6"
    assert seen["client_version_header"] == "2026.11.6"


def test_platform_test_button_on_card(app_client):
    app, client = app_client
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms")
    assert page.status_code == 200
    for pid in ("trae-cn", "trae-global", "workbuddy", "codebuddy", "lobster", "qoder-cn"):
        assert f'action="/admin/platforms/{pid}/test"' in page.text, pid
    assert page.text.count("js-test") >= 6


def test_platform_test_endpoint_ok_and_failure(app_client, monkeypatch):
    import re

    app, client = app_client
    ctx = app.state.ctx
    ctx.providers.add_credential_account(
        "lobster", "conn-test", {"access_token": "t", "uid": "cu"}, uid="cu"
    )
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    page = client.get("/admin/platforms")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)

    class FakeAdapter:
        async def list_models(self, provider, account):
            return [{"id": "m1"}, {"id": "m2"}]

    monkeypatch.setattr(ctx.providers, "adapter", lambda provider: FakeAdapter())
    ok = client.post(
        "/admin/platforms/lobster/test",
        data={"_csrf": csrf},
        headers={"Accept": "application/json"},
    )
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["ok"] is True
    assert isinstance(payload["latency_ms"], int)
    assert "2 个模型" in payload["message"]

    class BoomAdapter:
        async def list_models(self, provider, account):
            raise RuntimeError("upstream unreachable")

    monkeypatch.setattr(ctx.providers, "adapter", lambda provider: BoomAdapter())
    bad = client.post(
        "/admin/platforms/lobster/test",
        data={"_csrf": csrf},
        headers={"Accept": "application/json"},
    )
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    assert "upstream unreachable" in bad.json()["message"]

    no_csrf = client.post(
        "/admin/platforms/lobster/test", data={}, headers={"Accept": "application/json"}
    )
    assert no_csrf.status_code == 400


def test_platform_test_endpoint_trae_and_empty(app_client, monkeypatch):

    app, client = app_client
    ctx = app.state.ctx
    client.post(
        "/admin/login",
        data={"username": "admin", "password": "TestPass123!"},
        follow_redirects=False,
    )
    import re

    page = client.get("/admin/platforms")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)

    async def fake_models(account, force=False):
        return [{"id": "x"}, {"id": "y"}, {"id": "z"}]

    monkeypatch.setattr(ctx.accounts, "models", fake_models)
    ok = client.post(
        "/admin/platforms/trae-cn/test", data={"_csrf": csrf}, headers={"Accept": "application/json"}
    )
    assert ok.json()["ok"] is False  # 测试库没有 Trae 账号
    assert "没有已启用的 Trae 账号" in ok.json()["message"]

    ctx.accounts._upsert(
        uid="7777777777777777",
        name="Conn Trae",
        auth_type="manual",
        access_token="tok",
        refresh_token="",
        session_cookie="",
        expires_at=0,
        raw={"UserID": "7777777777777777"},
        device_id="1",
        machine_id="1",
        region="cn",
    )
    ok2 = client.post(
        "/admin/platforms/trae-cn/test", data={"_csrf": csrf}, headers={"Accept": "application/json"}
    )
    assert ok2.json()["ok"] is True
    assert "3 个模型" in ok2.json()["message"]


def test_virtual_alias_can_reuse_display_name(app_client):
    import json
    import pytest

    app, _ = app_client
    ctx = app.state.ctx
    ctx.db.execute(
        "UPDATE providers SET models_json=? WHERE id='workbuddy'",
        (
            json.dumps(
                [
                    {"id": "hy4free", "display_name": "Hy4"},
                    {"id": "hy4std", "display_name": "Hy4"},
                ]
            ),
        ),
    )
    group_id = ctx.virtuals.create("hy4")
    assert group_id > 0
    ctx.virtuals.add_member(group_id, "workbuddy", "hy4std", 1)
    group = ctx.virtuals.get_by_name("hy4")
    assert group is not None and group["members"][0]["model"] == "hy4std"
    with pytest.raises(ValueError):
        ctx.virtuals.create("HY4FREE")
