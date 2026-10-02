# 新功能：從阿里雲盤秒傳到 115，交給 MoviePilot 整理（方案，2026-10-03）

基準：`main` = `6388753`（轉存 115 分享已上線）。接在「轉存 115 分享」（`docs/share-transfer-2026-10-03.md`、`embyserver/share115.py`）之後做，重用它加的 `Organizer.organize_units_in_background`、`Organizer.is_pinned`、`GET /web/api/moviepilot/library-dirs`（回 `{enabled, dirs, error}`）、網頁上「整理到」的選單（`shDirs`、`shTargetUi`、`shTargetChange`、`shTargetPick`）。

## 目標

使用者在 Mi302 登入阿里雲盤，選一個阿里雲盤上的資料夾（或一支檔案），Mi302：

1. 列出底下的影片和字幕（阿里雲盤的檔案清單直接給整支檔案的 SHA1）；
2. 一支一支向 115 要求秒傳到「存到」底下的新資料夾（保留原本的子資料夾結構）。115 要二次驗證時，向阿里雲盤讀那一小段算 SHA1 回給它。115 上沒有的檔案不能秒傳，**不真的上傳**，列出來；
3. 秒傳完的資料夾加進「整理 115 網盤」，選了「整理到」就在背景交給 MoviePilot 整理（和轉存分享一樣）。

## 實測過的和沒實測的

| 項目 | 狀態 | 內容 |
|---|---|---|
| `GET https://proapi.115.com/app/uploadinfo`（cookie） | 實測 | `state: true`，有 `user_id`（整數）、`userkey`、`size_limit`、`upload_allowed`、`upload_allowed_msg` 等。 |
| `GET https://appversion.115.com/1/web/1.0/api/chrome` | 實測 | `data.win.version_code`，2026-10-03 是 `36.0.1`。 |
| `POST https://uplb.115.com/4.0/initupload.php` | 實測（第一步） | 用 `p115cipher.make_upload_payload(payload)` 得到 `params`（`k_ec`）和加密的 `data`，回應用 `p115cipher.ecdh_aes_decrypt(resp.content)` 解開是 JSON。**User-Agent 一定要是 `Mozilla/5.0 115Browser/<版本>`、`appversion` 要是上面的版本**，不然回 `{"status": 4, "statuscode": 403, "statusmsg": "请升级到最新版本"}`。隨機的 SHA1 回 `{"status": 1, "statuscode": 0, …, "bucket", "object", "callback"}` = 115 沒有這個檔案、要真的上傳；這一步不會建立檔案。`p115cipher.MD5_SALT` 和 OpenList 的 `md5Salt` 一樣。 |
| 秒傳的二次驗證和成功 | **沒實測**（照 OpenList `drivers/115/util.go`、p115client） | `status: 7`（`statuscode: 701`）時回 `sign_key` 和 `sign_check`（`"起-迄"`，迄含在內）；讀原檔那一段算 SHA1（大寫十六進位）當 `sign_val`，帶 `sign_key`、`sign_val` 再送一次 init（時間戳 `t`、`token` 要重算，`make_upload_payload` 會重算）。`status: 2`（`statuscode: 0`）= 秒傳成功，回應有 `pickcode`。OpenList 註記：115 對秒傳有時限，拖太久即使 SHA1 對也會回「sig invalid」，所以拿到 sign_check 要馬上讀、馬上送。 |
| 阿里雲盤開放平台 `https://openapi.alipan.com` | 實測（錯誤情況） | 伺服器連得到。錯的 token 回 `{"code": "AccessTokenInvalid", "message": "…", "requestId": "…"}`。 |
| OpenList 的線上換 token API `https://api.oplist.org/alicloud/renewapi` | 實測（錯誤情況） | 伺服器連得到。錯的 refresh token 回 `{"text": "invalid refresh_token"}`。 |
| 阿里雲盤成功時的欄位 | **沒實測**（照 OpenList `drivers/aliyundrive_open`） | 見下面「阿里雲盤」。 |

沒實測的一律防禦式解析，錯誤原文照實顯示。

