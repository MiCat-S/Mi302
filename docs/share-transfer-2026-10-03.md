# 新功能：轉存 115 分享，交給 MoviePilot 整理（方案，2026-10-03）

基準：`main` = `84f2380`。

## 目標

使用者貼上別人的 115 分享連結（可以一次貼好幾個），Mi302：

1. 讀出分享裡有什麼（標題、頂層的資料夾和檔案、大小），讓使用者勾要轉存哪些（預設全勾）；
2. 轉存到使用者指定的 115 資料夾（「存到」）底下，**每個分享自己一個子資料夾**；
3. 等 115 轉存完，把那個子資料夾加進「整理 115 網盤」（和「瀏覽 115」的「整理這個資料夾…」一樣，釘在清單最上面）；
4. 使用者選了「整理到」哪個媒體庫目錄時，在背景請 MoviePilot 預覽、沒問題的直接整理（改成 MoviePilot 的命名格式、搬進媒體庫）；有問題的跳過，留在「整理 115 網盤」給使用者看。整理完，搬空的暫存子資料夾移到 115 回收站。

分享出來的命名格式五花八門，所以一定經過 MoviePilot 整理，不直接轉存進媒體庫。

## 實測過的和沒實測的

| 項目 | 狀態 | 內容 |
|---|---|---|
| `GET https://webapi.115.com/share/snap` | 實測（錯誤情況） | 存在。不存在的分享回 HTTP 200、`{"state": false, "error": "该文件分享链接不存在或已被删除", "errno": 4100026, "errtype": ""}`。`https://115cdn.com/webapi/share/snap` 一樣。 |
| `GET https://webapi.115.com/category/get?cid=&aid=1` | 實測 | `{"state": true, "count": "89", "size": "43.29GB", "folder_count": "14", "file_name": …}`：`count` 是底下（含子資料夾）的檔案數，**是字串**；`size` 是格式化過的字串，不要拿來比。 |
| MoviePilot 目錄設定（`mp.library_dirs()`） | 實測（使用者的） | 每一項的下載目錄 `storage` 都是 `local`，媒體庫目錄 `library_storage` 都是 `u115`（`/cms/电视剧/国产剧/`、`/cms/电影/华语电影/`…，`media_type` 是「电视剧」或「电影」）。所以轉存進 115 暫存資料夾的東西，**「照 MoviePilot 的目錄設定」（auto）一定整理不了**（`organize115.py` 的 `_target` 找不到包含暫存資料夾的 u115 媒體庫目錄，MoviePilot 的 `transfer_target` 也只認本機下載目錄）。要讓使用者選媒體庫目錄，用 `target="path"`。 |
| `share/snap` 成功時的欄位 | **沒實測**（照開源 p115client／OpenList） | `data.shareinfo.share_title`、`share_state`（1 正常）、`forbid_reason`、`file_size`；`data.count`；`data.list[]` 每一項：有 `fid` 的是檔案（id = `fid`），沒有的是資料夾（id = `cid`）；名稱 `n`、大小 `s`。參數 `share_code`、`receive_code`、`cid`（0 = 分享的最上層）、`offset`、`limit`。 |
| `POST https://webapi.115.com/share/receive` | **沒實測** | 表單 `share_code`、`receive_code`、`file_id`（要轉存的 id，逗號分隔）、`cid`（存到哪個資料夾）。成功 `state: true`。 |
| `POST https://webapi.115.com/files/add` | **沒實測** | 表單 `pid`（上層 id）、`cname`（新資料夾名稱）。成功時回應裡有新資料夾的 `cid`（或 `file_id`）。同名已存在時 `state: false`。 |

沒實測的介面一律**防禦式解析**：欄位缺了、型別不對不要丟 KeyError；`state` 為假時把 115 的 `error`（或 `error_msg`、`errno`）原樣顯示給使用者，和 `offline115.py` 的做法一樣。

