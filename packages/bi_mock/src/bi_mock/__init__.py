"""BI Mock Server.

实现 TD §2.1 定义的 13 个 BI API 端点的 mock，供开发期与单测使用。

启动：
    uv run --package bi-mock uvicorn bi_mock.main:app --port 8081

Fixture 数据在 ``packages/bi_mock/fixtures/`` 下，可按需扩展。
"""

__version__ = "0.1.0"
