"""道鉴检定脚本共用工具（仅标准库）。

提供：HTTP 客户端（urllib）、统一 JSON 结果落盘、百分位统计、通用命令行参数、
环境探测与断言辅助。所有检定脚本只依赖本模块与 Python 标准库，不引入第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

CST = timezone(timedelta(hours=8))

# 本机默认现状（可用命令行参数覆盖）
DEFAULT_QUASHENG = "http://127.0.0.1:4010"
DEFAULT_SANXIAN = "http://127.0.0.1:14011"  # adb forward tcp:14011 -> tcp:4031
DEFAULT_ZHENTU = "http://127.0.0.1:4020"
DEFAULT_CHAT_MODEL = "qwen2.5:0.5b"
DEFAULT_EMBED_MODEL = "nomic-embed-text:latest"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str, default: str | None = None) -> str | None:
        """大小写不敏感地取响应头（urllib 会把 header 名转成小写）。"""
        target = name.lower()
        for k, v in self.headers.items():
            if k.lower() == target:
                return v
        return default


def request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    token: str = "",
    timeout: float = 120.0,
) -> HttpResponse:
    """发起一次 HTTP 请求；非 2xx 不抛异常，按 HttpResponse 返回，便于检定断言。"""
    hdrs = dict(headers or {})
    if payload is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if token:
        hdrs.setdefault("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return HttpResponse(resp.status, dict(resp.headers.items()), body, time.perf_counter() - t0)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return HttpResponse(exc.code, dict(exc.headers.items()), body, time.perf_counter() - t0)


def post(url: str, payload: dict[str, Any], **kw: Any) -> HttpResponse:
    return request("POST", url, payload=payload, **kw)


def get(url: str, **kw: Any) -> HttpResponse:
    return request("GET", url, **kw)


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for(predicate: Callable[[], bool], timeout: float, interval: float = 0.5) -> bool:
    """轮询直到 predicate 为真或超时；返回是否在时限内满足。"""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def metric_values(text: str, metric: str) -> list[float]:
    """从 Prometheus 文本中取某指标所有样本的值（忽略 HELP/TYPE 与标签内容）。"""
    out: list[float] = []
    for line in text.splitlines():
        if not line.startswith(metric) or line.startswith("#"):
            continue
        rest = line[len(metric):]
        if rest and not (rest[0].isspace() or rest[0] == "{"):
            continue
        try:
            out.append(float(line.rsplit(" ", 1)[1]))
        except (ValueError, IndexError):
            pass
    return out


def metric_sum(text: str, metric: str) -> float:
    return sum(metric_values(text, metric))


def url_host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname or "127.0.0.1", port


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def percentile(values: list[float], q: float) -> float | None:
    """最近秩百分位（q in [0,1]）；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p50": round(percentile(values, 0.5) or 0, 4),
        "p95": round(percentile(values, 0.95) or 0, 4),
        "max": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
    }


# --------------------------------------------------------------------------- #
# 断言与结果
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    id: str
    title: str
    method: str
    judgement: str
    data: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    measured: bool = True
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(passed), detail))

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def finish(self) -> dict[str, Any]:
        self.finished_at = now_iso()
        return {
            "id": self.id,
            "title": self.title,
            "method": self.method,
            "judgement": self.judgement,
            "measured": self.measured,
            "passed": self.passed if self.measured else None,
            "data": self.data,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "notes": self.notes,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class Report:
    suite: str
    env: dict[str, Any] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)

    def add(self, case: CaseResult) -> CaseResult:
        self.cases.append(case)
        return case

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "started_at": self.started_at,
            "finished_at": now_iso(),
            "env": self.env,
            "summary": {
                "total": len(self.cases),
                "passed": sum(1 for c in self.cases if c.measured and c.passed),
                "failed": sum(1 for c in self.cases if c.measured and not c.passed),
                "unmeasured": sum(1 for c in self.cases if not c.measured),
            },
            "cases": [c.finish() for c in self.cases],
        }

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return p


def write_json(path: str | Path, obj: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def exit_with(report: Report, path: str | Path, *, strict: bool = False) -> int:
    out = report.write(path)
    summary = report.to_dict()["summary"]
    print(f"\n[道鉴] 结果已写入: {out}")
    print(f"[道鉴] 汇总: {summary}")
    # 严格模式：任一已测用例失败则退出码非 0；未测（如环境缺失）默认不判失败
    if strict and summary["failed"]:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--base-url", default=DEFAULT_QUASHENG, help="被测节点 base url")
    p.add_argument("--token", default=os.environ.get("BIYOU_TOKEN", ""), help="Bearer token（可选）")
    p.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL, help="chat 模型名")
    p.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help="embedding 模型名")
    p.add_argument("--out", default="", help="结果 JSON 输出路径（默认 scripts/daojian/out/<suite>.json）")
    p.add_argument("--strict", action="store_true", help="已测用例失败时返回非 0 退出码")
    return p


def default_out(suite: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return str(root / "scripts" / "daojian" / "out" / f"{suite}.json")


def env_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "host": socket.gethostname(),
        "python": sys.version.split()[0],
        "captured_at": now_iso(),
    }
    # adb 设备（存在则记录）
    import shutil
    import subprocess

    if shutil.which("adb"):
        try:
            r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5)
            lines = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("List")]
            info["adb_devices"] = lines
        except Exception as exc:  # noqa: BLE001
            info["adb_devices"] = f"probe failed: {exc}"
    return info


def meta_from_args(args: argparse.Namespace) -> dict[str, Any]:
    e = env_info()
    e.update(
        {
            "base_url": args.base_url,
            "chat_model": args.chat_model,
            "embed_model": args.embed_model,
        }
    )
    return e
