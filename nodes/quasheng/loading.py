"""按需加载追踪：让 /v1/health 在模型加载窗口内返回 `loading`。

Ollama 在收到请求后才加载模型（数秒级）。仅凭 `/api/ps` 无法稳定捕捉这段
窗口——请求发出到第一个 chunk 之间模型可能尚未出现在 ps 里。这里用
"请求已发出但模型尚未确认加载" 的 pending 集合来标记 loading，并在
首个 chunk / 成功响应 / 上游错误时清理。
"""
from __future__ import annotations

import time
from collections.abc import Iterable

# 与 `_WARMUP` 失败后的标签匹配共用：Ollama 会把省略 tag 的名字补成 :latest，
# 因此比较时按 `:` 之前的部分归一（也兼容 `name:tag` 的常规写法）。
def model_key(name: str | None) -> str:
    if not name:
        return ""
    return name.rsplit(":", 1)[0] if ":" in name else name


class LoadingTracker:
    def __init__(self, ttl: float = 300.0, clock=time.monotonic) -> None:
        self._ttl = max(1.0, float(ttl))
        self._clock = clock
        self._loaded: set[str] = set()
        self._pending: dict[str, float] = {}

    def set_loaded(self, names: Iterable[str]) -> None:
        """启动时用 /api/ps 建立初始已加载集合。"""
        self._loaded = {model_key(n) for n in names if n}
        self._drop_loaded_pending()

    def note_request(self, model: str) -> None:
        """请求已进入节点、但模型尚未确认加载时登记 pending。"""
        key = model_key(model)
        if key and key not in self._loaded:
            self._pending.setdefault(key, self._clock())

    def mark_loaded(self, model: str) -> None:
        key = model_key(model)
        self._loaded.add(key)
        self._pending.pop(key, None)

    def mark_failed(self, model: str) -> None:
        self._pending.pop(model_key(model), None)

    def sync(self, loaded: Iterable[str]) -> None:
        """用最新 /api/ps 快照覆盖已加载集合，并清掉已加载的 pending。"""
        self._loaded = {model_key(n) for n in loaded if n}
        self._drop_loaded_pending()

    def _drop_loaded_pending(self) -> None:
        for key in list(self._pending):
            if key in self._loaded:
                self._pending.pop(key, None)

    def pending_models(self) -> list[str]:
        """未超过 TTL 的 pending 模型（超时视为请求已失败）。"""
        now = self._clock()
        for key, started in list(self._pending.items()):
            if now - started >= self._ttl:
                self._pending.pop(key, None)
        return list(self._pending)

    def is_loading(self) -> bool:
        return bool(self.pending_models())
