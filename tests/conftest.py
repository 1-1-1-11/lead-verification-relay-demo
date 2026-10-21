"""每个 API 测试都从种子数据和空日志开始。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay_demo.app import app, reset_runtime


@pytest.fixture()
def client():
    reset_runtime()
    yield TestClient(app)
    reset_runtime()
