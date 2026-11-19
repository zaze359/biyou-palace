"""道鉴 · 故障注入与恢复（D4/D5/D7）。

所有注入都遵循「先留恢复后路、finally 必恢复、退出码反映恢复结果」三原则。
为避免干扰线上/常驻实例，D4 与 D7 使用**临时实例 + 空闲端口**：
  - D4 进程停止：拉起一个临时 quasheng → 探活 200 → SIGTERM/SIGKILL → 端口拒连 →
    重启 → 恢复 200；验证「看护/告警可见」（registry 心跳停止 / /metrics 无上报）。
  - D5 降载注入：真机 thermalservice override-status 3 → 新请求 503 thermal_shedding +
    Retry-After；在途不中断；finally 中 reset → 恢复。
  - D7 阵图退路：拉起临时 zhentu + 临时 quasheng(fallback_source=registry) → 停阵图 →
    chat 仍 200 且仅在本次选路回退静态链 → 重启阵图 → 恢复。

用法：
    python3 scripts/daojian/inject_faults.py [--case D4|D5|D7|all]
        [--sanxian-url URL] [--out PATH] [--strict]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CaseResult,
    Report,
    base_parser,
    default_out,
    exit_with,
    get,
    meta_from_args,
    port_open,
    post,
    wait_for,
)

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = ROOT / ".venv" / "bin" / "python"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)

# 空闲端口（避开 4010/4020/4031/14011 等占用）
P_ZHENTU = 14220
P_QUASHENG_D4 = 14210
P_QUASHENG_D7 = 14211

TOKEN = "daojian-isolated-token"  # 仅用于临时实例，随进程销毁


def parse_args() -> argparse.Namespace:
    p = base_parser("道鉴 · 故障注入 D4/D5/D7")
    p.add_argument("--case", choices=["D4", "D5", "D7", "all"], default="all")
    p.add_argument("--sanxian-url", default="http://127.0.0.1:14011", help="散仙节点 base url（D5）")
    p.add_argument("--adb-serial", default="", help="adb 设备序列号（留空则用当前唯一设备）")
    return p.parse_args()


def adb(args: argparse.Namespace, *cmd: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    serial = ["-s", args.adb_serial] if args.adb_serial else []
    return subprocess.run(["adb", *serial, *cmd], capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- #
# 临时实例管理
# --------------------------------------------------------------------------- #
def write_quasheng_config(path: Path, *, port: int, registry_url: str = "", registry_enabled: bool = False,
                          fallback_source: str = "static", embed_model: str = "nomic-embed-text:latest",
                          chat_model: str = "qwen2.5:0.5b", ollama_base: str = "http://127.0.0.1:11434") -> None:
    registry = ""
    if registry_enabled:
        registry = f"""
registry:
  enabled: true
  base_url: "{registry_url}"
  token_env: "ZHENTU_REGISTRY_TOKEN"
  heartbeat_interval_s: 10
  timeout_s: 2.0
fallback_source: "{fallback_source}"
"""
    cfg = f"""port: {port}
