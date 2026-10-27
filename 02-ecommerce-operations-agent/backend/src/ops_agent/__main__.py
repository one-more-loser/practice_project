"""显式区分本地演示与01真实接入；不自动迁移其他项目数据库。"""

import argparse
import os
from pathlib import Path

import uvicorn

from ops_agent.app import create_app
from ops_agent.db import Database


def main():
    parser = argparse.ArgumentParser(description="02经营Agent服务")
    parser.add_argument("command", choices=("serve", "init-db"))
    parser.add_argument("--demo", action="store_true", help="仅本机合成店铺演示")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8120)
    parser.add_argument("--origin", default="http://127.0.0.1:5174")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    mode = "demo" if args.demo else "integration"
    if args.demo:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            parser.error("演示管理员身份只允许监听本机地址")
        (root / ".runtime").mkdir(exist_ok=True)
        database_url = "sqlite+pysqlite:///" + (root / ".runtime" / "demo.db").as_posix()
    else:
        database_url = os.environ.get("OPS_DATABASE_URL")
        if not database_url:
            parser.error("真实接入须设置OPS_DATABASE_URL，使用ops02_开头专用PostgreSQL库")
    if args.command == "init-db":
        database = Database(database_url, mode)
        database.initialize()
        database.close()
        print("02独立数据库结构已初始化；未操作01")
        return
    app = create_app(database_url=database_url, mode=mode, origin=args.origin,
        identity_base_url=os.environ.get("OPS_IDENTITY_BASE_URL"),
        trade_base_url=os.environ.get("OPS_TRADE_BASE_URL"),
        service_token=os.environ.get("OPS_ACTIVITY_SERVICE_TOKEN"),
        service_stores=tuple(s for s in os.environ.get("OPS_SERVICE_STORES", "").split(",") if s))
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