## 使用流程（網頁）

「115 網盤」頁新增子分頁「轉存分享」（排在「離線下載」後面），網址 `#115/share`。版面照「離線下載」（`admin.html` 第 547–562 行）：

1. **連結**：textarea，一行一個分享。可以是 `https://115.com/s/swxxxx?password=abcd`、`115cdn.com`、`anxia.com` 的網址，也可以是「連結 + 訪問碼」寫在同一行（`访问码：abcd`、`提取码: abcd`、`密码 abcd`、`訪問碼`、`提取碼`、`密碼`）。
2. **讀取分享**：按了列出每個分享一塊：標題、總大小、頂層每一項（資料夾或檔案、大小）前面一個勾選框，預設全勾；讀不到的分享顯示 115 的錯誤原文。
3. **存到**：115 資料夾路徑 + 「選擇…」（`pickDir('115', …)`）。記在這個瀏覽器（`localStorage` 鍵 `mi302-share-folder`）。空白不能送。存到同步目錄裡時顯示提醒：「這個資料夾在同步目錄裡，整理之前增量同步會先替原始檔名產生 strm；建議用同步目錄外的資料夾，例如 /待整理」。
4. **整理到**：下拉選單，選項是：
   - MoviePilot 目錄設定裡存儲是 115 的每一個媒體庫目錄（名稱和路徑，例如「日番（/cms/电视剧/日番）」），來自新的 `GET /web/api/moviepilot/library-dirs`；
   - 「其他 115 資料夾…」：再出現一個路徑輸入框和「選擇…」；
   - 「先不整理（只加進「整理 115 網盤」）」。
   選擇記在這個瀏覽器（`mi302-share-target`、`mi302-share-target-path`）。MoviePilot 沒設定或讀不到目錄設定時，只剩後兩項，並說明原因。
5. **轉存**：送出勾選的。確認框寫明：幾個分享、存到哪裡、整理到哪裡；轉存不會刪任何東西。之後在背景做，卡片上顯示每個分享的進度，可以按「停止」（做完手上這一個就停）。
6. 做完每個分享顯示結果：轉存失敗（115 的原因）／已加進「整理 115 網盤」／已交給 MoviePilot 整理（連到「整理 115 網盤」看結果）。

## 後端

### 新模組 `embyserver/share115.py`

照 `offline115.py` 的寫法（模組最上面的說明寫清楚用的是 115 沒有文件的 webapi，115 改了就會失敗，錯誤照實顯示）。

