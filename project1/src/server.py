"""CLI 入口：python server.py [serve|seed]。"""
from __future__ import annotations

import sys


def usage() -> None:
    print("Usage: python server.py [serve|seed]")
    print("  seed  - 初始化数据库（创建表、桩、测试用户）")
    print("  serve - 启动 HTTP 服务（默认 http://127.0.0.1:8000）")
    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1]
    if cmd == "seed":
        from app.seed import seed
        seed()
        print("OK: database seeded.")
        return
    if cmd == "serve":
        import uvicorn
        host = "127.0.0.1"
        port = 8000
        if len(sys.argv) >= 4:
            host = sys.argv[2]
            port = int(sys.argv[3])
        uvicorn.run("app.main:app", host=host, port=port, reload=False, log_level="info")
        return
    usage()


if __name__ == "__main__":
    main()
