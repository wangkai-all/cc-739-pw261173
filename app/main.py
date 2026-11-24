"""启动入口：python -m app.main [--host 127.0.0.1] [--port 8080] [--data data]"""
from __future__ import annotations

import argparse

from .server import make_server


def main():
    ap = argparse.ArgumentParser(description="采购组合报价分析服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", default="data", help="情景数据目录")
    args = ap.parse_args()
    httpd = make_server(args.host, args.port, args.data)
    print(f"组合报价分析服务已启动: http://{args.host}:{args.port}/")
    print(f"数据目录: {args.data}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
