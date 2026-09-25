# embyserver

一個 API 與 Emby 相容的影片管理伺服器。支援 Emby 的第三方播放器（Infuse、VidHub、SenPlayer、Emby 官方 App 等）可以直接登入使用。播放 `.strm` 時，伺服器以 **HTTP 302** 把播放器導向 strm 內記載的真實網址，影片流量不經過伺服器。

## 運作方式

1. 掃描設定中的媒體庫資料夾，把 `.strm` 與一般影片檔、NFO、海報寫入 SQLite。
2. 播放器呼叫 `POST /Items/{id}/PlaybackInfo`。strm 項目回傳 `Protocol=Http`、`IsRemote=true`，並強制 DirectPlay（關閉轉碼），`DirectStreamUrl` 指回 `/videos/{id}/stream.{container}`。
3. 播放器請求 `/videos/{id}/stream`、`/videos/{id}/original.xxx`、`/items/{id}/download` 時：
   - strm 項目：讀取 strm 內容，套用 `path_rules`，可選擇先跟隨上游重導向鏈，然後回 `302 Location: <真實網址>`。
   - 一般影片檔：直接送檔，支援 Range。
4. 路徑不分大小寫，`/emby`、`/mediabrowser` 前綴可有可無。

302 流程參考自 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 `embyreverseproxy` 外掛。差別在於這裡不需要背後有真的 Emby，Emby API 由本專案自行實作。

## 115 網盤

伺服器本身就能登入 115、從 115 目錄產生 strm，並在播放時取得 115 直鏈，不需要 MoviePilot 或其他工具。

### 登入

開啟 `http://<主機>:8096/web/115`，先用本伺服器的管理員帳號登入。有兩種 115 登入方式，可以同時使用：

- **115 開放平台（建議）**：先到 [115 開放平台](https://open.115.com) 申請應用取得 AppID，填進網頁（或設定檔的 `p115.open_app_id`），再掃碼授權。使用官方授權介面，token 會自動續期，比較不會被風控或被其他登入踢下線。
- **Cookie**：按「產生 115 登入二維碼」掃碼，或直接貼上 cookie，也可以寫在設定檔的 `p115.cookies`。

兩者都有時，取直鏈與列目錄會優先走開放平台，失敗再改用 cookie。

### 從 115 產生 strm

在設定檔的 `p115.strm.tasks` 寫好「115 目錄 → 本機資料夾」的對應，然後把本機資料夾設成媒體庫路徑。同步時伺服器會遞迴列出 115 目錄：影片產生 `.strm`，`nfo`、圖片、字幕可以一併下載，目錄結構保持不變。同步完會自動重新掃描媒體庫。

strm 內容是本伺服器的短連結：

```
http://192.168.1.10:8096/d/abcdefghijklmnopq.mkv
```

- `base_url` 填本伺服器對外的位址。
- 網址最後的副檔名讓播放器與掃描器認得容器格式。
- `include_name: true` 會在網址後面附上 `?/原檔名`，方便人工辨識；伺服器會忽略這一段。

觸發方式：
- `/web/115` 頁面上的「立即從 115 同步 strm」
- 設定 `interval` 定時同步
- 命令列 `python -m embyserver -c config.yaml --sync-115`

第二次同步時，內容沒變的 strm 不會重寫，已下載且大小相同的中繼資料也不會重新下載。開啟 `delete_stale` 會刪除 115 上已經不存在的項目。

### 播放

1. 播放器請求 strm 項目時，伺服器從 strm 取出 pickcode，以**播放器自己的 User-Agent** 向 115 取下載直鏈（115 的直鏈綁定 UA），然後 302 過去。直鏈依 (pickcode, UA) 快取到到期前 5 分鐘。
2. 其他工具產生的 strm 也認得，例如 `…/d/{pickcode}`、`…?pickcode=xxx`，舊檔案不必重新產生。
3. 115 取直鏈失敗時，會退回 strm 原網址。

## 部署

### Docker Compose（建議）

```bash
git clone https://github.com/MiCat-S/Mi302.git
cd Mi302
mkdir -p config
cp config.example.yaml config/config.yaml
# 編輯 config/config.yaml：帳號密碼、媒體庫路徑（用容器內路徑 /media/...）、115 設定
# 編輯 docker-compose.yml：把 /path/to/media 改成主機上的媒體資料夾
docker compose up -d --build
docker compose logs -f   # 看啟動與掃描紀錄
```

- 資料庫存在 `config/data/`，更新或重建容器都不會遺失。
- 更新版本：`git pull && docker compose up -d --build`。
- 設定檔裡的路徑都要寫**容器內**的路徑，例如主機 `/volume1/media` 掛到 `/media` 時，媒體庫路徑寫 `/media/movies`。
- `p115.strm.base_url` 填播放器連得到的位址，例如 `http://192.168.1.10:8096`。

### 直接用 Python

需要 Python 3.10 以上。

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 修改帳號與媒體庫路徑
python -m embyserver -c config.yaml
```

### 啟動後

1. 播放器（Infuse、VidHub、SenPlayer、Emby 官方 App 等）新增 Emby 伺服器，位址填 `http://<主機>:8096`，用設定檔裡的帳號登入。
2. 要用 115：開啟 `http://<主機>:8096/web/115` 登入 115，再按「立即從 115 同步 strm」。
3. 需要從外網連線時，在前面加一層反向代理（例如 Nginx、Caddy）提供 HTTPS，並把 `base_url` 改成對外網址。

## 媒體庫結構

```
movies/
  Inception (2010)/
    Inception (2010).strm      # 內容：https://.../Inception.mkv
    movie.nfo  poster.jpg  fanart.jpg
tv/
  Dark (2017)/
    tvshow.nfo  poster.jpg
    Season 1/
      Dark.S01E01.strm
```

季資料夾可以命名為 `Season 1`、`S01`、`第1季` 或 `Specials`。集數可從 `S01E02`、`1x02`、`第2集`、`EP02` 這些格式解析，也會讀取同名的 `.nfo`。

## 已實作的端點

- 系統：`System/Info/Public`、`System/Info`、`System/Ping`、`System/Endpoint`
- 使用者：`Users/AuthenticateByName`、`Users/Public`、`Users/{id}`、`Sessions/Logout`
- 媒體庫：`Users/{id}/Views`、`Library/MediaFolders`、`Library/VirtualFolders`、`Library/Refresh`
- 項目：`Users/{id}/Items`、`Items`（ParentId、Recursive、IncludeItemTypes、SortBy、SearchTerm、Filters、分頁）、`Users/{id}/Items/{itemId}`、`Items/Latest`、`Items/Resume`、`Shows/{id}/Seasons`、`Shows/{id}/Episodes`、`Shows/NextUp`、`Genres`
- 播放：`Items/{id}/PlaybackInfo`、`Videos/{id}/*`、`Items/{id}/Download`、`Sessions/Playing[/Progress|/Stopped]`
- 使用者資料：`PlayedItems`、`FavoriteItems`
- 圖片：`Items/{id}/Images/{type}`

## 測試

```bash
pip install pytest
python -m pytest
```