- `class ShareError(Exception)`。
- `parse_links(text) -> (shares, rejected)`：一行一個；分享碼取網址 `/s/<code>`（`[0-9a-zA-Z]+`），訪問碼取 `password=<4 碼>`，或同一行的「访问码／提取码／密码／訪問碼／提取碼／密碼」後面（允許冒號、全形冒號、空白）的 4 個英數字。同一個分享碼只留一個。沒有分享碼的那一行放進 rejected。沒有訪問碼的照樣送（有些分享不用）。
- `class ShareJob`（dataclass，有 `as_dict()`，照 `organize115.py` 的 `DeleteJob`／`BatchJob`）：`running`、`stopping`、`stopped`、`started`、`finished`、`error`、`current`、`folder`、`target`、`target_path`、`shares: List[dict]`（每個分享：`code`、`title`、`state`（waiting／receiving／waiting_115／pinned／organizing／done／failed／skipped）、`message`、`path`（暫存子資料夾）、`unit_id`）。
- `class ShareTransfer`：
  - `__init__(self, p115, organizer, strm_sync)`；`self.workers = Workers()`；`self.job = ShareJob()`；`stop()`（程式結束）和 `cancel()`（使用者按停止：做完手上這一個就停）。
  - `read(text) -> dict`：解析連結，逐一 `snap`，回傳 `{"shares": [{code, receive_code, title, size, count, items: [{id, name, is_dir, size}], error}], "rejected": [...]}`。每個分享之間至少隔 1 秒 × `p115.breaker.slowdown`（呼叫前 `p115.breaker.check()`）。分享最上層超過一頁時用 `offset` 翻頁，最多列 1000 項。
  - `start(shares, folder, target, target_path) -> dict`：參數檢查（要用 cookie 登入，沒有就丟 `ShareError("轉存分享要用掃碼登入（cookie）")`；`folder` 不能是 `/`；`target` 是 `path` 或空字串（空 = 先不整理）；`path` 時 `target_path` 不能是 `/`）；已經在轉存就丟錯；用 `p115.dir_id(folder)` 找到「存到」的 id（找不到就說找不到，不自動建立）；建好 `ShareJob` 後 `self.workers.start(self._run, ...)`。
  - `_run`：一個分享一個分享做，每個之間看停止旗標、隔 ≥1 秒 × slowdown：
    1. 建暫存子資料夾：名稱 = 分享標題清理過（去掉 `/ \ : * ? " < > |` 和控制字元、頭尾空白和點，最多 100 字；空的用分享碼）。`POST /files/add`；同名已存在（`state` 假）就改成「名稱 (分享碼)」再建一次；還是失敗就這個分享 failed。
    2. `POST /share/receive`（`file_id` = 勾選的 id 用逗號接起來，`cid` = 暫存子資料夾）。115 回錯誤 → failed，訊息照原文。
    3. 等 115 轉存完：每 3 秒 × slowdown 用 `list_dir(暫存 cid)` 看勾選的頂層名稱是否都出現；都出現後，用 `GET /category/get?cid=&aid=1` 讀 `count`，隔 10 秒再讀一次，兩次一樣才算完成。最多等 10 分鐘（常數），超過就這個分享 failed：「115 還在轉存，等它做完到『瀏覽 115』把 <路徑> 加進整理」。
    4. 加進「整理 115 網盤」：`organizer.folder_unit(cid, path)`（它會問 MoviePilot 名稱）；記下回傳的 `id` 當 `unit_id`。`OrganizeError` → failed（訊息照原文），但轉存已經成功，訊息要說檔案在哪裡。
    5. 所有分享做完後，`target == "path"` 而且有成功釘上的：呼叫 `organizer.organize_units_in_background(unit_ids, "path", target_path, cleanup=True)`；開始了就把那些分享標成 organizing／done（並說明到「整理 115 網盤」看結果）；因為已經有別的整理在跑而開始不了，就標成 pinned，訊息「已加進『整理 115 網盤』；目前有別的整理在跑，等它做完再按『全部整理』」。`target` 空 → pinned。
    6. 背景執行緒的例外要接住寫進 `job.error`，`finally` 把 `running` 設回 False（照 `_organize_all`）。
  - `status() -> dict`：`self.job.as_dict()`。
  - 115 的請求都走 `p115._webapi_get`／`p115._webapi_post`（會檢查限流、登入失效並熔斷；`state` 假時丟 `P115Error`，訊息裡有 115 的 `error`）。`category/get` 也一樣。

### `embyserver/organize115.py`

- 把 `organize_all`（第 1110–1139 行）裡「檢查能不能開始、建 `BatchJob`、`reorg.hold()`」那一段抽成 `_start_batch(units, held_count, target, target_path, cleanup, limit) -> dict`，`organize_all` 改成呼叫它，**行為不變**（現有測試要全過）。
- 新增 `organize_units_in_background(unit_ids, target, target_path, cleanup=True) -> Optional[dict]`：從 `self._pinned` 取出這些 id 的 Unit（找不到的略過），交給 `_start_batch`（不套「先不整理」、不套影片數上限）；開始不了（已經在全部整理、正在刪除、正在問 MoviePilot 檢查、`reorg.hold()` 失敗、沒登入 115）時**不丟錯**，回傳 `None` 並記日誌。
- 注意 `MAX_PINNED = 20`：一次轉存超過 20 個分享時，前面的會被擠出釘選清單；`organize_units_in_background` 收到的 id 若已不在 `_pinned` 就略過，`ShareTransfer` 要把那些分享的訊息寫成「已轉存到 <路徑>，釘選清單滿了，請到『瀏覽 115』加進整理」。
- `Unit.cleanup_folders()`（第 199 行）對資料夾 Unit 回傳它自己，所以 `cleanup=True` 整理完會把搬空的暫存子資料夾移到回收站（還有影片的不動），不用另外處理。

