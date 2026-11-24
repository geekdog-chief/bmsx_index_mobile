"""民间温度计桌面GUI：pywebview 套壳 dashboard.html，后台自动更新，三态状态灯。

- 绿=已更新 / 黄=正在后台更新(带进度) / 红=更新失败但保留旧数据
- 后台线程跑 update_index.build_snapshot，表现层钩子推送进度
- WebView2/pywebview 缺失时退回系统浏览器
"""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_URL = (BASE_DIR / "dashboard.html").as_uri()

PUSH_INTERVAL = 0.15  # 进度推送节流到 150ms，避免 evaluate_js 洪泛


def read_last_updated() -> str | None:
    """旧快照的 updatedAt（ISO），失败时页面仍可用本地 data.js 的时间。"""
    latest = BASE_DIR / "data" / "latest.json"
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
        return str(payload.get("updatedAt") or "") or None
    except Exception:
        return None


def fmt_hhmm(iso_text: str | None) -> str:
    if not iso_text:
        return ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso_text).strftime("%H:%M")
    except Exception:
        return ""


class GuiBridge:
    """JS 可调用的入口 + 后台更新线程 + 进度泵。"""

    def __init__(self) -> None:
        self.window = None
        self._lock = threading.Lock()
        self._running = False
        self._pending: "queue.Queue[dict]" = queue.Queue()
        self._pump_stop = threading.Event()

    # ---- JS API ----
    def update_now(self) -> str:
        """立即更新按钮：已在跑则忽略，否则后台起一轮 build_snapshot。"""
        self.start_update()
        return "ok"

    # ---- 内部 ----
    def attach(self, window) -> None:
        self.window = window
        threading.Thread(target=self._pump_loop, daemon=True).start()

    def start_update(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run_update, daemon=True).start()

    def _run_update(self) -> None:
        try:
            # 走 LLM 覆盖层统一入口：与 CLI 同一条评分链路（羊驼接管+词典兜底），
            # 且构建完成后原子落盘 latest/history/meta/data.js
            from update_index_llm import run_update

            run_update(progress_callback=self._pending.put)
        except Exception as exc:
            self._pending.put({
                "type": "state", "state": "bad",
                "error": f"{type(exc).__name__}: {exc}",
                "keptAt": fmt_hhmm(read_last_updated()),
            })
            print(traceback.format_exc())
        finally:
            with self._lock:
                self._running = False

    def _pump_loop(self) -> None:
        """汇总 _pending 中最新事件，节流向页面推送。"""
        while not self._pump_stop.is_set():
            try:
                payload = self._pending.get(timeout=0.2)
            except queue.Empty:
                continue
            # 排空队列只留最新
            latest = payload
            while True:
                try:
                    latest = self._pending.get_nowait()
                except queue.Empty:
                    break
            if self.window is None:
                continue
            try:
                self.window.evaluate_js(
                    f"receiveGuiStatus({json.dumps(latest, ensure_ascii=False)})"
                )
            except Exception:
                # 窗口可能刚关闭或 WebView 未就绪，丢一条不致命
                pass


def run_gui() -> int:
    try:
        import webview  # pywebview
    except ImportError:
        print("未安装 pywebview，退回系统浏览器模式。")
        import webbrowser
        webbrowser.open(DASHBOARD_URL)
        return 0

    bridge = GuiBridge()
    try:
        window = webview.create_window(
            "民间温度计 · 宝妈指数",
            DASHBOARD_URL,
            width=1200,
            height=800,
            min_size=(960, 640),
            js_api=bridge,
        )
    except Exception as exc:
        print(f"创建窗口失败（{exc}），退回系统浏览器模式。")
        import webbrowser
        webbrowser.open(DASHBOARD_URL)
        return 0

    bridge.attach(window)
    window.events.closed += lambda: bridge._pump_stop.set()

    # 启动即后台拉一轮；45分钟缓存续用意味着多数情况秒完成
    def _boot():
        time.sleep(1.0)  # 等页面 ready
        bridge.start_update()

    threading.Thread(target=_boot, daemon=True).start()

    try:
        webview.start(debug=False)
    except Exception as exc:
        print(f"webview 启动失败（{exc}），退回系统浏览器模式。")
        import webbrowser
        webbrowser.open(DASHBOARD_URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