## 阿里雲盤（新模組 `embyserver/aliyun.py`）

照 OpenList 的 `aliyundrive_open` 驅動（OpenList 已經把網頁版的 `aliyundrive` 驅動標成「已棄用」，那個要 secp256k1 簽章，不要用）。

- 登入：使用者貼 refresh token。換 access token 有兩種：
  - 設定了自己的 `aliyun.client_id`、`aliyun.client_secret`：`POST https://openapi.alipan.com/oauth/access_token`，JSON `{"client_id", "client_secret", "grant_type": "refresh_token", "refresh_token"}` → `access_token`、`refresh_token`、`expires_in`。
  - 沒有（大部分人）：`GET <aliyun.online_api>?refresh_ui=<refresh token>&server_use=true&driver_txt=alicloud_qr`（預設 `https://api.oplist.org/alicloud/renewapi`）→ `{"refresh_token", "access_token"}`，失敗時 `{"text": "原因"}`。**這會把 refresh token 送給 OpenList 的服務**，網頁和 Wiki 都要寫明；不想這樣就填自己的 client id／secret。refresh token 用 OpenList 的取得工具（`https://api.oplist.org/`，選阿里雲盤掃碼）拿。
  - 換回來的 refresh token 會換新（舊的可能失效），**每次換完都要存回資料庫**。refresh token 存在資料庫 meta（鍵 `aliyun_refresh_token`，和 115 的 cookie 一樣不寫進設定檔）；access token 只放記憶體，記到期時間（`expires_in`，沒有就 2 小時），提早 5 分鐘換。換 token 要上鎖，同時只換一次。
- 請求：`Authorization: Bearer <access token>`，POST 的 body 是 JSON。回 `code` 是 `AccessTokenInvalid`、`AccessTokenExpired`、`I400JD` 時換一次 token 再試一次；其他 `code` 丟 `AliyunError(f"{code}：{message}")`。
- 頻率限制（官方文件，照 OpenList）：列目錄每秒 4 次、取下載網址每秒 1 次、其他每秒 15 次。用三個「兩次之間至少隔幾秒」的限速（0.26、1.1、0.07 秒），執行緒安全。
- `POST /adrive/v1.0/user/getDriveInfo`（無 body）→ `user_id`、`name`、`default_drive_id`（備份盤）、`resource_drive_id`（資源庫）、`backup_drive_id`。快取到換帳號為止。
- `POST /adrive/v1.0/openFile/list`，JSON `{"drive_id", "parent_file_id"（根目錄是 "root"）, "limit": 100, "marker", "order_by": "name", "order_direction": "ASC"}` → `items[]`（`file_id`、`name`、`type`（"file"／"folder"）、`size`、`content_hash`（SHA1，`content_hash_name` 是 "sha1"）、`file_extension`）和 `next_marker`（空字串 = 沒有下一頁）。
- `POST /adrive/v1.0/openFile/getDownloadUrl`，JSON `{"drive_id", "file_id", "expire_sec": 900}` → `url`。
- 讀一段：`GET <url>`，標頭 `Range: bytes=<起>-<迄>`，要回 206 而且長度剛好是 `迄 - 起 + 1`（起是 0、迄是檔案最後一個位元組時 200 也接受）；逾時 30 秒。
- 路徑：網頁上的阿里雲盤路徑最上層是兩個虛擬資料夾「資源庫」（`resource_drive_id`）和「備份盤」（`default_drive_id`），只列帳號有的。`/資源庫/電影/某部片` 用一層一層列目錄找名稱解析（不用 `get_by_path`，它沒有實測過）。
- 帳號狀態：`status()` → `{"logged_in", "name", "drives": [...], "error"}`。登出：清掉 meta 和記憶體裡的 token。

## 115 秒傳（新模組 `embyserver/rapid115.py`）

