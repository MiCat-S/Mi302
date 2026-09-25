"""strm 真實網址解析與 302 快取。"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .config import RedirectConfig
from .p115 import P115Error, P115Service, extract_pickcode
from .scanner import read_strm

log = logging.getLogger(__name__)


def apply_path_rules(value: str, rules) -> str:
    """前綴替換。可作用於本機路徑，也可作用於網址的 path 部分。"""
    if not value or not rules:
        return value
    query = ""
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        # 規則寫的是完整網址前綴時直接比對整串
        for rule in rules:
            if value.startswith(rule.source):
                return rule.target.rstrip("/") + value[len(rule.source):]
        path = parsed.path or "/"
        query = parsed.query
    else:
        path = value if value.startswith("/") else "/" + value
    for rule in rules:
        src = rule.source.rstrip("/") or "/"
        if path == src or path.startswith(src + "/"):
            suffix = path[len(src):].lstrip("/")
            new = rule.target.rstrip("/") + ("/" + suffix if suffix else "")
            return new + ("?" + query if query else "")
    return value


class Redirector:
    def __init__(self, config: RedirectConfig, p115: Optional[P115Service] = None):
        self.config = config
        self.p115 = p115
        self._cache: Dict[Tuple[int, str], Tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(follow_redirects=True, timeout=config.resolve_timeout)

    def strm_target(self, item) -> Optional[str]:
        """回傳 strm 指向的位置（套用路徑規則後）；非 strm 回傳 None。"""
        if not item["is_strm"]:
            return None
        raw = read_strm(Path(item["path"]))
        if not raw:
            return None
        return apply_path_rules(raw, self.config.path_rules)

    def display_target(self, item, base_url: str) -> Optional[str]:
        """PlaybackInfo 裡要讓播放器看到的 Path。

        115 pickcode 項目改成指回本伺服器的串流網址：有些播放器遇到 Http 來源會直接播 Path，
        這樣才能確保請求經過本伺服器、用播放器自己的 UA 向 115 取直鏈。
        """
        target = self.strm_target(item)
        if target and extract_pickcode(target) and self.p115 and self.p115.logged_in:
            container = item["container"] or "mkv"
            return f"{base_url.rstrip('/')}/videos/{item['id']}/stream.{container}?Static=true"
        return target

    def final_url(self, item, headers: Dict[str, str]) -> Optional[str]:
        target = self.strm_target(item)
        if not target:
            return None
        ua = headers.get("user-agent", "")
        # strm 帶 pickcode（P115StrmHelper 格式或 115://）且已登入 115：直接向 115 取直鏈
        pickcode = extract_pickcode(target)
        if pickcode and self.p115 and self.p115.logged_in:
            try:
                return self.p115.download_url(pickcode, ua)
            except P115Error:
                log.warning("115 取直鏈失敗，改用 strm 原網址：%s", target, exc_info=True)
        if not target.startswith(("http://", "https://")):
            return None
        if not self.config.resolve_redirects:
            return target
        key = (item["id"], ua)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[1] > now:
                return hit[0]
        final = self._resolve(target, ua)
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
            self._cache[key] = (final, now + self.config.cache_ttl)
        return final

    def _resolve(self, url: str, ua: str) -> str:
        """跟隨重導向鏈取最終網址（例如 alist / 115 直鏈），失敗就用原網址。"""
        try:
            headers = {"User-Agent": ua} if ua else {}
            resp = self._client.head(url, headers=headers)
            return str(resp.url)
        except Exception:
            log.warning("解析重導向失敗，使用原網址：%s", url, exc_info=True)
            return url