### `embyserver/moviepilot.py` 和 `routes/web/moviepilot.py`

- `GET /web/api/moviepilot/library-dirs`（要管理員）：`mp.library_dirs()` 裡 `library_storage == "u115"` 而且有 `library_path` 的，回傳 `{"dirs": [{"name", "path"（去掉結尾的 /，開頭補 /）, "type"（media_type 原文）}]}`，同一個路徑只留一個。MoviePilot 沒設定 → `{"dirs": [], "error": "還沒設定 MoviePilot"}`；讀不到 → `{"dirs": [], "error": 原因}`（不要回 5xx，網頁照樣能用「其他 115 資料夾」）。

### 路由 `embyserver/routes/web/p115.py`

放在雲下載後面，錯誤處理照 `_offline`（第 31 行）另寫一個 `_share`（`ShareError`、`P115Error` → 400）：

- `POST /web/api/115/share/read` `{"text": "..."}` → `ShareTransfer.read`（在 threadpool 跑）。
- `POST /web/api/115/share/start` `{"shares": [{"code", "receive_code", "title", "ids": [...]}], "folder", "target": "path" | "", "target_path"}` → 開始，回傳 job。`ids` 是字串（115 的 id 有 19 位，網頁不要轉成數字）。
- `GET /web/api/115/share/status` → job。
- `POST /web/api/115/share/stop` → 做完手上這一個就停。

### 接線

- `app.py`：`app.state.share = ShareTransfer(app.state.p115, app.state.organizer, app.state.strm_sync)`（在 organizer 建好之後）；加進第 118 行 `stop_all([...])` 的清單。
- `routes/web/server.py` 的 `_busy`（第 19–29 行）加 `("轉存分享", st.share.job.running)`。

## 網頁 `embyserver/web/admin.html`

- 第 430 行的子分頁列加 `<button data-act="sub" data-sub="share" role="tab" aria-selected="false">轉存分享</button>`（在「離線下載」後面）。
- 新增卡片 `data-sub="share"`（放在離線下載的兩張卡片後面），內容見上面「使用流程」。說明文字附 Wiki 連結 `https://github.com/MiCat-S/Mi302/wiki/115-網盤與同步#轉存分享`。
- 第 1144 行 `SUBS['115']` 加 `share: () => loadShare()`：恢復記住的「存到」「整理到」，讀 `library-dirs` 填下拉選單，讀 `share/status` 顯示上一次的結果，跑著就輪詢（`later('share', pollShare)`，分頁在背景時 `later` 會自己延後）。
- 第 3209 行 `STOPS` 加 `share: ['/web/api/115/share/stop', {}, '停止轉存？', '做完手上這一個分享就停；已經轉存的留在 115，沒做的不做。', () => pollShare()]`，停止按鈕照其他卡片的寫法。
- 動態產生的按鈕用 `data-act`（全頁共用的 click 監聽，見第 1035 行的說明），名稱、路徑一律 `esc()`。
- 115 的 id 一律當字串處理。
- 只用精確替換改這個檔案，不要整個重寫。

## 測試