- 常數：`APPVER_API`、`UPLOAD_INFO_API`、`UPLOAD_INIT_API`（上表的網址）、`APPVER_FALLBACK = "36.0.1"`。
- `app_version()`：讀 `APPVER_API`，快取 6 小時；讀不到用上次的，再不行用 `APPVER_FALLBACK`。
- `upload_info()`：用 115 cookie 讀 `UPLOAD_INFO_API`，快取 1 小時（換 cookie 就作廢）；`state` 假或沒有 `userkey` → 丟錯。`size_limit` 是單檔上限（數字，0 或沒有就不檢查）。
- `_init(payload)`：`kw = make_upload_payload(payload)`；`POST UPLOAD_INIT_API`，`params=kw["params"]`、`content=kw["data"]`，標頭 `Cookie`、`User-Agent: Mozilla/5.0 115Browser/<版本>`、`Content-Type: application/x-www-form-urlencoded`。HTTP 405／429 → `p115.breaker.inspect(status=…)` 後丟 `P115Throttled`；其他非 200 丟錯；`json.loads(ecdh_aes_decrypt(resp.content))`。用 `p115._client`（同一個連線池），呼叫前 `p115.breaker.check()`。
- `rapid_upload(name, size, sha1, cid, read_range) -> dict`：payload = `{"appid": 0, "appversion": 版本, "behavior_type": 0, "sign_key": "", "sign_val": "", "topupload": 0, "userid": user_id, "userkey": userkey, "filename": name, "fileid": sha1.upper(), "filesize": size, "target": f"U_1_{cid}"}`。
  1. `_init`。`status == 4` 而且訊息含「升级」：重讀版本號（不用快取）再送一次。
  2. `status == 7`：解析 `sign_check`，`read_range(起, 迄)` 讀那一段（長度不對就失敗），`sign_val = sha1(資料).hexdigest().upper()`，帶 `sign_key`、`sign_val` 再 `_init` 一次。
  3. `status == 2` → `{"state": "ok", "pickcode"}`；`status == 1` → `{"state": "missing", "message": "115 上沒有這個檔案，不能秒傳"}`；其他 → `{"state": "failed", "message": statusmsg 或 statuscode}`。
  `make_upload_payload` 會改傳進去的 dict（加 `t`、`sig`、`token`），每次送都用新的 dict。
- `class RapidJob`（dataclass，`as_dict()`）：`running`、`stopping`、`stopped`、`started`、`finished`、`error`、`current`、`source`、`folder`（115 上新建的那個資料夾路徑）、`total`、`done`、`ok`、`missing`、`failed`、`skipped`（沒有 SHA1、超過 115 單檔上限、不是影片或字幕）、`results`（每一支：`path`、`size`、`state`、`message`，最多 2000 筆，數量照算）、`unit_id`、`organizing`、`note`。
- `class RapidUploader(organizer, strm_sync, aliyun)`：115 用 `strm_sync.p115`（屬性，和 `ShareTransfer` 一樣的理由：和整理同一個帳號、測試換假 115 時一起換到）；`read(source)`（列出要秒傳的：數量、大小、沒有 SHA1 的、超過上限的，前 200 支的清單，給網頁確認）、`start(source, folder, target, target_path, media_only=True)`、`status()`、`cancel()`、`stop()`，背景用 `Workers`。
  - `start` 的檢查：115 要 cookie 登入（「秒傳要用掃碼登入（cookie）」）；阿里雲盤要登入；`folder` 不能是 `/`；`target` 是 `path` 或空字串；已經在跑就丟錯；`p115.dir_id(folder)` 找得到。
  - `_run`：
    1. 解析來源（資料夾或一支檔案），遞迴列出檔案（最多 5000 支，超過就說超過、不做）。`media_only` 時只留 `VIDEO_EXTS` 和字幕（`.srt .ass .ssa .sup .vtt .sub .idx`；字幕副檔名放進 `filetypes.py` 當 `SUBTITLE_EXTS`，`METADATA_EXTS` 裡的字幕改用它組）。
    2. 在「存到」底下建資料夾：名稱是來源的名稱（一支檔案時用去掉副檔名的檔名），已經有同名的就加「 (2)」「 (3)」…。子資料夾照來源的相對路徑一層一層建（路徑 → cid 記在字典裡，不重建）。建資料夾用 `P115Service.make_dir`（見下面）。
    3. 一支一支秒傳：每支之間看停止旗標，和上一次 115 請求至少隔 1 秒 × `p115.breaker.slowdown`。沒有 SHA1、超過上限的記成 skipped。`read_range` 是需要時才向阿里雲盤取下載網址（同一支只取一次）再讀那一段。`AliyunError`、`P115Error` 記在那一支的結果裡繼續下一支；`P115Throttled`（熔斷）就整批停下，`job.error` 寫原因。
    4. 做完：一支都沒成功 → 把剛建的資料夾（是空的才刪：先列一次確認）移到 115 回收站，`deletelog.record(db, "rapid", …)`（`deletelog.SOURCES` 加 `"rapid": "秒傳沒成功的空資料夾"`）；有成功的 → `organizer.folder_unit(cid, path)` 加進「整理 115 網盤」，`target == "path"` 時 `organizer.organize_units_in_background([unit_id], "path", target_path, cleanup=True)`，開始不了就在 `note` 說明（和轉存分享一樣的寫法）。
    5. 例外接住寫 `job.error`，`finally` 設 `running = False`。

