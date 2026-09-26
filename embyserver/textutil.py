"""中文片名的排序和搜尋：拼音排序、拼音（全拼、首字母）搜尋、繁簡互通。

- 排序：中文轉成全拼，一個字一個音節、用空格隔開（流浪地球2 → liu lang di qiu 2），
  所以按字母跳轉有效，而且先比第一個字再比第二個字，和字典順序一樣。非中文維持原本的小寫。
- 搜尋：每個項目存一段 search_text（簡體片名、簡體原名、全拼、首字母），搜尋詞也轉簡體比對；
  兩個字以上的中文搜尋詞再用全拼比一次，繁體字轉簡體有時對不上（慶餘年 → 庆馀年），拼音一定一樣。
"""

from __future__ import annotations

import re
from typing import Optional

from pypinyin import Style, lazy_pinyin
from zhconv import convert

CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
PUNCT_RE = re.compile(r"[^\w\s]+")
SPACE_RE = re.compile(r"\s+")
SEP = "|"  # search_text 各段之間的分隔，避免跨段比對到


def has_cjk(text: Optional[str]) -> bool:
    return bool(text and CJK_RE.search(text))


def simplified(text: Optional[str]) -> str:
    text = text or ""
    return convert(text, "zh-hans") if has_cjk(text) else text


def _clean(text: str) -> str:
    return SPACE_RE.sub(" ", PUNCT_RE.sub(" ", text)).strip()


def sort_key(name: Optional[str]) -> str:
    """排序用的名稱：中文轉全拼（每字一個音節），其餘小寫。"""
    name = name or ""
    if not has_cjk(name):
        return name.lower()
    return _clean(" ".join(lazy_pinyin(simplified(name))).lower())


def pinyin_full(text: Optional[str]) -> str:
    """全拼連在一起，去掉標點空格：慶餘年 → qingyunian。"""
    return re.sub(r"[\W_]+", "", "".join(lazy_pinyin(simplified(text or ""))).lower())


def pinyin_initials(text: Optional[str]) -> str:
    """首字母：庆余年 → qyn；非中文的字照留。"""
    return re.sub(r"[\W_]+", "", "".join(lazy_pinyin(simplified(text or ""), style=Style.FIRST_LETTER)).lower())


def search_text(name: Optional[str], original: Optional[str] = None) -> str:
    """存在資料庫的搜尋欄位：簡體片名、簡體原名，片名有中文時再加全拼和首字母。"""
    parts = [simplified(name).lower(), simplified(original).lower()]
    if has_cjk(name):
        parts += [pinyin_full(name), pinyin_initials(name)]
    return SEP.join(p for p in parts if p)


def cjk_count(text: Optional[str]) -> int:
    return len(CJK_RE.findall(text or ""))