- `tests/fakes.py` 的 `Fake115` 加：`/share/snap`（照上表的成功欄位；不存在的分享回 `{"state": false, "error": "该文件分享链接不存在或已被删除", "errno": 4100026}`）、`/share/receive`（把勾選的項目建進目標資料夾，讓之後的 `/files` 列得到）、`/files/add`（同名回 state 假）、`/category/get`（回 `count` 字串）。
- 新檔 `tests/test_share115.py`（或併進最接近的現有測試檔，照 `docs/` 的慣例：共用假物件放 `tests/fakes.py`）：
  - `parse_links`：網址帶 password、同一行寫訪問碼（簡繁、全形冒號）、沒有訪問碼、重複、看不懂的行。
  - 讀分享：成功、分享不存在（錯誤原文出現在結果裡）。
  - 轉存：成功 → 子資料夾建好、項目轉進去、加進整理清單（`organizer.list()` 的 `pinned` 有它）；同名資料夾已存在 → 用「名稱 (分享碼)」；receive 失敗 → failed、訊息照原文；115 一直沒出現項目 → 逾時 failed（把等待常數在測試裡調小）。
  - 選了整理到：`organize_units_in_background` 被呼叫；已經在全部整理時不開始，分享標成 pinned 並有說明。
  - `organize_all` 抽出 `_start_batch` 後現有的整理測試全過。
  - `GET /web/api/moviepilot/library-dirs`：只回 u115 的、MoviePilot 沒設定時回空清單和原因。
  - `_busy` 有「轉存分享」。
  - 沒登入 cookie 時 `start` 回 400。

## 文件

- `docs/modules.md`：加 `embyserver/share115.py` 一節（流程、等 115 轉存完的判斷、和 organizer 的關係、沒實測的介面），`organize115.py` 那節補 `organize_units_in_background`。
- Wiki 三語 `115-網盤與同步.md`／`115-网盘与同步.md`／`115-Cloud-Sync.md`：在「離線下載」一節後面加「轉存分享」一節（怎麼用、存到哪裡、為什麼要選「整理到」而且「照 MoviePilot 的目錄設定」不適用、停止、限制：要掃碼登入、115 沒有文件的介面、釘選清單最多 20 個）。英文標題用 `Transfer shares`，括號附繁體原文。
- Wiki 三語 `技術細節與開發.md`／`技术细节与开发.md`／`Technical-Details.md` 的 API 表加 `/web/api/115/share/*` 和 `/web/api/moviepilot/library-dirs`。
- 沒有新的設定鍵，設定檔參考不用改。

## 給實作者的規則

- 工作目錄：`/Users/cat/Documents/Claude/Mi302/.claude/worktrees/mi302-code-review-b130c3`。
- 不要 `git commit`／`push`／`stash`／`reset`／`checkout`；不要連伺服器、不要跑 `termark`；不要真的打 115 或 MoviePilot（測試全用假的）。
- `embyserver/web/admin.html` 有 3,950 行：只用精確替換，不要整個覆寫；用行號範圍讀。
- 風格照周圍：繁體中文的註解和訊息、一行說清楚為什麼；長的模組說明放 `docs/modules.md`，檔案開頭只留一句和指向它的話（照 `organize115.py`、`deletelog.py`）。共用的測試假物件放 `tests/fakes.py`。ruff 的複雜度限制（C901）要過，太長就拆函式。
- 每一段做完跑：
  - `VENV=/private/tmp/claude-501/-Users-cat-Documents-Claude-Mi302--claude-worktrees-mi302-code-review-b130c3/9c07e3cd-16bc-4bfe-a210-006563efa19d/scratchpad/venv`
  - `$VENV/bin/ruff check embyserver tests moviepilot-plugin`
  - `$VENV/bin/python -m pytest -q -p no:cacheprovider`（全部要過；`tests/test_admin_page.py` 用 `node --check` 檢查網頁腳本語法）
- 做完回報：改了哪些檔案（`git diff --stat`）、新增的端點和函式、ruff 和 pytest 最後幾行、哪些地方是照「沒實測」的欄位猜的、拿不準或沒照方案做的地方（寫原因）。