## 共用：`P115Service.make_dir(pid, name) -> int`

轉存分享已經有建資料夾的程式（`share115.py` 的 `ShareTransfer._add_folder`：`POST /files/add`，新 id 從 `cid`／`file_id`／`data.cid`／`data.file_id` 找，都沒有就照路徑 `dir_id`）。把它搬成 `P115Service.make_dir(pid, name, path)`（只負責建一個、回傳新 id；`state` 假照樣丟 `P115Error`），`share115.py` 改用它，行為和測試都不變。秒傳要「同名就加 (2)」時另外先列一次 `pid` 挑一個沒用過的名字再建。

## 設定

- 設定檔新段落 `aliyun`：`client_id`（空）、`client_secret`（空）、`online_api`（預設 `https://api.oplist.org/alicloud/renewapi`；要以 `https://` 開頭或是空的；空 = 不用線上 API，只能用自己的 client id）。照 `webdav` 的做法加進 `config.py`（dataclass）、`settings.py`（`ALIYUN_FIELDS`、檢查）、`config_file.py`（寫回設定檔，附註解）。
- refresh token 不進設定檔（資料庫 meta）。

## 路由（新檔 `embyserver/routes/web/aliyun.py`，在 `app.py` 掛上；錯誤 → 400）

- `GET /web/api/aliyun/status`：帳號狀態。
- `POST /web/api/aliyun/token` `{"refresh_token"}`：存起來、換一次 token、讀 drive info；失敗回 400 並清掉剛存的。
- `POST /web/api/aliyun/logout`。
- `GET /web/api/aliyun/dirs?path=`：列這一層的子資料夾（給選資料夾的對話框；`path` 空或 `/` 時回「資源庫」「備份盤」）。也回這一層的檔案數，讓使用者知道選對了沒有。
- `POST /web/api/115/rapid/read` `{"source", "media_only"}`、`POST /web/api/115/rapid/start` `{"source", "folder", "target", "target_path", "media_only"}`、`GET /web/api/115/rapid/status`、`POST /web/api/115/rapid/stop`（放在 `routes/web/p115.py` 轉存分享後面）。

## 接線

- `app.py`：`app.state.aliyun = AliyunDrive(db, config.aliyun)`；`app.state.rapid = RapidUploader(app.state.p115, app.state.aliyun, app.state.organizer)`；`rapid` 加進 `stop_all`；設定改了（`aliyun.*`）要讓 `AliyunDrive` 用新的（照 `p115.set_timeout` 那種「改了就套用」的做法，或每次請求讀 config）。
- `routes/web/server.py` 的 `_busy` 加 `("秒傳", st.rapid.job.running)`。

## 網頁

