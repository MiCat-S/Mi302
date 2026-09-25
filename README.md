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

## 快速開始

整個設定只有兩個地方：

1. **設定檔 `config.yaml`**：只需要改帳號密碼和媒體庫資料夾，範本 `config.example.yaml` 裡標了「要改」的就是。
2. **網頁 `http://<主機>:8096/web/115`**：登入 115、選擇要產生 strm 的 115 目錄、按同步。

照下面「部署」啟動後：

1. 開 `http://<主機>:8096/web/115`，用設定檔裡的管理員帳號登入。
2. **第 1 步**：按「產生登入二維碼」，用手機 115 App 掃描確認。
3. **第 2 步**：填 115 目錄（例如 `/影視/電影`），選要放進哪個媒體庫資料夾，按「新增」。可以新增多個。
4. **第 3 步**：按「立即從 115 同步 strm」。同步完伺服器會自動掃描媒體庫。
5. 播放器（Infuse、VidHub、SenPlayer、Emby 官方 App 等）新增 Emby 伺服器，位址填 `http://<主機>:8096`，用設定檔裡的帳號登入。

> 本機資料夾建議用媒體庫底下的子資料夾（例如 `/media/movies/115`），避免和你自己放的檔案混在一起；開啟 `delete_stale` 時，這個資料夾裡 115 上已不存在的 strm 會被刪除。

## 115 網盤

伺服器本身就能登入 115、從 115 目錄產生 strm，並在播放時取得 115 直鏈，不需要 MoviePilot 或其他工具。

### 登入

在 `/web/115` 掃碼登入即可，不需要申請任何東西。登入會佔用 115 的一個裝置類型（預設是支付寶小程式），同類型的其他登入會被踢下線；要換類型可以改進階設定 `p115.app`。

也可以在網頁上貼上 cookie，或寫在設定檔的 `p115.cookies`。

**進階：115 開放平台**。如果你自己在 [115 開放平台](https://open.115.com) 申請到了應用，可以在網頁的進階區塊填 AppID 掃碼授權（或寫在 `p115.open_app_id`）。授權後取直鏈與列目錄會優先走開放平台，失敗再改用掃碼登入。一般用戶不需要這一步。

### 產生 strm

同步時伺服器會遞迴列出 115 目錄：影片產生 `.strm`，`nfo`、圖片、字幕可以一併下載，目錄結構保持不變。

strm 內容是本伺服器的短連結：

```
http://192.168.1.10:8096/d/abcdefghijklmnopq.mkv
```

- 伺服器位址會自動使用你開 `/web/115` 時的網址，網頁第 3 步會顯示目前用的位址。放在反向代理後面、想固定用網域時，再到設定檔填 `p115.strm.base_url`。
- 網址最後的副檔名讓播放器與掃描器認得容器格式。
- `include_name: true` 會在網址後面附上 `?/原檔名`，方便人工辨識；伺服器會忽略這一段。

觸發方式：
- `/web/115` 頁面上的「立即從 115 同步 strm」
- 設定 `p115.strm.interval` 定時同步（分鐘）
- 命令列 `python -m embyserver -c config.yaml --sync-115`

第二次同步時，內容沒變的 strm 不會重寫，已下載且大小相同的中繼資料也不會重新下載。

### 播放

1. 播放器請求 strm 項目時，伺服器從 strm 取出 pickcode，以**播放器自己的 User-Agent** 向 115 取下載直鏈（115 的直鏈綁定 UA），然後 302 過去。直鏈依 (pickcode, UA) 快取到到期前 5 分鐘。
2. 其他工具產生的 strm 也認得，例如 `…/d/{pickcode}`、`…?pickcode=xxx`，舊檔案不必重新產生。
3. 115 取直鏈失敗時，會退回 strm 原網址。

## 部署

### Docker Compose

```bash
git clone https://github.com/MiCat-S/Mi302.git
cd Mi302
mkdir -p config
cp config.example.yaml config/config.yaml
```

然後改兩個檔案：

- `docker-compose.yml`：把 `/path/to/media` 改成主機上放影片的資料夾，例如 `/volume1/media`。這個資料夾在容器裡叫 `/media`。
- `config/config.yaml`：改密碼。媒體庫路徑用容器裡的路徑，例如主機的 `/volume1/media/movies` 要寫 `/media/movies`。

```bash
docker compose up -d --build
docker compose logs -f   # 看啟動與掃描紀錄
```

- 資料庫、115 登入狀態、網頁上設定的同步任務都存在 `config/data/`，更新或重建容器都不會遺失。
- 更新版本：`git pull && docker compose up -d --build`。

### 直接用 Python

需要 Python 3.10 以上。

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 改密碼，媒體庫路徑寫這台機器上的實際路徑
python -m embyserver -c config.yaml
```

### 從外網連線

在前面加一層反向代理（例如 Nginx、Caddy）提供 HTTPS，並在設定檔填 `p115.strm.base_url` 為對外網址，再同步一次讓 strm 更新。

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
