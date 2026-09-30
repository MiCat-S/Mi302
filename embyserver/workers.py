"""背景工作的執行緒：每個服務記著自己開的，程式結束時先叫它們停、再等它們結束（有上限），之後才關資料庫和連線池。
不等的話，還在跑的工作會在資料庫關掉之後才去查（sqlite3.ProgrammingError），或用到已經關掉的連線池。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, List, Optional

log = logging.getLogger(__name__)

SHUTDOWN_WAIT = 30.0  # 程式結束時最多等背景工作幾秒；等不到的就不等了（daemon 執行緒跟著程式結束）


class Stopped(Exception):
    """程式要結束、或使用者按了「停止」：背景工作在兩項之間收手，沒做完的下次再做。"""


class Workers:
    """一個服務開的背景執行緒和它的停止旗標。工作裡要等的地方用 stop.wait()，迴圈每一項之間看 stop，設了就收手。

    busy：服務自己「正在做」的鎖。在請求裡直接做的工作（例如刪除）不是這裡開的執行緒，等這把鎖放開就知道做完了。
    """

    def __init__(self, stop: Optional[threading.Event] = None, busy=None):
        self.stop = stop or threading.Event()
        # 使用者按了「停止」：只停這個服務現在這一次工作（定時同步這類迴圈不受影響），下一次開始時清掉。
        # 和 stop（程式要結束）分開，stop 設了就不會再清
        self.cancel = threading.Event()
        self.busy = busy
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self, target: Callable, *args) -> threading.Thread:
        t = threading.Thread(target=target, args=args, daemon=True, name=getattr(target, "__qualname__", None))
        with self._lock:  # 在鎖裡啟動：join 拿到的都是已經啟動的
            self._threads = [x for x in self._threads if x.is_alive()]
            t.start()
            self._threads.append(t)
        return t

    @property
    def halted(self) -> bool:
        """程式要結束，或使用者按了停止。"""
        return self.stop.is_set() or self.cancel.is_set()

    @property
    def by_user(self) -> bool:
        """停下來是因為使用者按了停止（不是程式要結束）。"""
        return self.cancel.is_set() and not self.stop.is_set()

    def check(self) -> None:
        """兩項之間呼叫：程式要結束或按了停止就丟 Stopped。做到一半的工作不能照常收尾時用（例如掃描、同步，
        收尾會把沒看到的當成已經刪掉），丟出去才不會走到收尾。"""
        if self.halted:
            raise Stopped()

    def wait(self, seconds: float) -> bool:
        """等 seconds 秒；程式要結束或按了停止就提早醒來，回傳 True。"""
        end = time.monotonic() + seconds
        while not self.halted:
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.stop.wait(min(0.5, left))
        return True

    def join(self, timeout: float) -> List[str]:
        """等開過的執行緒（和 busy 鎖）結束，最多 timeout 秒；回傳還沒結束的名稱。"""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        left = [t.name for t in threads if t.is_alive()]
        if self.busy is not None:
            if self.busy.acquire(timeout=max(0.0, deadline - time.monotonic())):
                self.busy.release()
            else:
                left.append("進行中的工作")
        return left


def stop_all(services: Iterable, timeout: float = SHUTDOWN_WAIT) -> List[str]:
    """先叫每個服務停（在等的馬上醒來），再等它們的背景工作結束，全部加起來最多 timeout 秒；一個出錯不影響其他的。

    services 照「會開別人工作的在前面」排（整理完會開同步，同步完會開探測、刮削），等完前面的，後面的也都開好了；
    再看一輪，接住中途才開的。回傳等不到的工作名稱。
    """
    services = list(services)
    for s in services:
        try:
            s.stop()
        except Exception:  # 一個停不了不影響其他的
            log.warning("停止背景工作時出錯", exc_info=True)
    deadline = time.monotonic() + timeout
    left: List[str] = []
    for _ in range(2):
        left = []
        for s in services:
            left += s.workers.join(max(0.0, deadline - time.monotonic()))
    if left:
        log.warning("程式結束：%s 等了 %s 秒還沒停下，不再等", "、".join(left), int(timeout))
    return left
