"""擋住亂猜的：登入猜密碼、不用登入的轉址端點亂打 pickcode。都只記在記憶體，重新啟動就歸零。

來源（client）是連線的 IP。Mi302 放在反向代理後面時是代理轉來的 X-Forwarded-For；直接對外的話這個標頭可以偽造，
所以每一種都另外有一個不看來源的總量上限。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Tuple

MAX_KEYS = 10000  # 最多記幾個來源；超過就先清掉過期的，還是滿的話新的來源不個別記（總量上限照樣擋）


class TooManyAttempts(Exception):
    """猜太多次了，retry_after 秒之後再試。"""

    def __init__(self, retry_after: float):
        super().__init__(f"嘗試太多次，請 {int(retry_after) + 1} 秒後再試")
        self.retry_after = retry_after


class FailureLimiter:
    """一段時間內失敗太多次就先擋：per_client 是同一個來源的上限，overall 是所有來源加起來的上限。
    只算失敗的，正常使用（都成功）不受影響。"""

    def __init__(self, per_client: int, overall: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.per_client, self.overall, self.window, self.clock = per_client, overall, window, clock
        self._by_client: Dict[str, Deque[float]] = {}
        self._all: Deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, q: Deque[float], now: float) -> None:
        while q and now - q[0] >= self.window:
            q.popleft()

    def retry_after(self, client: str) -> float:
        """現在要等幾秒才能再試；0 = 可以。"""
        now = self.clock()
        with self._lock:
            self._trim(self._all, now)
            mine = self._by_client.get(client)
            if mine is not None:
                self._trim(mine, now)
                if not mine:
                    del self._by_client[client]
                elif len(mine) >= self.per_client:
                    return self.window - (now - mine[0])
            if len(self._all) >= self.overall:
                return self.window - (now - self._all[0])
        return 0.0

    def failed(self, client: str) -> None:
        now = self.clock()
        with self._lock:
            self._all.append(now)
            if client not in self._by_client and len(self._by_client) >= MAX_KEYS:
                for key in [k for k, q in self._by_client.items() if now - q[-1] >= self.window]:
                    del self._by_client[key]
                if len(self._by_client) >= MAX_KEYS:
                    return
            self._by_client.setdefault(client, deque()).append(now)


class LoginThrottle:
    """猜密碼：同一個來源對同一個帳號連續錯 FREE 次之後，每錯一次要等的時間加倍（30 秒起，最多 15 分鐘）；
    登入成功、或 15 分鐘沒再錯就歸零。同一個來源不管猜哪個帳號，10 分鐘內錯 20 次也擋。
    照（帳號, 來源）記：別人在別的地方猜你的帳號，不會把你自己鎖在外面。"""

    FREE = 5
    BASE, CAP, FORGET = 30.0, 900.0, 900.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._fails: Dict[Tuple[str, str], Tuple[int, float]] = {}  # (帳號, 來源) → (連續錯幾次, 最後一次的時間)
        self._by_client = FailureLimiter(per_client=20, overall=200, window=600.0, clock=clock)
        self._lock = threading.Lock()

    @staticmethod
    def _key(user: str, client: str) -> Tuple[str, str]:
        return (user or "").strip().lower(), client or ""

    def retry_after(self, user: str, client: str) -> float:
        now = self.clock()
        with self._lock:
            count, last = self._fails.get(self._key(user, client), (0, 0.0))
        wait = 0.0
        if count >= self.FREE and now - last < self.FORGET:
            wait = min(self.BASE * 2 ** (count - self.FREE), self.CAP) - (now - last)
        return max(wait, self._by_client.retry_after(client or ""), 0.0)

    def failed(self, user: str, client: str) -> None:
        now, key = self.clock(), self._key(user, client)
        self._by_client.failed(client or "")
        with self._lock:
            count, last = self._fails.get(key, (0, 0.0))
            if now - last >= self.FORGET:
                count = 0
            if key not in self._fails and len(self._fails) >= MAX_KEYS:
                self._fails = {k: v for k, v in self._fails.items() if now - v[1] < self.FORGET}
                if len(self._fails) >= MAX_KEYS:
                    return
            self._fails[key] = (count + 1, now)

    def succeeded(self, user: str, client: str) -> None:
        with self._lock:
            self._fails.pop(self._key(user, client), None)
