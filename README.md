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

參考 [p115strmhelper](https://github.com/DDSRem-Dev/MoviePilot-Plugins/tree/main/plugins.v2/p115strmhelper) 的做法，伺服器本身就能登入 115 並產生直鏈，不需要另外跑 MoviePilot：

1. 開啟 `http://<主機>:8096/web/115`，先用本伺服器的管理員帳號登入，再按「產生 115 登入二維碼」，然後用 115 App 掃描並確認。也可以直接貼上 cookie，或寫在設定檔的 `p115.cookies`。
2. strm 內容只要帶有 pickcode 就會被接手，以下格式都可以：
   - `http://<MoviePilot>/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=xxx`：P115StrmHelper 產生的 strm 不必改
   - `http://<本伺服器>/p115/redirect?pickcode=xxx`
   - `115://xxx`
3. 播放時，伺服器以**播放器自己的 User-Agent** 向 115 取下載直鏈（115 的直鏈綁定 UA），然後 302 過去。直鏈依 (pickcode, UA) 快取到到期前 5 分鐘。
4. 115 取直鏈失敗時，會退回 strm 原網址（例如交給 MoviePilot 處理）。

### 從 115 產生 strm

在設定檔的 `p115.strm.tasks` 寫好「115 目錄 → 本機資料夾」的對應，然後把本機資料夾設成媒體庫路徑。同步時伺服器會遞迴列出 115 目錄：影片產生 `.strm`，`nfo`、圖片、字幕可以一併下載，目錄結構保持不變。同步完會自動重新掃描媒體庫。

產生的 strm 與 P115StrmHelper 格式相同，兩邊可以互換使用：

```
http://192.168.1.10:8096/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=abcdefghijklmnopq
```

`base_url` 對應 P115StrmHelper 的「MoviePilot 地址」。`strm_url_format: pickname` 會再附上 `&file_name=檔名`。檔名規則也相同：`電影.mkv` 產生 `電影.strm`，`原盤.iso` 產生 `原盤.iso.strm`。

觸發方式：
- `/web/115` 頁面上的「立即從 115 同步 strm」
- 設定 `interval` 定時同步
- 命令列 `python -m embyserver -c config.yaml --sync-115`

第二次同步時，內容沒變的 strm 不會重寫，已下載且大小相同的中繼資料也不會重新下載。開啟 `delete_stale` 會刪除 115 上已經不存在的項目。

## 使用

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 修改帳號與媒體庫路徑
python -m embyserver -c config.yaml
```

客戶端新增伺服器時填 `http://<主機>:8096`。

Docker：

```bash
docker build -t embyserver .
docker run -d -p 8096:8096 -v $PWD/config:/config -v /path/to/media:/media embyserver
```

`config.yaml` 裡的 `data_dir` 建議設成 `/config/data`，這樣資料庫會保留在掛載的資料夾中。

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
