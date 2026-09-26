# 全倉庫審閱：待辦清單

2026-09-26 對整個倉庫做了一次審閱（115、同步與掃描、背景服務、API 層、管理網頁與安裝腳本五塊分開看），
每一條都對照程式碼確認過。下面「已完成」的已經推到 main；「待辦」是還沒做的，依嚴重程度排。
行號會隨修改變動，所以都用檔案和函式名稱標位置。

## 接手須知

- 直接在 main 上提交，提交訊息用繁體中文，結尾加 `Co-Authored-By` 那一行；做完就推送，不用等使用者說。
- 每一批修改都要有回歸測試；目前 `pytest -q` 是 164 個全過。
- 動到 `embyserver/web/admin.html` 時，把 `<script>` 抽出來跑 `node --check`。
- 不要加回任何 Docker 相關檔案或說明（已經整個移除；install.sh 裡的 docker 字樣只是拒絕舊參數和搬遷舊安裝用的）。
- 使用者用 MoviePilot V3（看 V3 分支的原始碼），播放器是 SenPlayer。
- 安裝依賴：`pip install -r requirements.txt pytest`（需要 Python 3.10 以上）。

## 已完成

| 提交 | 內容 |
|---|---|
| eb88e94 | 115：錯誤回應帶空 data 不再當成空清單（開了 delete_stale 會把本機 strm 全刪）；JSON 限流、CDN 405 也停手不改逐層列目錄；cookie 有非 ASCII 字元直接拒絕；開放平台二維碼過期、目錄不存在；自己的 strm 取不到直鏈直接 502；任務 API 送錯格式回 400 |
| 5a612d5 | 同步與掃描：只改大小寫不誤刪；「刪舊集再上傳新集」不清刮削資料；115 一支都沒列出時不刪；壞掉的檔案不中斷掃描；nfo 只寫年份不產生假日期；拿掉演員時跟著清；任務資料夾要絕對路徑、不能互相包含；副檔名集中到 filetypes.py |
| 9312bf1 | API 層第一批：API 金鑰不能進 /web/api、/p115；路徑只轉英文小寫（/Persons/Émilie）；500 訊息遮 token；關閉時停同步和探測執行緒；設定檔不認得的鍵只警告；設定依宣告型別轉換（0.5 不再變 0）；探測狀態不再搶 deque 而 500；探測執行緒不會因意外錯誤死掉 |

## 待辦

### 一、API 層（routes/、auth.py、dto.py）

1. **下載路由不用登入。** `routes/playback.py` 的 `item_download`（`/Items/{id}/Download`、`/Items/{id}/File`）走 `_stream`，只在 `redirect.require_auth` 開啟時才檢查登入，預設關。串流 `/videos/{id}/stream*` 要維持不帶 token 可播（播放器常不帶），Download／File 改成 `require_user`。`tests/test_api.py` 的 `test_strm_302` 目前斷言 Download 不帶 token 會 302，要一起改。
2. **表單登入 500。** `routes/system.py` 的 AuthenticateByName 用 `request.form()`，但沒裝 python-multipart。改用 `urllib.parse.parse_qsl(raw.decode())` 解析，不加依賴。
3. **開始播放把續播點清成 0。** `routes/playback.py` 的 `_report`：回報沒帶 PositionTicks 時 `pos = 0` 還寫進去；`legacy_start` 一定不帶。沒帶位置且不是停止時只更新 `last_played`。
4. **UserId 處理不一致。** `routes/items.py` 的 `_user_id_from` 註解說管理員可以代查別人，但 `_query_items` 和 `_dto` 永遠用登入者自己的資料；非管理員打 `/Items?UserId=別人` 也回 200。二選一：把有效的 user id 一路傳下去，或不符就 403。`_query_items` 的 `user_id` 參數目前沒用到。
5. **非管理員可以讀任何使用者。** `routes/system.py` 的 `user_get`：不是自己也不是管理員就 403。
6. **async 函式裡做阻塞 I/O。** `routes/web.py` 的 `intro_clear`、`create_api_key`，`routes/items.py` 的 `upload_image`。改成一般 def 或包 `run_in_threadpool`。
7. **非物件 JSON 造成 500。** `routes/web.py` 的 `_body` 收到 `[1]` 這種內容時，後面 `.get` 會炸。不是 dict 就回 400。
8. **圖片路由的 `{index}` 宣告成 int。** `/Items/1/Images/Primary/abc` 回 422 JSON，Emby 是純文字 404。改成 str，或拿掉沒用到的參數。
9. **可讀性。**
   - `routes/items.py` 的 genres 裡 `import json as _json`（檔頭已經 import json），`_background` 裡臨時 import threading；`routes/system.py` 的 `library_refresh` 也是。
   - `routes/playback.py` 從 items.py 匯入私有的 `_set_user_data`，應搬到共用模組。
   - 魔術數字：看完門檻 0.9（playback），季×100000+集（items.py 兩處）。
   - `routes/system.py` 的 `OperatingSystem` 寫死 Linux；dto 的劇集 `Status` 寫死 Continuing。
   - `auth._is_api_key` 每個請求都讀資料庫、解析 JSON；`tokens.last_used` 從不更新。
   - settings 的 `*_FIELDS`、config 的 dataclass、`config_file.render` 三處手動列欄位，新欄位要改三處，漏改不會有測試發現。

