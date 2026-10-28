"""KB 摄入的 OOXML 解压炸弹预检（untrusted_parse 簇 B 接线）。

背景：docx/xlsx/pptx 是不受信 zip。实测 1.5MB 的 pptx 可撑到 3GB RSS(2030×)，
docx 同样暴露。worker 跑在网关进程内 → OOM = 全租户下线。此前 KB 摄入路径
(`_load_with_retry`)**完全没接** `untrusted_parse` 的预检，而 glossary/附件/
template_clone 三条路都接了(tech-design-m2.md:11 把 KB 簇 B 显式登记为 defer)。
"""

from __future__ import annotations

import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.worker import KbWorker


def _bomb_pptx(path, ratio_mb: int = 400) -> str:
    """造真炸弹：高压缩比 zip（全 0 字节压缩率极高，磁盘上很小）。
    不会真吃内存 —— 预检读的是 zip 中央目录的声明大小，不解压。
    """
    p = str(path / "bomb.pptx")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", b"<Types/>")
        z.writestr("ppt/media/huge.bin", b"\0" * (ratio_mb * 1024 * 1024))
    return p


def _benign_pptx(path) -> str:
    p = str(path / "ok.pptx")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", b"<Types/>")
        z.writestr("ppt/slides/slide1.xml", b"<sld>hello</sld>" * 50)
    return p


def _worker_and_loader():
    worker = KbWorker(files_store=MagicMock(), store=MagicMock(), embedder=MagicMock())
    loader = MagicMock()
    loader.load = AsyncMock(return_value=SimpleNamespace(documents=[], layer_used="L0", duration_ms=1, metadata={}))
    return worker, loader


@pytest.mark.asyncio
async def test_zip_bomb_rejected_before_loader_runs(tmp_path) -> None:
    """⚠ 回归闸：炸弹必须在 loader.load **之前**被拦 —— 预检读中央目录不解压，
    放到 load 之后就等于让 python-pptx 先把几个 G 吃进内存。
    """
    bomb = _bomb_pptx(tmp_path)
    worker, loader = _worker_and_loader()
    file = SimpleNamespace(artifact_id=bomb, filename="bomb.pptx", use_llm_marker=False)

    result = await worker._load_with_retry(file, "kbf_bomb", loader)

    loader.load.assert_not_awaited(), "炸弹居然进了 loader"
    assert result.metadata["parse_failed"] is True
    assert result.metadata["reason"]  # Nonempty user-facing reason.
    assert not result.metadata.get("transient")  # Must not retry.


@pytest.mark.asyncio
async def test_benign_ooxml_passes_through(tmp_path) -> None:
    """正常 pptx 不能被误伤（预检只看压缩比）。"""
    ok = _benign_pptx(tmp_path)
    worker, loader = _worker_and_loader()
    file = SimpleNamespace(artifact_id=ok, filename="ok.pptx", use_llm_marker=False)

    await worker._load_with_retry(file, "kbf_ok", loader)

    loader.load.assert_awaited_once()  # Normal file still reaches loader.


@pytest.mark.asyncio
async def test_non_ooxml_skips_precheck(tmp_path) -> None:
    """pdf/md/图片 不是 zip → 预检 no-op，零成本、不误伤。"""
    p = tmp_path / "a.md"
    p.write_text("# hi", encoding="utf-8")
    worker, loader = _worker_and_loader()
    file = SimpleNamespace(artifact_id=str(p), filename="a.md", use_llm_marker=False)

    await worker._load_with_retry(file, "kbf_md", loader)

    loader.load.assert_awaited_once()
