"""BaseLoader 抽象 + LoadResult.

Tech design v3.6 §3.1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bowhead_core.kb.document import KbDocument

LayerName = Literal["L0", "L1", "L2"]


@dataclass
class LoadResult:
    """Loader 输出: extracted KbDocument list + metadata about parse layer used."""

    documents: list[KbDocument]
    layer_used: LayerName
    duration_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseLoader(ABC):
    """Loader interface. 每 mime-type 一个实现, dispatch 走 mime → loader.

    Loader 内部决定 L0→L1→L2 fallback. 输出 KbDocument list (pre-splitter).
    """

    @abstractmethod
    async def load(self, file_path: str, *, use_llm: bool = False) -> LoadResult:
        """Load file → KbDocument list.

        Args:
            file_path: 文件本地路径 (artifact_store.fetch 后的)
            use_llm: marker --use_llm flag (PDF only V1, qwen3-vl-plus 走 P1-T2)
        """