### 二、背景服務（intro、backup、people、moviepilot、prober）

1. **片頭片尾：「從片尾直接跳到結尾」永遠學不到。** `intro.py` 的 `report`：外層 if 要求跳之前的位置在 8 分鐘內，裡面的 elif 又要求在片長 75% 以後，只有 10 分鐘內的短片同時成立。把「8 分鐘內」移進片頭那個 if。補測試：40 分鐘的集從 30:24 跳到 39:55 要記下片尾。
2. **備份可能是空的，還會把舊的好備份刪掉。** `backup.py` 的 `run` 用 `f"file:{path}?mode=ro"` 沒跳脫；資料夾名稱有 `#`、`?`（例如 NAS 的 Disk#2）時會開到另一個空資料庫。改用 `Path(...).resolve().as_uri() + "?mode=ro"`，並在 `os.replace` 前檢查備份裡有 meta、users 表。
3. **中文名查詢卡在同一個人。** `people.py` 的 `PersonNames.run`：MoviePilot 查某個人回 4xx（例如 nfo 的 tmdbid 是 `nm0000123`）就當成 MoviePilot 掛了、整批停下，這個人又沒記成「查過」，下次同一批卡在同一處。`pending()` 過濾非數字的 tmdbid；`_from_moviepilot` 分辨連線錯誤（停下）和單一 id 的 4xx（記成查過沒有）。Wikidata 一批全是非數字 id 時 VALUES 是空的，SPARQL 回 400。
4. **JWT 過期時一起重登。** `moviepilot.py` 的 `_request`：最多 8 條刮削執行緒同時拿到 401、同時重新登入。加鎖，「token 還是舊的才重登」。
5. **MoviePilot 連不上時網頁卡住。** `moviepilot.py` 的 `_request` 沒接 httpx 錯誤，網址填錯時「測試連線」回 500；搭配 admin.html 的 `testMP` 沒有 try/catch，網頁一直顯示「測試中…」。兩邊都要改。
6. **已快取的直鏈也占探測名額。** `prober.py` 的 `_source` 在取直鏈前就 `_wait_turn()`，剛播過（直鏈在快取裡）的也占掉間隔和每小時名額。先查快取，沒命中才排隊。
7. **TMDB 沒這一季時的誤導訊息。** `moviepilot.py` 的 `tmdb_episodes` 遇到 404 時日誌說「MoviePilot 沒有這個 API，請升級」。404 另外處理成「TMDB 沒有第 N 季」。
8. **可讀性。**
   - `moviepilot.py`：登入迴圈最後的 `raise MoviePilotError("MoviePilot 登入失敗")` 走不到；`library_series` 先 ORDER BY sort_name 又用 `name.lower()` 重排，中文變成按字碼排，應該用 sort_name；`ScrapeResult.errors` 沒上限；`mp_no_image` 從不清；`_fill_season` 對 `info["first"]` 硬取。
   - `intro.status` 在 SQL 裡算了沒用到的 first_at、intros、credits。
   - `people.py` 和 `intro.py` 各有一個相同的 `one = lambda`，應放到 `Database.scalar()`。
   - `people.py`：`TOP_ACTORS + 10` 註解說演員，實際也算導演、編劇；`PersonNames` 呼叫 MoviePilot 的私有 `_request`；UA 寫死 "Mi302/0.1"；`parse_people` 迴圈裡定義 closure（ruff B023，行為沒錯，改成傳參數）。
   - `mediainfo.py`：壞掉的 X-mediainfo.json 每次重掃都重新解析、重新警告。寫到一半留下的 `.part` 檔沒清（backup 也是）。
   - `prober.py`：到每小時上限時每分鐘印一次同樣的 INFO。

