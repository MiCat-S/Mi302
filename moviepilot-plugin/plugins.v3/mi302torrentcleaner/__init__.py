"""Mi302 清種助手：qBittorrent 裡下載中的種子太久沒速度就刪掉，排在後面的接著開始；不夠幾個在下載就強制開始，開了沒速度的也刪。

用 MoviePilot 已經設定好的 qBittorrent 下載器（不用再填網址帳密），定時工作交給 MoviePilot 的排程。規則和 Mi302 本體的
「MoviePilot → qBittorrent」一樣（見 keeper.py）；兩邊擇一打開就好，都開的話會搶著刪同一個種子。

- 每隔幾分鐘讀一次種子清單，只看正在下載、還沒下載完的；排隊、暫停、校驗中、做種的不動。
- 從看到它在下載算起，連續幾分鐘沒速度就刪（排隊的時間不算，MoviePilot 重新啟動後從頭算）；刪幾個就讓排在後面的接著開始幾個。
- 「隨時要有幾個在下載」：有速度的不夠，就照佇列順序強制開始排隊的（不受 qBittorrent 的佇列上限限制）；強制開始後幾秒內
  沒速度、tracker 又有正常回應，就是種子有問題，刪掉；tracker 沒回應或還有人做種的放回佇列，一小時內不再試。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .keeper import Keeper, KeeperConfig

HISTORY_KEEP = 100
PAGE_ROWS = 20


class _QbClient:
    """keeper 要的介面，接到 MoviePilot 下載器模組裡的 qbittorrent-api Client（instance.qbc）。"""

    def __init__(self, qbc) -> None:
        self.qbc = qbc

    def torrents(self) -> List[dict]:
        return [dict(t) for t in self.qbc.torrents_info()]

    def delete(self, hashes: List[str], files: bool) -> None:
        self.qbc.torrents_delete(delete_files=files, torrent_hashes=hashes)

    def start(self, hashes: List[str]) -> None:
        self.qbc.torrents_resume(torrent_hashes=hashes)  # qbittorrent-api 對 5.0 起的版本會自動改叫 start

    def set_force(self, hashes: List[str], on: bool) -> None:
        self.qbc.torrents_set_force_start(enable=on, torrent_hashes=hashes)

    def tracker_working(self, h: str) -> bool:
        rows = self.qbc.torrents_trackers(torrent_hash=h) or []
        return any(int(r.get("status") or 0) == 2 for r in rows if not str(r.get("url") or "").startswith("**"))


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(float(value)), hi))
    except (TypeError, ValueError):
        return default


def _float(value, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(float(value), hi))
    except (TypeError, ValueError):
        return default


class Mi302TorrentCleaner(_PluginBase):
    plugin_name = "Mi302 清種助手"
    plugin_desc = "qBittorrent 裡太久沒速度的種子自動刪掉，排在後面的接著開始；不夠幾個在下載就強制開始，開了沒速度的也刪。"
    plugin_icon = "https://raw.githubusercontent.com/MiCat-S/Mi302/main/embyserver/web/icon-192.png"
    plugin_version = "1.0.0"
    plugin_author = "MiCat-S"
    author_url = "https://github.com/MiCat-S/Mi302"
    plugin_order = 98
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._notify = False
        self._downloaders: List[str] = []  # 要管的下載器名稱；空的 = 所有 qBittorrent
        self._interval = 5  # 幾分鐘看一次
        self._cfg = KeeperConfig()
        self._keepers: Dict[str, Keeper] = {}
        self._last: Dict[str, dict] = {}  # 下載器 → 上一輪的結果
        self._lock = threading.Lock()  # 一次只跑一輪
        self._timer: Optional[threading.Timer] = None  # 剛強制開始了種子：過幾秒回來看，不等下一次排程
        self._stop = threading.Event()

    # ---------------- 外掛的基本介面 ----------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        picked = config.get("downloaders") or []
        self._downloaders = [str(x) for x in (picked if isinstance(picked, list) else [picked]) if x]
        self._interval = _int(config.get("interval"), 5, 1, 1440)
        tags = tuple(x.strip() for x in str(config.get("tags") or "").split(",") if x.strip())
        self._cfg = KeeperConfig(
            stalled_minutes=_int(config.get("stalled_minutes"), 60, 10, 10080),
            stalled_speed=_float(config.get("stalled_speed"), 0.0, 0.0, 100000.0),
            delete_files=bool(config.get("delete_files", True)),
            no_seeds_only=bool(config.get("no_seeds_only")),
            keep_active=_int(config.get("keep_active"), 0, 0, 50),
            force_seconds=_int(config.get("force_seconds"), 30, 10, 600),
            tags=tags,
        )
        self._keepers = {}  # 設定改了：沒速度的時間從頭算
        self._stop.clear()
        self._cancel_timer()
        if config.get("onlyonce"):
            logger.info("Mi302 清種助手：立即看一次")
            self.update_config({**config, "onlyonce": False})
            self._schedule(3)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{"id": "Mi302TorrentCleaner", "name": "Mi302 清種助手：看一次沒速度的種子", "trigger": "interval",
                 "func": self.run, "kwargs": {"minutes": self._interval}}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        items = [{"title": name, "value": name} for name in self._qb_names()]
        switch = lambda model, label: {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [  # noqa: E731
            {"component": "VSwitch", "props": {"model": model, "label": label}}]}
        number = lambda model, label, hint: {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [  # noqa: E731
            {"component": "VTextField", "props": {"model": model, "label": label, "type": "number", "hint": hint,
                                                   "persistent-hint": True}}]}
        return [{"component": "VForm", "content": [
            {"component": "VRow", "content": [switch("enabled", "啟用"), switch("notify", "刪掉時發通知"), switch("onlyonce", "立即看一次")]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                    {"component": "VSelect", "props": {"model": "downloaders", "label": "下載器", "items": items, "multiple": True,
                                                       "chips": True, "clearable": True, "hint": "不選 = 所有 qBittorrent 下載器",
                                                       "persistent-hint": True}}]},
                number("interval", "幾分鐘看一次", "1–1440，預設 5"),
            ]},
            {"component": "VRow", "content": [
                number("stalled_minutes", "連續幾分鐘沒速度就刪", "10–10080，預設 60。從看到它在下載算起，排隊、暫停的時間不算"),
                number("stalled_speed", "平均速度不超過幾 KB/s 算沒速度", "預設 0：完全沒下載到東西才算"),
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VTextField", "props": {"model": "tags", "label": "只看帶這些標籤的種子", "hint": "逗號分開；空的 = 全部",
                                                           "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [switch("delete_files", "連同下載到一半的檔案一起刪"), switch("no_seeds_only", "只刪做種數為 0 的")]},
            {"component": "VRow", "content": [
                number("keep_active", "隨時要有幾個種子在下載", "0–50，0 = 不管。有速度的不夠就照佇列順序強制開始排隊的，排隊的用完就不開"),
                number("force_seconds", "強制開始後幾秒內沒速度就刪", "10–600，預設 30。tracker 有正常回應卻沒速度才算種子有問題"),
            ]},
            {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text":
                    "只看正在下載、還沒下載完的種子，排隊、暫停、校驗中、做種的不動。刪掉幾個，排在後面的就接著開始幾個。"
                    "MoviePilot 會當成被刪的那幾集已經送去下載，之後還缺的要再訂閱（或用 Mi302 的補全缺集）。"
                    "和 Mi302 本體的「MoviePilot → qBittorrent」是同一套規則，兩邊擇一打開就好。"}}]}]},
        ]}], {"enabled": False, "notify": False, "onlyonce": False, "downloaders": [], "interval": 5, "stalled_minutes": 60,
              "stalled_speed": 0, "tags": "MOVIEPILOT", "delete_files": True, "no_seeds_only": False, "keep_active": 0,
              "force_seconds": 30}

    def get_page(self) -> List[dict]:
        blocks: List[dict] = []
        if not self._last:
            blocks.append(_alert("還沒看過。啟用後每隔幾分鐘看一次，或打開「立即看一次」。"))
        for name, r in self._last.items():
            head = (f"{name}：{_ago(r['at'])}看過，下載中 {r['downloading']} 個（有速度 {r['moving']} 個），排在後面等著的 {r['waiting']} 個"
                    + (f"，強制開始、還在等的 {r['forcing']} 個" if r.get("forcing") else ""))
            blocks.append(_alert(head, "error" if r.get("error") else "info"))
            if r.get("error"):
                blocks.append(_alert(r["error"], "error"))
            if r.get("slow"):
                blocks.append(_table(["種子", "沒速度多久", "下載了", "做種"], [
                    [t["name"], (f"強制開始 {t['quiet']} 秒" if t.get("forced") else _span(t["quiet"])), f"{t['progress'] * 100:.1f}%", str(t["seeds"])]
                    for t in r["slow"]]))
        history = self.get_data("removed") or []
        if history:
            blocks.append(_alert(f"最近刪掉的（{len(history)}）"))
            blocks.append(_table(["時間", "種子", "原因", "下載了", "檔案"], [
                [time.strftime("%m/%d %H:%M", time.localtime(x["at"])), x["name"],
                 (f"強制開始後 {x['seconds']} 秒沒速度" if x.get("rule") == "forced" else f"{_span(x['seconds'])}沒速度"),
                 f"{x['progress'] * 100:.1f}%", "一起刪了" if x.get("files") else "留著"] for x in history[:PAGE_ROWS]]))
        return blocks

    def stop_service(self) -> None:
        self._stop.set()
        self._cancel_timer()

    # ---------------- 看一次 ----------------

    def run(self) -> None:
        if not self._lock.acquire(blocking=False):
            return  # 上一輪還沒做完
        try:
            self._cancel_timer()
            for name, qbc in self._clients():
                keeper = self._keepers.get(name)
                if keeper is None:
                    keeper = self._keepers[name] = Keeper(self._cfg)
                result = keeper.check(_QbClient(qbc))
                self._last[name] = result.as_dict()
                self._report(name, result)
            if any(k.forcing for k in self._keepers.values()) and not self._stop.is_set():
                self._schedule(self._cfg.force_seconds + 2)  # 剛強制開始了種子：過幾秒回來看
        except Exception as exc:
            logger.error(f"Mi302 清種助手：看種子時出錯：{exc}")
        finally:
            self._lock.release()

    def _clients(self) -> List[Tuple[str, Any]]:
        """要管的 qBittorrent 下載器和它們的 qbittorrent-api Client。"""
        from app.sdk.services import DownloaderHelper

        out = []
        for name, service in DownloaderHelper().get_services(type_filter="qbittorrent").items():
            if self._downloaders and name not in self._downloaders:
                continue
            qbc = getattr(service.instance, "qbc", None)
            if qbc is None:
                logger.warning(f"Mi302 清種助手：下載器「{name}」沒連上，這一輪跳過")
                continue
            out.append((name, qbc))
        if not out and not self._downloaders:
            logger.warning("Mi302 清種助手：MoviePilot 裡沒有啟用的 qBittorrent 下載器")
        return out

    def _qb_names(self) -> List[str]:
        try:
            from app.sdk.services import DownloaderHelper

            return [name for name, conf in DownloaderHelper().get_configs().items() if conf.type == "qbittorrent"]
        except Exception as exc:  # 設定頁打得開比較重要
            logger.debug(f"Mi302 清種助手：列下載器出錯：{exc}")
            return []

    def _report(self, name: str, result) -> None:
        if result.error:
            logger.warning(f"Mi302 清種助手：{name}：{result.error}")
        lines = []
        for r in result.removed:
            how = f"強制開始後 {r['seconds']} 秒" if r["rule"] == "forced" else f"已經 {r['minutes']} 分鐘"
            lines.append(f"「{r['name']}」{how}沒速度（下載了 {r['progress'] * 100:.1f}%），刪掉了{'，連同檔案' if r['files'] else ''}")
        for line in lines:
            logger.info(f"Mi302 清種助手：{name}：{line}")
        if result.started:
            logger.info(f"Mi302 清種助手：{name}：接著開始 " + "、".join(f"「{t['name']}」" for t in result.started))
        if result.skipped:
            logger.info(f"Mi302 清種助手：{name}：強制開始後沒速度但先不刪、放回佇列："
                        + "、".join(f"「{t['name']}」（{t['why']}）" for t in result.skipped))
        if result.removed:
            rows = [{**r, "downloader": name} for r in result.removed]
            self.save_data("removed", (rows + (self.get_data("removed") or []))[:HISTORY_KEEP])
            if self._notify:
                self._notify_removed(name, lines)

    def _notify_removed(self, name: str, lines: List[str]) -> None:
        try:
            from app.schemas.types import NotificationType

            mtype = getattr(NotificationType, "Plugin", None)
        except Exception:
            mtype = None
        try:
            self.post_message(mtype=mtype, title=f"Mi302 清種助手：{name} 刪了 {len(lines)} 個種子", text="\n".join(lines))
        except Exception as exc:
            logger.warning(f"Mi302 清種助手：發通知出錯：{exc}")

    def _schedule(self, seconds: float) -> None:
        self._cancel_timer()
        self._timer = threading.Timer(seconds, self.run)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


# ---------------- 詳情頁用的小零件 ----------------


def _alert(text: str, kind: str = "info") -> dict:
    return {"component": "VAlert", "props": {"type": kind, "variant": "tonal", "text": text, "class": "mb-2"}}


def _table(heads: List[str], rows: List[List[str]]) -> dict:
    return {"component": "VTable", "props": {"hover": True, "class": "mb-4"}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": h} for h in heads]}]},
        {"component": "tbody", "content": [{"component": "tr", "content": [{"component": "td", "text": c} for c in row]} for row in rows]},
    ]}


def _span(seconds: int) -> str:
    return f"{max(1, round(seconds / 60))} 分鐘" if seconds < 3600 else f"{seconds / 3600:.1f} 小時".replace(".0 ", " ")


def _ago(at: float) -> str:
    s = time.time() - at
    return "剛剛" if s < 60 else f"{int(s // 60)} 分鐘前" if s < 3600 else f"{int(s // 3600)} 小時前"
