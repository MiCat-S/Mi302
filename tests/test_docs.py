"""說明文件的一致性：Wiki 的連結和錨點都找得到、每一頁都在側欄、三種語言的設定檔參考都寫到每一個鍵；
README 和管理網頁連到 Wiki 的「說明」也要找得到。"""

import re
from collections import Counter
from dataclasses import fields
from pathlib import Path
from urllib.parse import unquote

import pytest

from embyserver.config import (Config, LibraryConfig, MediaInfoConfig, MoviePilotConfig, P115Config, P115StrmConfig,
                               RedirectConfig, ServerConfig, StrmTask, UserConfig)

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "docs" / "wiki"
WIKI_URL = "https://github.com/MiCat-S/Mi302/wiki/"
PAGES = {p.stem: p for p in WIKI.glob("*.md")}
CONFIG_REFERENCE = ["設定檔參考", "配置文件参考", "Configuration-Reference"]
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
FENCE_RE = re.compile(r"^```.*?^```", re.M | re.S)


def slug(heading: str) -> str:
    """GitHub 產生標題錨點的方式：小寫、拿掉標點（留字母、數字、底線、連字號、空白），空白換成連字號。"""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # 標題裡的連結只留文字
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def anchors(page: str) -> set:
    text = FENCE_RE.sub("", PAGES[page].read_text(encoding="utf-8"))
    seen: Counter = Counter()
    out = set()
    for heading in HEADING_RE.findall(text):
        s = slug(heading)
        out.add(f"{s}-{seen[s]}" if seen[s] else s)  # 同名的標題：第二個起加 -1、-2
        seen[s] += 1
    return out


def check_target(target: str, where: str) -> str:
    """回傳錯誤說明，找得到就回傳空字串。target 是 Wiki 頁名，可以帶 #錨點。"""
    page, _, anchor = unquote(target).partition("#")
    if page not in PAGES:
        return f"{where}：找不到 Wiki 頁面 {page}"
    if anchor and anchor.lower() not in anchors(page):
        return f"{where}：{page} 沒有標題對得到 #{anchor}"
    return ""


def test_wiki_links_point_to_existing_pages_and_headings():
    problems = []
    for name, path in PAGES.items():
        text = FENCE_RE.sub("", path.read_text(encoding="utf-8"))
        for target in LINK_RE.findall(text):
            if target.startswith(WIKI_URL):
                target = target[len(WIKI_URL):]
            elif re.match(r"[a-z]+:", target) or target.startswith(("/", "../")):
                continue  # 外部網址、倉庫裡的檔案
            if target.startswith("#"):
                target = name + target
            problems.append(check_target(target, name))
    assert not [p for p in problems if p]


def test_readme_and_admin_page_links_to_wiki():
    problems = []
    for path in [*ROOT.glob("README*.md"), ROOT / "embyserver" / "web" / "admin.html"]:
        for target in re.findall(re.escape(WIKI_URL) + r"([^\s\"')<>]+)", path.read_text(encoding="utf-8")):
            problems.append(check_target(target, path.name))
    assert not [p for p in problems if p]


def test_every_wiki_page_is_in_the_sidebar():
    linked = {unquote(t).partition("#")[0] for t in LINK_RE.findall((WIKI / "_Sidebar.md").read_text(encoding="utf-8"))}
    missing = sorted(p for p in PAGES if not p.startswith("_") and p not in linked)
    assert not missing


@pytest.mark.parametrize("page", CONFIG_REFERENCE)
def test_config_reference_mentions_every_key(page):
    """設定檔的每一個鍵，三種語言的設定檔參考都要寫到（加了新設定卻只寫一種語言時這裡會紅）。"""
    text = PAGES[page].read_text(encoding="utf-8")
    classes = [Config, ServerConfig, UserConfig, LibraryConfig, RedirectConfig, P115Config, P115StrmConfig,
               StrmTask, MoviePilotConfig, MediaInfoConfig]
    hidden = {"path", "file_mtime", "problems"}  # Config 自己用的，不在設定檔裡
    keys = {f.name for cls in classes for f in fields(cls)} - hidden
    keys |= {"from", "to"}  # 路徑對應、路徑替換（PathRule）在設定檔裡寫成 from／to
    headings = set(HEADING_RE.findall(text))
    missing = sorted(k for k in keys if f"`{k}`" not in text and f"{k}:" not in text and k not in headings)
    assert not missing