### 三、115 層（p115.py、p115_open.py、redirect.py）

1. **舊 cookie 失效會連累開放平台。** 全量同步的導出目錄樹只能用 cookie；cookie 失效時熔斷變成「登入失效」，連開放平台正常可用的背景工作也全停，直到重新設定 cookie。開放平台有授權時，cookie 失效應該只停用 cookie 通道，並在網頁提示。
2. **cookie 用 http 明文送出。** `DOWNLOAD_API` 是 `http://proapi.115.com/...`。試試 https 能不能用。
3. **不用登入的轉址端點可能打出限流。** `/d/{code}`、`/p115/redirect` 每個亂打的 pickcode 都會向 115 請求一次，打出限流會讓背景同步停 45 分鐘。考慮負面快取或每個 IP 限速。
4. **可讀性。**
   - `Breaker.tripped` 是有副作用的 property（會改狀態、寫日誌）。
   - 魔術數字：`LIST_PAGE_SIZE = 1150`、`life_events` 的 64 和 1000、`_snippet` 的 2000 和 120。
   - `p115_open.py` 的 `iter_changed_files`、`list_dir` 各自整理欄位，fallback 不一致，抽一個 `_normalize`。
   - UA 空字串時，cookie 通道送空 UA，開放平台換成 OPEN_UA；應在 `download_url` 統一決定。`user_info` 沒帶 UA、`_cookie_profile` 帶 BROWSER_UA。
   - 資料庫已經有 cookie 後，設定檔的 `p115.cookies` 就被忽略，沒有註解說明。
   - `p115_open.py`：`_call` 最後的 raise 走不到；放棄的二維碼 `_sessions` 不清；`qrcode_status` 換 token 前就 pop 了會話，網路錯一次就要重新掃碼。
   - `redirect.apply_path_rules`：規則 from 是 "/" 時只會比對到 "/" 本身。

### 四、同步與掃描（strm_sync.py、scanner.py）

1. **大量刪除沒有上限。** 已加「115 一支都沒列出時不刪」，但 115 目錄填到另一個只有幾支影片的資料夾時，還是會刪掉本機大部分 strm。考慮刪除數超過本機 strm 的一半且多於 20 支時不刪、留說明。
2. **路徑逃逸的保險。** 115 檔名若有 `..`（沒確認 115 允許）會組出任務資料夾外的路徑。在 `_target` 或 `_handle_file` 加一道「結果要在 local 底下」的檢查。
3. **可讀性。** `_run_full` 自己組 local 和 `_TaskIndex`，其他地方都用 `_Ctx`；`_handle_file` 和 `_place_file` 名稱太像；「列出來的少於九成就改逐層列目錄」的 0.9 沒命名；`parse_nfo` 每個欄位 `text()` 叫兩次；`scanner.image_ext` 夾在常數中間。

### 五、管理網頁、安裝腳本、README