- 「115 網盤」頁的子分頁加「阿里雲盤秒傳」（`data-sub="rapid"`，在「轉存分享」後面），`SUBS['115']` 加 `rapid: () => loadRapid()`。
- 卡片一「阿里雲盤帳號」：狀態（帳號名稱、有哪些盤，或沒登入、錯誤）；refresh token 輸入框 +「登入」；說明：怎麼拿 refresh token（連到 `https://api.oplist.org/`）、沒填 client id 時 token 會經過 OpenList 的服務換；`<details>` 進階：client id、client secret、線上 API 網址（`data-key="aliyun.…"`，按鈕存）；「登出」（要確認）。
- 卡片二「秒傳到 115」：
  - 來源：阿里雲盤路徑 +「選擇…」：讓既有的選資料夾對話框（`pickDir`、`pickGo`）支援第三種 `kind = 'aliyun'`，用 `GET /web/api/aliyun/dirs`。
  - 「只要影片和字幕」開關（預設開，記在瀏覽器）。
  - 「讀取」：顯示幾支影片、幾個字幕、總大小、沒有 SHA1 的、超過 115 單檔上限的，和前幾支的清單。
  - 存到（115 資料夾，`mi302-rapid-folder`）、整理到（**和轉存分享同一套選單**：把 `shDirs`／`shTargetUi`／`shTargetChange`／`shTargetPick` 抽成用前綴參數化的共用函式（例如 `targetMenu('sh')`、`targetMenu('rp')`，元素 id 照前綴、`localStorage` 鍵分開：轉存分享維持 `mi302-share-target`、`mi302-share-target-path`，秒傳用 `mi302-rapid-target`、`mi302-rapid-target-path`），兩邊一起用，不要複製一份；轉存分享的行為不變）。
  - 「開始秒傳」→ 確認框（幾支、存到哪、整理到哪；115 沒有的檔案不會上傳）→ 背景進度（做到第幾支、成功、115 沒有、失敗、略過），「停止」放進 `STOPS`。
  - 結果：失敗、115 沒有的列出原因（最多 200 筆，多的寫「還有 N 筆」）；成功後連到「整理 115 網盤」。
- 只用精確替換改 `admin.html`。

## 測試

- 新增假阿里雲盤（`httpx.MockTransport`，放 `tests/fakes.py`）：getDriveInfo、list（分頁 `next_marker`）、getDownloadUrl、下載網址的 Range 讀取、access token 過期一次（回 `AccessTokenExpired`）後換 token 成功、線上 API 換 token（回新的 refresh token，要確認有存回資料庫）。
- 115 秒傳：測試裡把 `_init` 換成假的（不測加密本身），另外一個測試確認 `_init` 送出的標頭有 `115Browser/<版本>`、`params` 有 `k_ec`（`make_upload_payload` 照常呼叫）。情境：status 2 直接成功；status 7 → 讀對的那一段（驗證 `sign_val` 是那一段的 SHA1 大寫）→ status 2；status 1 → missing；status 4「请升级到最新版本」→ 重讀版本號再送；HTTP 405 → 熔斷、整批停。
- 整批：資料夾結構照原樣建、同名資料夾加 (2)、沒有 SHA1 和超過上限的略過、一支都沒成功時刪掉空資料夾並寫刪除紀錄、成功時加進整理清單、選了整理到時呼叫 `organize_units_in_background`、停止。
- 設定：`aliyun.online_api` 不是 https 時 400；設定檔寫回有 `aliyun` 段落。
- `_busy` 有「秒傳」。

## 文件

- `docs/modules.md`：`aliyun.py`、`rapid115.py` 各一節（秒傳的流程、二次驗證、哪些是照 OpenList 寫的沒實測）。
- Wiki 三語 `115-網盤與同步`：加「從阿里雲盤秒傳」一節（為什麼要登入阿里雲盤：115 要驗證檔案中間一段；refresh token 怎麼拿、會經過 OpenList 的服務；115 沒有的不會上傳；整理到；限制）。
- Wiki 三語設定檔參考：`aliyun` 段落。技術細節三語：新的 API。

## 規則

和轉存分享的方案一樣（不 commit／push、不連伺服器、不真的打 115／阿里雲盤／MoviePilot、`admin.html` 只用精確替換、每段跑 ruff 和全部測試、回報格式）。
