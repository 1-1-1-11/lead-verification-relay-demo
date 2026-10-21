"""python -m relay_demo：按环境变量监听本地端口。"""

from __future__ import annotations

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run("relay_demo.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
