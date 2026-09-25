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

不需要改任何設定檔，全部在網頁上完成。照下面「部署」啟動後，用瀏覽器開 `http://<主機>:8096/web`：

1. **建立管理員**：第一次打開會請你設定帳號密碼。登入後的「概覽」頁有設定步驟清單，照著點「前往」就好。
2. **媒體庫**：按「新增媒體庫」，取名（例如「電影」）、選類型，再按「加入資料夾」點選伺服器上的資料夾，最後按下方的「儲存並掃描」。Docker 版的媒體資料夾在 `/media` 底下。
3. **115 網盤**：
   - 按「掃碼登入」，用手機 115 App 掃描確認。
   - 在「同步任務」按「瀏覽」選 115 目錄和要放 strm 的本機資料夾，按「新增任務」。本機資料夾要在某個媒體庫裡，建議用子資料夾，例如 `/media/movies/115`。
   - 按「增量同步」。第一次會自動先跑一次全量，同步完會自動掃描媒體庫。
4. **刮削**（選用）：有 MoviePilot 的話，在「MoviePilot」分頁填網址和 API 令牌，之後新產生的 strm 會自動送去刮削。見下面「[刮削：交給 MoviePilot](#刮削交給-moviepilot)」。
5. **播放器**（Infuse、VidHub、SenPlayer、Emby 官方 App 等）新增 Emby 伺服器，位址填 `http://<主機>:8096`，用剛才的帳號登入。

家人的帳號在「使用者」分頁新增；自動同步間隔、strm 網址等在「115 網盤」的同步選項和「進階設定」。忘記管理員密碼時，執行 `python -m embyserver --reset-password admin 新密碼`（Docker：`docker compose exec mi302 python -m embyserver -c /config/config.yaml --reset-password admin 新密碼`）。

### 設定檔

網頁上的設定都存在設定檔 `config.yaml`（Docker 版在 `config/config.yaml`），兩邊保持一致：

- 第一次啟動時自動產生，每一項都附說明註解，不用自己建立。
- 在網頁上儲存設定時自動寫回，覆寫前把舊檔留成 `config.yaml.bak`。檔案每次都依範本重新產生，自己加的註解不會保留。
- 也可以直接改檔案，網頁重新整理後就會套用；`server` 的 `host`、`port`、`data_dir` 要重新啟動才生效。檔案格式寫錯時，網頁上方會顯示錯誤，並繼續用上次讀到的設定。
- 帳號密碼、115 登入狀態、網頁上建立的 API 金鑰存在資料庫（`data/`），不寫進設定檔。檔案裡的 `users` 只在第一次啟動時用來建立帳號。
- 舊版存在資料庫裡的網頁設定和同步任務，更新後第一次啟動會自動搬進設定檔。

各項目的說明見 `config.example.yaml`。

## 115 網盤

伺服器本身就能登入 115、從 115 目錄產生 strm，並在播放時取得 115 直鏈，不需要 MoviePilot 或其他工具。

### 登入

在 `/web` 的「115 網盤」分頁掃碼登入即可，不需要申請任何東西。登入會佔用 115 的一個裝置類型（預設是支付寶小程式），同類型的其他登入會被踢下線；要換類型可以改進階設定 `p115.app`。

也可以在網頁上貼上 cookie，或寫在設定檔的 `p115.cookies`。

### 帳號狀態

登入後「115 網盤」分頁的「帳號」卡片會顯示帳號資訊（「概覽」頁也有摘要），按「重新整理」會立即向 115 重新查詢（平常一分鐘內用快取）：

- 帳號名稱、UID、頭像
- VIP 等級與到期日（永久 VIP 會標示）
- 空間：已用、總量、剩餘，附用量條
- Cookie 是否仍然有效；失效時顯示 115 回的原因，重新掃碼即可
- 登入方式（掃碼、貼 cookie 或設定檔）、掃碼時佔用的裝置類型、登入時間
- 目前登入這個帳號的所有裝置，標出哪一個是本伺服器；看得出是不是被其他同類型登入踢掉
- 有授權 115 開放平台時，另外顯示開放平台的帳號與 token 到期時間

**進階：115 開放平台**。如果你自己在 [115 開放平台](https://open.115.com) 申請到了應用，可以在網頁的進階區塊填 AppID 掃碼授權。授權後取直鏈與列目錄會優先走開放平台，失敗再改用掃碼登入。一般用戶不需要這一步。

### 產生 strm

同步時伺服器會遞迴列出 115 目錄：影片產生 `.strm`，`nfo`、圖片、字幕可以一併下載，目錄結構保持不變。

strm 內容是本伺服器的短連結：

```
http://192.168.1.10:8096/d/abcdefghijklmnopq.mkv
```

- 伺服器位址會自動使用你開管理網頁時的網址，網頁第 3 步會顯示目前用的位址。放在反向代理後面、想固定用網域時，到「進階設定」填 strm 的伺服器網址。
- 網址最後的副檔名讓播放器與掃描器認得容器格式。
- 進階設定可以在網址後面附上 `?/原檔名`，方便人工辨識；伺服器會忽略這一段。

### 增量同步與全量同步

| | 增量同步 | 全量同步 |
|---|---|---|
| 依據 | 115 的生活事件（網盤的操作紀錄），再用修改時間補抓 | 逐層列出整個 115 目錄重新比對 |
| 處理 | 上傳、接收、複製、移動、改名、刪除 | 補齊漏掉的檔案；勾選「跟著刪」時清掉已刪除的項目 |
| 速度 | 快，只讀上次同步之後的事件 | 目錄多時較慢 |
| 適合 | 每隔幾分鐘自動跑 | 預設每週自動跑一次，查漏補缺 |

增量同步分兩步：

1. **讀生活事件**。115 會記錄網盤裡的每個操作，Mi302 讀上次同步之後的事件，同一個檔案只看最後一個事件：
   - 上傳、接收、複製、從外面移進同步目錄：產生 strm；整個資料夾移進來時會列出裡面的檔案。
   - 在同步目錄裡移動、改名（檔案或資料夾）：本機的 strm 跟著搬，同名的 nfo、海報、字幕（例如 MoviePilot 刮削的）也一起搬，不必重新刮削。
   - 刪除、移出同步目錄：勾選「跟著刪」時，刪掉 strm 和它的刮削資料。
2. **依修改時間補抓**。請 115 列出同步目錄裡最近修改的檔案，補上沒有產生事件的上傳（例如離線下載、第三方工具上傳）。只需要一次請求，看到舊檔案就停。

其他說明：

- 生活事件只給檔案 id，所以全量同步時會記下每個 115 檔案對應的本機路徑。任務第一次增量同步（包括從舊版升級上來）會先自動跑一次全量。
- 115 最多只給最近的一萬筆事件；太久沒同步、中間的事件已經讀不到時，會自動改跑全量。
- 讀生活事件需要掃碼或 cookie 登入，同步時會順便打開 115 生活的「最近記錄」開關（關閉時 115 不記錄事件）。只用開放平台登入時只做第 2 步。
- 全量同步的間隔依各任務上次全量的時間計算，重新啟動不會重新計時。

觸發方式：
- 網頁上的「增量同步」、「全量同步」按鈕
- 同步選項裡的自動增量同步間隔（分鐘，建議 5）與自動全量同步間隔（小時，預設 168 = 每週）
- 命令列 `python -m embyserver -c config.yaml --sync-115`（全量）或 `--sync-115 incremental`

已下載且大小相同的中繼資料不會重新下載。

### 播放

1. 播放器請求 strm 項目時，伺服器從 strm 取出 pickcode，以**播放器自己的 User-Agent** 向 115 取下載直鏈（115 的直鏈綁定 UA），然後 302 過去。直鏈依 (pickcode, UA) 快取到到期前 5 分鐘。
2. 其他工具產生的 strm 也認得，例如 `…/d/{pickcode}`、`…?pickcode=xxx`，舊檔案不必重新產生。
3. 115 取直鏈失敗時，會退回 strm 原網址。

## 刮削：交給 MoviePilot

Mi302 不自己刮削，只讀取資料夾裡已經有的 nfo 和海報。這些資料可以來自 115（同步時一併下載），或交給 [MoviePilot](https://github.com/jxxghp/MoviePilot) 刮削：

1. 同步產生新的 strm 後，Mi302 把這些檔案的路徑送給 MoviePilot 的刮削 API。
2. MoviePilot 辨識影片、到 TMDB 等來源查資料，把 nfo、海報、背景圖寫進同一個資料夾。
3. 刮削完成後 Mi302 自動重新掃描，播放器就看得到海報和簡介。

### 設定

1. **兩邊要看得到同一批檔案**。兩邊看到的路徑一樣（例如都直接裝在同一台機器上）就不用填路徑對應；不一樣時在「路徑對應」填 `Mi302 的路徑 => MoviePilot 的路徑`：
   - Mi302 在 Parallels 虛擬機裡看到 `/media/psf/Vo`，MoviePilot 裝在 Mac 上看到 `/Volumes/Vo`：填 `/media/psf/Vo => /Volumes/Vo`。
   - 兩個都用 Docker：把同一個主機資料夾掛進兩個容器，掛載路徑一樣就不用填；例如 Mi302 掛成 `/media`、MoviePilot 掛成 `/mnt/media`，就填 `/media => /mnt/media`。
2. 在 MoviePilot 的「設定 → 系統」複製 **API 令牌**。
3. 在 Mi302 網頁的「MoviePilot」分頁填 MoviePilot 網址（例如 `http://192.168.1.10:3000`）和 API 令牌，按「儲存」再按「測試連線」。
4. 測試出現「拒絕存取」時，表示你的 MoviePilot 版本較舊、刮削 API 只接受登入，請展開「舊版 MoviePilot」填帳號密碼。

「測試連線」只檢查網址和 API 令牌：它送一個空路徑給刮削 API，MoviePilot 會回「刮削路径无效」拒絕，所以不會真的刮削。路徑對應對不對，要看第一次刮削的結果；MoviePilot 找不到檔案時，「刮削」卡片會列出它收到的路徑。

### 送出規則

- 電影：送 strm 檔本身。
- 劇集：整部劇還沒有 `tvshow.nfo` 時送整個劇集資料夾，一次處理劇、季、集；已經刮削過的劇只送新的那幾集。
- 已經有 nfo 的影片不送（包括單片資料夾裡的 `movie.nfo`），避免覆蓋從 115 帶下來或之前刮好的資料。
- 連線或認證失敗時會停下整批，網頁上顯示原因。

「同步產生新的 strm 後自動送去刮削」預設開啟；關掉時同步完直接掃描。已經存在的媒體庫可以按「刮削缺少資料的項目」，把所有還沒有 nfo 的影片送一次。

### 讓 MoviePilot 把 Mi302 當成 Emby

MoviePilot 可以把 Mi302 加成媒體伺服器，用來判斷片子是否已經有了、整理完自動通知 Mi302 重新掃描：

1. 在 Mi302 網頁的「MoviePilot」分頁建立一把 API 金鑰。
2. 在 MoviePilot 的「設定 → 媒體伺服器」新增 Emby，地址填 `http://<Mi302 主機>:8096`，API 金鑰貼上剛才那把。

## 部署

### Docker Compose

```bash
git clone https://github.com/MiCat-S/Mi302.git
cd Mi302
```

編輯 `docker-compose.yml`，把 `/path/to/media` 改成主機上放影片的資料夾，例如 `/volume1/media`。這個資料夾在容器裡叫 `/media`，網頁上選資料夾時會從這裡開始。

```bash
docker compose up -d --build
docker compose logs -f   # 看啟動與掃描紀錄
```

然後開 `http://<主機>:8096/web`，照「快速開始」設定。

- 設定存在 `config/config.yaml`，帳號、115 登入狀態、同步進度存在 `config/data/`，更新或重建容器都不會遺失。
- 更新版本：`git pull && docker compose up -d --build`。
- 要改埠號：改 `docker-compose.yml` 的 `"8096:8096"` 左邊的數字。

### 直接用 Python

需要 Python 3.10 以上。

```bash
pip install -r requirements.txt
python -m embyserver
```

然後開 `http://<主機>:8096/web`。設定檔 `config.yaml` 和資料庫 `data/` 會建立在目前的資料夾。要改埠號時，改 `config.yaml` 的 `server.port` 後重新啟動。

### 從外網連線

在前面加一層反向代理（例如 Nginx、Caddy）提供 HTTPS，並在網頁「進階設定」填 strm 的伺服器網址為對外網址，再同步一次讓 strm 更新。

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

季資料夾可以命名為 `Season 1`、`S01`、`第1季`、`第一季` 或 `Specials`。集數可從 `S01E02`、`1x02`、`第2集`、`第十二集`、`EP02` 這些格式解析，也會讀取同名的 `.nfo`。

劇集媒體庫裡可以有分類資料夾（例如 MoviePilot 二級分類的 `电视剧/国产剧/庆余年 (2019)/`），媒體庫路徑直接選 `电视剧` 就好，不用每個分類各加一次。判斷方式：有 `tvshow.nfo`、名稱帶年份（`劇名 (2019)`）、裡面有季資料夾或直接放著影片的資料夾是一部劇；其他資料夾當成分類，往下找（最多三層）。想讓每個分類在播放器首頁各佔一列，就每個分類各建一個媒體庫。

## 已實作的端點

- 系統：`System/Info/Public`、`System/Info`、`System/Ping`、`System/Endpoint`
- 使用者：`Users`、`Users/AuthenticateByName`、`Users/Public`、`Users/{id}`、`Sessions/Logout`
- 媒體庫：`Users/{id}/Views`、`Library/MediaFolders`、`Library/VirtualFolders`、`Library/VirtualFolders/Query`、`Library/SelectableMediaFolders`、`Library/Refresh`、`Library/Media/Updated`
- 項目：`Users/{id}/Items`、`Items`（ParentId、Recursive、IncludeItemTypes、SortBy、SearchTerm、Filters、分頁）、`Users/{id}/Items/{itemId}`、`Items/Latest`、`Items/Resume`、`Shows/{id}/Seasons`、`Shows/{id}/Episodes`、`Shows/NextUp`、`Genres`、`Items/Counts`、`Items/{id}/Refresh`
- 播放：`Items/{id}/PlaybackInfo`、`Videos/{id}/*`、`Items/{id}/Download`、`Sessions/Playing[/Progress|/Stopped]`
- 使用者資料：`PlayedItems`、`FavoriteItems`
- 圖片：`Items/{id}/Images/{type}`

## 測試

```bash
pip install pytest
python -m pytest
```