1. **舊 Docker 安裝搬遷會弄丟埠號和媒體路徑。** `install.sh` 的 `load_env` 讀了 `MI302_PORT` 卻沒用，`MI302_MEDIA` 沒讀；改用 Python 後埠號變 8096，媒體庫和任務路徑還是容器裡的 `/media/...`，全部顯示資料夾不存在，而 `save_env` 又把這兩個值寫掉。沿用舊埠號；`MI302_MEDIA` 不是 `/media` 時改寫設定檔裡的 `/media` 前綴，至少要醒目提醒。README 搬遷那段不要說「原樣沿用」。
2. **115 裝置類型下拉選單會把設定清空。** admin.html 進階設定的 `p115.app` 只有 7 個選項，設定值不在其中時，儲存會送空字串，之後掃碼登入失敗。未知的值補一個 option，或 `selectedIndex === -1` 時不送。
3. **卡片上的開關會還原同頁沒存的輸入。** `putSettings` 之後 `fillFields()` 重填整頁，只應刷新這次送出的欄位。
4. **補全缺集的劇集搜尋有競態。** `loadSeries` 沒有序號保護，慢的舊結果會蓋掉新的；offset 用畫面上的列數推算，換篩選後按「再顯示」會接到別的清單下面。
5. **伺服器重啟中打開網頁一片空白。** `boot()` 沒有錯誤處理。
6. **macOS 找不到 brew 裝的 ffprobe。** launchd plist 沒設 PATH，`/opt/homebrew/bin` 不在預設路徑裡。在 plist 加 PATH，或安裝時把 `command -v ffprobe` 寫進 `mediainfo.ffprobe`。
7. **macOS 上 `mi302` 指令建不起來時 README 沒說怎麼辦。** `/usr/local/bin` 是 root 擁有時會失敗；可以改試 `/opt/homebrew/bin`、`~/.local/bin`，README 補替代方式。
8. **日誌頁說「最近 3000 筆」，實際 `LOG_MAX = 1000`。**
9. **低優先。** `startQr` 連點兩下會有兩個輪詢互相覆蓋；`loadUsers`、`logoutOpen`、`logout115`、`loadKeys`、`addKey` 沒有 try/catch；`qrcode_image` 用 innerHTML 沒 `esc`；非管理員登入網頁時已發的 token 沒登出；`install.sh -y` 遇到埠被占用直接結束、沒說明；舊版目錄 chown 後 git 擁有者不一致，更新時會誤報連不上 GitHub。
10. **可讀性。** install.sh 的 `TZ`、`TZ_NAME`、`host_tz` 算了沒用，`set_conf` 重複呼叫，`current_port` 是多餘的別名；admin.html 的 `pollQr` 的 img 參數沒用、`.steps{counter-reset}` 沒用、AppID 有兩個輸入框、`syncWatch` 宣告在使用之後；README 的改密碼段落指向不存在的說明（手動安裝的指令是 `.venv/bin/python -m embyserver -c config.yaml --reset-password 帳號 新密碼`），「舊版 MoviePilot」摺疊標題和網頁上的文字對不上，sudo 說明不準（Linux 上每個子指令都會加 sudo），目錄裡「常見問題」的層級不對，選項表缺 `--branch`。

### 六、測試檔

- 沒用到的 import：`tests/test_incremental.py` 的 json、`tests/test_moviepilot.py` 的 MoviePilotConfig 和 PathRule、`tests/test_people.py` 的 json、`tests/test_web.py` 的 pytest。

## 建議順序

1. 背景服務第 1–3 項（片頭片尾學不到片尾、備份可能是空的、中文名卡住），都是使用者看得到的錯。
2. API 層第 1–5 項（安全性和續播點）。
3. 管理網頁第 1–5 項，和背景服務第 5 項一起做（MoviePilot 測試連線）。
4. 115 第 1 項。
5. 其餘可讀性項目。

## 還沒實機驗證的

- 片頭片尾跳過：等使用者用 SenPlayer 試（第一集手動跳過片頭，第二集看有沒有跳過按鈕）。
