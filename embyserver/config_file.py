"""把目前的設定寫回 config.yaml，讓網頁上的修改和設定檔保持一致。

檔案每次都依下面的範本重新產生（附說明註解），覆寫前把舊檔留成 config.yaml.bak。
帳號密碼、115 登入狀態、網頁建立的 API 金鑰存在資料庫，不寫進設定檔。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, List

import yaml

from .config import Config, PathRule


def _v(value: Any) -> str:
    """YAML 純量：需要時自動加引號（例如含有 : 或 # 的字串）。"""
    text = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True, width=10_000).strip()
    if text.endswith("\n..."):
        text = text[:-4].strip()
    return text


def _kv(key: str, value: Any, comment: str = "") -> str:
    """一行「鍵: 值」，說明註解對齊在同一欄。"""
    line = f"{key}: {_v(value)}"
    return f"{line.ljust(34)}  # {comment}" if comment else line


def _rules(rules: List[PathRule], indent: str) -> str:
    if not rules:
        return " []"
    return "".join(f"\n{indent}- from: {_v(r.source)}\n{indent}  to: {_v(r.target)}" for r in rules)


def render(config: Config) -> str:
    s, p, st, mp, rd = config.server, config.p115, config.p115.strm, config.moviepilot, config.redirect
    mi = config.mediainfo
    lines = [
        "# Mi302 設定檔",
        "#",
        "# 網頁 http://<這台機器的 IP>:<埠號>/web 上儲存設定時，會自動改寫這個檔案（舊檔留成 config.yaml.bak）。",
        "# 也可以直接改這個檔案：網頁重新整理後就會套用；server 的 host、port、data_dir 要重新啟動才生效。",
        "# 帳號密碼、115 登入狀態、網頁上建立的 API 金鑰存在資料庫，不在這裡。",
        "",
        "server:",
        _kv("  name", s.name, "播放器裡顯示的伺服器名稱"),
        _kv("  host", s.host, "監聽位址"),
        _kv("  port", s.port, "播放器連線用的埠號"),
        _kv("  data_dir", s.data_dir, "資料庫存放位置"),
        _kv("  public_users", s.public_users, "播放器登入畫面是否列出使用者"),
        _kv("  log_level", s.log_level, "日誌：info = 一般，debug = 詳細（另外記錄每個播放器請求）"),
        _kv("  backup_keep", s.backup_keep, "每天自動備份資料庫和設定檔到 data/backups，留最新幾份；0 = 不自動備份"),
        _kv("  chinese_people", s.chinese_people, "演職人員顯示中文名（經 MoviePilot 查 TMDB 別名，沒有再問 Wikidata）"),
        _kv("  chinese_genres", s.chinese_genres, "類型顯示中文（Action → 动作），繁體換成簡體；關掉後重新掃描即還原"),
        "",
        "# 第一次啟動時預先建立的帳號；之後新增帳號、改密碼請用網頁，不會寫回這裡",
    ]
    if config.users:
        lines.append("users:")
        for u in config.users:
            lines += [f"  - name: {_v(u.name)}", f"    password: {_v(u.password)}", f"    admin: {_v(u.admin)}"]
    else:
        lines.append("users: []")
    lines += ["", "# 媒體庫。type：movies = 電影，tvshows = 劇集"]
    if config.libraries:
        lines.append("libraries:")
        for lib in config.libraries:
            lines += [f"  - name: {_v(lib.name)}", f"    type: {_v(lib.type)}", "    paths:"]
            lines += [f"      - {_v(path)}" for path in lib.paths]
    else:
        lines.append("libraries: []")
    lines += [
        "",
        "# 115 網盤。登入請用網頁掃碼",
        "p115:",
        _kv("  cookies", p.cookies, "直接寫 115 cookie，效果等同在網頁掃碼登入（只在還沒登入時使用）"),
        _kv("  app", p.app, "掃碼登入時佔用的 115 裝置類型；同類型的其他登入會被踢下線"),
        _kv("  timeout", p.timeout, "呼叫 115 的逾時秒數"),
        _kv("  open_app_id", p.open_app_id, "115 開放平台 AppID，只有自己申請到應用的人才需要"),
        "  strm:",
        _kv("    interval", st.interval, "每隔幾分鐘自動增量同步（讀 115 生活事件）；0 = 不自動，建議 5"),
        _kv("    full_interval", st.full_interval, "每隔幾小時自動全量同步（查漏補缺）；0 = 不自動，168 = 每週"),
        _kv("    min_size_mb", st.min_size_mb, "小於這個大小（MB）的影片不產生 strm"),
        _kv("    download_metadata", st.download_metadata, "一併下載 115 上的 nfo、海報、字幕"),
        _kv("    delete_stale", st.delete_stale, "115 上刪掉或移出同步目錄的影片，本機的 strm 和刮削資料也跟著刪"),
        _kv("    base_url", st.base_url, "strm 裡的伺服器網址，留空 = 自動使用開網頁時的網址"),
        _kv("    include_name", st.include_name, "strm 網址後附上 ?/原檔名"),
        _kv("    request_delay", st.request_delay, "每列一個 115 目錄前等待的秒數"),
        _kv("    scan_after_sync", st.scan_after_sync, "同步完自動重新掃描媒體庫"),
        "    # 同步任務：115 目錄 → 本機資料夾（本機資料夾要在某個媒體庫裡）",
    ]
    if st.tasks:
        lines.append("    tasks:")
        for t in st.tasks:
            lines += [f"      - remote: {_v(t.remote)}", f"        local: {_v(t.local)}"]
    else:
        lines.append("    tasks: []")
    lines += [
        "",
        "# 刮削交給 MoviePilot：新產生的 strm 送給 MoviePilot 查資料、寫 nfo 和海報",
        "moviepilot:",
        _kv("  url", mp.url, "MoviePilot 網址，例如 http://192.168.1.10:3000"),
        _kv("  api_token", mp.api_token, "MoviePilot 的「設定 → 系統 → API 令牌」"),
        _kv("  username", mp.username, "MoviePilot 帳號密碼：補全缺集（建訂閱）需要；舊版刮削 API 不接受令牌時也需要"),
        _kv("  password", mp.password),
        _kv("  scrape_after_sync", mp.scrape_after_sync, "同步產生新的 strm 後自動送去刮削"),
        _kv("  fill_after_full_sync", mp.fill_after_full_sync, "全量同步後把所有有 tmdbid 的劇送給 MoviePilot 訂閱，補齊缺集"),
        _kv("  timeout", mp.timeout, "每一項刮削最多等幾秒"),
        _kv("  concurrency", mp.concurrency, "同時送幾項給 MoviePilot 刮削（1–8），太多可能被 TMDB 限速"),
        "  # 兩邊看到的路徑不同時：Mi302 的路徑（from）→ MoviePilot 的路徑（to）",
        "  path_mappings:" + _rules(mp.path_mappings, "    "),
        "",
        "# 媒體資訊：用 ffprobe 探測 strm 指向的影片，寫出 X-mediainfo.json（需要安裝 ffmpeg）",
        "mediainfo:",
        _kv("  enabled", mi.enabled, "整庫探測（手動按鈕、同步後自動）；關閉時仍會讀現成的 X-mediainfo.json"),
        _kv("  after_sync", mi.after_sync, "同步產生新的 strm 後自動探測（要開整庫探測）"),
        _kv("  on_demand", mi.on_demand, "播放器打開某部片或某一集時，在背景探測它"),
        _kv("  concurrency", mi.concurrency, "同時探測幾項（1–3），115 同時最多 3 條連線"),
        _kv("  interval", mi.interval, "每次向 115 取直鏈至少間隔幾秒（0.5–60）"),
        _kv("  hourly_limit", mi.hourly_limit, "每小時最多向 115 取幾次直鏈（0 = 不限）"),
        _kv("  timeout", mi.timeout, "每一項最多等幾秒"),
        _kv("  ffprobe", mi.ffprobe, "ffprobe 的路徑"),
        "",
        "# 給其他工具產生的 strm（例如 alist 網址或本機路徑）",
        "redirect:",
        _kv("  resolve_redirects", rd.resolve_redirects, "先由伺服器跟著上游重導向走到底"),
        _kv("  resolve_timeout", rd.resolve_timeout),
        _kv("  cache_ttl", rd.cache_ttl),
        _kv("  require_auth", rd.require_auth, "播放網址是否要求登入（很多播放器不帶 token）"),
        _kv("  default_container", rd.default_container, "strm 看不出影片格式時的預設格式"),
        "  # 把 strm 內容開頭的 from 換成 to，再 302 給播放器",
        "  path_rules:" + _rules(rd.path_rules, "    "),
        "",
        "# 固定的 API 金鑰，給其他程式以管理員身分呼叫 API（網頁上也能建立，那些存在資料庫）",
        "api_keys:" + ("".join(f"\n  - {_v(k)}" for k in config.api_keys) if config.api_keys else " []"),
        "",
    ]
    return "\n".join(lines)


def write(config: Config, path: str) -> float:
    """寫入設定檔（先寫暫存檔再換掉，舊檔留成 .bak），回傳新檔的修改時間。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(config), encoding="utf-8")
    if target.exists():
        shutil.copy2(target, target.with_name(target.name + ".bak"))
    try:
        os.replace(tmp, target)
    except OSError:
        # 設定檔是掛載進來的單一檔案時不能替換，只能直接覆寫內容
        target.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.unlink(missing_ok=True)
    return target.stat().st_mtime
