"""受控02客户端：固定配置地址、专用Bearer、3秒、有界响应、拒绝重定向。"""

from urllib.parse import urlsplit

import httpx

from cs_agent.core.errors import AppError


def service_config(settings, store_id):
    token = getattr(settings, "activity_service_token", None)
    token = token.get_secret_value() if hasattr(token, "get_secret_value") else token
    stores = getattr(settings, "activity_service_stores", ())
    base = str(getattr(settings, "activity_ops_base_url", "") or "").rstrip("/")
    url = urlsplit(base)
    if (
        not token
        or store_id not in stores
        or url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise AppError("activity_service_unavailable", 503, "活动服务尚未配置")
    return base, token


class OperationsClient:
    def __init__(self, settings, store_id, *, transport=None):
        self.base, self.token = service_config(settings, store_id)
        self.transport = transport

    def request(self, method, path, *, body=None, params=None):
        try:
            with httpx.Client(
                timeout=3, follow_redirects=False, transport=self.transport
            ) as client:
                with client.stream(
                    method,
                    self.base + path,
                    json=body,
                    params=params,
                    headers={"Authorization": "Bearer " + self.token},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise AppError("activity_remote_unavailable", 503, "活动服务结果未确认")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise AppError("activity_remote_contract", 503, "活动服务结果未确认")
                    import json

                    value = json.loads(raw)
                    data = value.get("data") if isinstance(value, dict) else None
                    if not isinstance(data, dict):
                        raise AppError("activity_remote_contract", 503, "活动服务结果未确认")
                    return data
        except (httpx.HTTPError, ValueError, TypeError):
            raise AppError("activity_remote_unavailable", 503, "活动服务结果未确认") from None

    def revision(self, plan_id, store_id):
        from urllib.parse import quote

        return self.request(
            "GET",
            "/api/ops/v1/internal/plan-revisions/" + quote(plan_id, safe=""),
            params={"store_id": store_id},
        )

    def grant(self, body):
        return self.request("POST", "/api/ops/v1/internal/execution-grants", body=body)