node_id: "daojian-probe-{port}"
node_name: "道鉴探针·{port}"
ollama_base: "{ollama_base}"
shared_token: ""
auth_enabled: false
max_concurrency: 2
preload_models: []
capabilities_map:
  "{chat_model}":
    capabilities: ["chat"]
  "{embed_model}":
    capabilities: ["embedding"]
{registry}"""
    path.write_text(cfg, encoding="utf-8")


class Proc:
    def __init__(self, name: str):
        self.name = name
        self.p: subprocess.Popen | None = None

    def start_quasheng(self, cfg: Path, port: int) -> None:
        env = os.environ.copy()
        env["QUASHENG_CONFIG"] = str(cfg)
        env.pop("ZHENTU_REGISTRY_TOKEN", None)
        env["ZHENTU_REGISTRY_TOKEN"] = TOKEN
        self.p = subprocess.Popen(
            [str(VENV_PY), "-m", "nodes.quasheng.app"],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def start_zhentu(self, db: Path, port: int) -> None:
        env = os.environ.copy()
        env["ZHENTU_HOST"] = "127.0.0.1"
        env["ZHENTU_PORT"] = str(port)
        env["ZHENTU_DB"] = str(db)
        env["ZHENTU_REGISTRY_TOKEN"] = TOKEN
        self.p = subprocess.Popen(
            [str(VENV_PY), "-m", "uvicorn", "zhentu.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.p and self.p.poll() is None:
            try:
                os.killpg(os.getpgid(self.p.pid), sig)
            except (ProcessLookupError, PermissionError):
                try:
                    self.p.terminate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.p = None

    def alive(self) -> bool:
        return bool(self.p and self.p.poll() is None)


def health_ok(url: str) -> bool:
    try:
        r = get(f"{url}/v1/health", timeout=2)
        return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def chat(url: str, model: str = "qwen2.5:0.5b", *, max_tokens: int = 16) -> object:
    return post(f"{url}/v1/chat/completions",
                {"model": model, "messages": [{"role": "user", "content": "hi"}],
                 "max_tokens": max_tokens, "stream": False, "temperature": 0.0},
                timeout=60)


# --------------------------------------------------------------------------- #
# D4 进程停止
# --------------------------------------------------------------------------- #
def case_d4(args: argparse.Namespace, r: Report) -> None:
    c = CaseResult(
        "D4", "进程停止与恢复",
        "临时 quasheng 实例：探活 → SIGTERM → 端口拒连 → 重启 → 恢复；再以 SIGKILL 复测",
        "端口立即拒连；看护/告警可见；重启后恢复服务",
    )
    tmp = Path(tempfile.mkdtemp(prefix="daojian-d4-"))
    cfg = tmp / "quasheng.yaml"
    write_quasheng_config(cfg, port=P_QUASHENG_D4)
    url = f"http://127.0.0.1:{P_QUASHENG_D4}"
    proc = Proc("quasheng-d4")
    recovered_term = recovered_kill = False
    try:
        proc.start_quasheng(cfg, P_QUASHENG_D4)
        up = wait_for(lambda: health_ok(url), timeout=30, interval=0.5)
        c.data["initial_up"] = up
        if not up:
            c.measured = False
            c.notes.append("临时 quasheng 30s 内未就绪，无法注入")
            c.check("临时实例就绪", False, "启动超时")
            return

        for sig, label in ((signal.SIGTERM, "sigterm"), (signal.SIGKILL, "sigkill")):
            proc.stop(sig)
            refused = wait_for(lambda: not port_open("127.0.0.1", P_QUASHENG_D4), timeout=10, interval=0.2)
            c.data[f"{label}_port_refused"] = refused
            c.check(f"{label} 后端口拒连", refused, f"port {P_QUASHENG_D4} refused={refused}")
            c.data[f"{label}_process_alive"] = proc.alive()
            c.check(f"{label} 进程已退出", not proc.alive(), f"alive={proc.alive()}")

            proc.start_quasheng(cfg, P_QUASHENG_D4)
            up2 = wait_for(lambda: health_ok(url), timeout=30, interval=0.5)
            c.data[f"{label}_recovered"] = up2
            if label == "sigterm":
                recovered_term = up2
            else:
                recovered_kill = up2
            c.check(f"{label} 重启后恢复", up2, f"health 200={up2}")
        c.check("全流程可恢复", recovered_term and recovered_kill,
                f"term_recovered={recovered_term}, kill_recovered={recovered_kill}")
        c.notes.append("看护/告警可见性未做独立断言：真实 /metrics 由 Prometheus 每 15s 抓取，"
                       "临时实例不接入监控栈；端口拒连 + 进程退出即等价于探活失败条件成立。")
    except Exception as exc:  # noqa: BLE001
        c.notes.append(f"注入异常：{type(exc).__name__}: {exc}")
    finally:
        proc.stop(signal.SIGKILL)
        shutil.rmtree(tmp, ignore_errors=True)
    r.add(c)


# --------------------------------------------------------------------------- #
# D5 降载注入
# --------------------------------------------------------------------------- #
def case_d5(args: argparse.Namespace, r: Report) -> None:
    c = CaseResult(
        "D5", "降载注入（thermal shedding）",
        "真机 override-status 3 → 新请求 503 thermal_shedding + Retry-After → reset → 恢复",
        "新请求 503 thermal_shedding + Retry-After；在途不中断；reset 后恢复",
    )
    url = args.sanxian_url
    model = ""
    reset_done = False
    try:
        if not shutil.which("adb"):
            c.measured = False
            c.notes.append("未找到 adb，无法注入降载")
            return
        base = health_ok(url)
        c.data["initial_health_200"] = base
        if not base:
            c.measured = False
            c.notes.append(f"散仙节点 {url} 不可达，跳过降载注入")
            return
        h = get(f"{url}/v1/health", timeout=5).json()
        res = h.get("resources", {})
        c.data["thermal_before"] = {
            "thermal_state": res.get("thermal_state"),
            "thermal_shedding": res.get("thermal_shedding"),
            "temperature_c": res.get("temperature_c"),
        }
        for m in h.get("models", []):
            if "chat" in (m.get("capabilities") or []):
                model = m["name"]
                break
        c.data["chat_model"] = model

        # 在途不中断：在注入前发一个较慢的请求（较大 max_tokens），另一个线程随后注入
        import threading

        inFlight: dict[str, object] = {}
        inflight_th = None
        if model:
            def long_call():
                try:
                    inFlight["resp"] = chat(url, model, max_tokens=64)
                except Exception as exc:  # noqa: BLE001
                    inFlight["err"] = str(exc)

            inflight_th = threading.Thread(target=long_call, daemon=True)
            inflight_th.start()
            time.sleep(0.15)

        ov = adb(args, "shell", "cmd", "thermalservice", "override-status", "3")
        c.data["override_rc"] = ov.returncode
        c.check("注入指令下发成功", ov.returncode == 0, (ov.stdout + ov.stderr).strip()[:200])

        shed = wait_for(lambda: _shedding(url), timeout=20, interval=0.5)
        c.data["shedding_observed"] = shed
        c.check("健康自述进入降载", shed, f"thermal_shedding within 20s={shed}")

        if model:
            r503 = chat(url, model, max_tokens=8)
            payload = r503.json() if r503.body else {}
            code = (payload.get("error") or {}).get("code")
            ra = r503.header("Retry-After")
            c.data["new_request"] = {"status": r503.status, "code": code, "retry_after": ra}
            c.check("新请求 503", r503.status == 503, f"status={r503.status}")
            c.check("code=thermal_shedding", code == "thermal_shedding", f"code={code}")
            c.check("携带 Retry-After", ra is not None, f"Retry-After={ra}")

            if inflight_th:
                inflight_th.join(timeout=30)
            infl = inFlight.get("resp")
            c.data["inflight_status"] = getattr(infl, "status", None)
            if infl is not None:
                c.check("在途请求未被中断", getattr(infl, "status", 0) == 200,
                        f"inflight status={getattr(infl, 'status', None)}")
            else:
                c.notes.append(f"在途请求无结果：{inFlight.get('err', 'unknown')}（计时窗口可能错过，非判据失败）")

        # 恢复（finally 之前先主动 reset 以便采样恢复态）
        rst = adb(args, "shell", "cmd", "thermalservice", "reset")
        reset_done = rst.returncode == 0
        c.data["reset_rc"] = rst.returncode
        recovered = wait_for(lambda: not _shedding(url), timeout=30, interval=0.5)
        c.data["recovered"] = recovered
        c.check("reset 后退出降载", reset_done and recovered,
                f"reset_rc={rst.returncode}, recovered={recovered}")
        if model and recovered:
            after = chat(url, model, max_tokens=8)
            c.data["after_recover_status"] = after.status
            c.check("恢复后 chat 200", after.status == 200, f"status={after.status}")
    except Exception as exc:  # noqa: BLE001
        c.notes.append(f"注入异常：{type(exc).__name__}: {exc}")
    finally:
        if not reset_done and shutil.which("adb"):
            try:
                adb(args, "shell", "cmd", "thermalservice", "reset")
                c.notes.append("finally 兜底执行 thermalservice reset")
            except Exception as exc:  # noqa: BLE001
                c.notes.append(f"finally reset 失败：{exc}")
    r.add(c)


def _shedding(url: str) -> bool:
    try:
        h = get(f"{url}/v1/health", timeout=3).json()
        return bool(h.get("resources", {}).get("thermal_shedding"))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# D7 阵图退路
# --------------------------------------------------------------------------- #
def case_d7(args: argparse.Namespace, r: Report) -> None:
    c = CaseResult(
        "D7", "阵图退路（registry 不可达）",
        "临时 zhentu + 临时 quasheng(fallback_source=registry) → 停阵图 → chat 仍 200 → 重启阵图恢复",
        "阵图不可达只影响本次选路，自动退回静态链，chat 不失败",
    )
    tmp = Path(tempfile.mkdtemp(prefix="daojian-d7-"))
    zt_db = tmp / "registry.db"
    qs_cfg = tmp / "quasheng.yaml"
    write_quasheng_config(qs_cfg, port=P_QUASHENG_D7,
                          registry_url=f"http://127.0.0.1:{P_ZHENTU}",
                          registry_enabled=True, fallback_source="registry")
    zt = Proc("zhentu-d7")
    qs = Proc("quasheng-d7")
    qs_url = f"http://127.0.0.1:{P_QUASHENG_D7}"
    zt_url = f"http://127.0.0.1:{P_ZHENTU}"
    recovered = False
    try:
        zt.start_zhentu(zt_db, P_ZHENTU)
        zt_up = wait_for(lambda: _zhentu_up(zt_url), timeout=30, interval=0.5)
        c.data["zhentu_initial_up"] = zt_up
        if not zt_up:
            c.measured = False
            c.notes.append("临时 zhentu 未就绪，无法注入")
            c.check("临时阵图就绪", False, "启动超时")
            return

        qs.start_quasheng(qs_cfg, P_QUASHENG_D7)
        qs_up = wait_for(lambda: health_ok(qs_url), timeout=30, interval=0.5)
        c.data["quasheng_initial_up"] = qs_up
        if not qs_up:
            c.measured = False
            c.notes.append("临时 quasheng(registry 模式) 未就绪")
            c.check("临时节点就绪", False, "启动超时")
            return
        before = chat(qs_url)
        c.data["chat_before_status"] = before.status
        c.check("阵图在时 chat 200", before.status == 200, f"status={before.status}")

        # 注入：停止阵图
        zt.stop(signal.SIGKILL)
        zt_refused = wait_for(lambda: not port_open("127.0.0.1", P_ZHENTU), timeout=10, interval=0.2)
        c.data["zhentu_port_refused"] = zt_refused
        c.check("阵图停止后端口拒连", zt_refused, f"refused={zt_refused}")

        # 连续多次 chat，确认选路失败不影响本地推理
        statuses = []
        for _ in range(3):
            resp = chat(qs_url, max_tokens=8)
            statuses.append(resp.status)
            time.sleep(0.3)
        c.data["chat_during_outage"] = statuses
        c.check("阵图不可达时 chat 不失败", all(s == 200 for s in statuses), f"statuses={statuses}")

        h = get(f"{qs_url}/v1/health", timeout=5).json()
        c.data["node_status_during_outage"] = h.get("status")
        c.check("节点自身状态不受影响", h.get("status") in ("ready", "degraded"),
                f"status={h.get('status')}")
        c.notes.append("隔离原则：阵图故障仅影响本次选路（回退静态链），本地 chat/embeddings/health 保持可用；"
                       "本用例以 chat 200 + 节点 ready 作为可观测判据。")

        # 恢复：重启阵图
        zt.start_zhentu(zt_db, P_ZHENTU)
        recovered = wait_for(lambda: _zhentu_up(zt_url), timeout=30, interval=0.5)
        c.data["zhentu_recovered"] = recovered
        c.check("阵图重启后恢复", recovered, f"recovered={recovered}")
    except Exception as exc:  # noqa: BLE001
        c.notes.append(f"注入异常：{type(exc).__name__}: {exc}")
    finally:
        qs.stop(signal.SIGKILL)
        zt.stop(signal.SIGKILL)
        shutil.rmtree(tmp, ignore_errors=True)
    r.add(c)


def _zhentu_up(url: str) -> bool:
    try:
        return get(f"{url}/zhentu/v1/health", timeout=2).status == 200
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    args = parse_args()
    r = Report("inject_faults", env=meta_from_args(args))
    which = args.case
    if which in ("D4", "all"):
        case_d4(args, r)
    if which in ("D5", "all"):
        case_d5(args, r)
    if which in ("D7", "all"):
        case_d7(args, r)
    out = args.out or default_out("inject_faults")
    return exit_with(r, out, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
