class AppError(Exception):
    """只能携带已审核的固定消息，不携带请求原文或数据库异常。"""

    def __init__(self, code: str, status: int, message: str):
        self.code, self.status, self.message = code, status, message
        super().__init__(code)


def not_found():
    return AppError("not_found", 404, "资源不存在或不可访问")
