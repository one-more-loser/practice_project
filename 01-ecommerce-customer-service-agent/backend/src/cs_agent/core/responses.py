"""无应用初始化副作用的公共成功包络。"""


def envelope(request, data):
    return {"contract_version": "API-CS-v0.1", "request_id": request.state.request_id, "data": data}
