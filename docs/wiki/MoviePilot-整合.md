**繁體中文** | [简体中文](MoviePilot-集成) | [English](MoviePilot)

Mi302 不自己刮削，這件事交給 [MoviePilot](https://github.com/jxxghp/MoviePilot)。這一頁說明怎麼連上 MoviePilot、哪些影片會送去刮削、補全缺集怎麼判斷缺哪幾集，以及怎麼讓 MoviePilot 把 Mi302 當成 Emby 媒體伺服器。

Mi302 是照 MoviePilot V3 做的。舊版大多能用，但有幾項功能會打折扣，見[舊版 MoviePilot](#舊版-moviepilot)。

## 運作方式

Mi302 只讀資料夾裡已經有的 nfo 和圖片。這些資料可以在 115 同步時一併下載（見[115 網盤與同步](115-網盤與同步)），或交給 MoviePilot 刮削：

1. 同步產生新的 strm 後，Mi302 先掃描有變動的地方，新片馬上出現在播放器裡（這時還沒有海報）。
2. Mi302 把新 strm 的路徑送給 MoviePilot 的刮削 API（`POST /api/v1/media/scrape/local`）。
3. MoviePilot 辨識影片，到 TMDB 等來源查資料，把 nfo、海報、背景圖寫進同一個資料夾。
4. 刮削完成後，Mi302 只重新掃描刮削過的地方，播放器就看得到海報和簡介。

所以 MoviePilot 和 Mi302 要看得到同一批檔案，例如裝在同一台機器，或掛載同一個網路磁碟。

## 連線設定

設定在「MoviePilot」分頁的「連線」卡片。

1. 在 MoviePilot 的「設定 → 系統」複製 **API 令牌**。
2. 在 Mi302 填「MoviePilot 網址」（例如 `http://192.168.1.10:3000`，要以 `http://` 或 `https://` 開頭）和「API 令牌」。
3. 兩邊看到的路徑不一樣時，填「路徑對應」，見下一節。
4. 按「測試連線」。它會先儲存，再測試。

「測試連線」只檢查網址和 API 令牌。它送一個空路徑給刮削 API，MoviePilot 會回「刮削路径无效」，所以不會真的刮削。路徑對應對不對，要看第一次刮削的結果。卡片右上角平常顯示「已設定」或「未設定」，測試後顯示「連線成功」或「連線失敗」。

| 網頁上的名稱 | 設定鍵 | 預設值 | 說明 |
|---|---|---|---|
| MoviePilot 網址 | `moviepilot.url` | 空 | 結尾的 `/` 會去掉 |
| API 令牌 | `moviepilot.api_token` | 空 | 放在 `X-API-KEY` 標頭和 `token` 查詢參數送出 |
| 同時刮削幾項 | `moviepilot.concurrency` | 3 | 1–8 |
| 路徑對應 | `moviepilot.path_mappings` | 無 | 見下一節 |
| 同步產生新的 strm 後自動送去刮削 | `moviepilot.scrape_after_sync` | 開 | 關掉時同步完只掃描 |
| MoviePilot 帳號、MoviePilot 密碼 | `moviepilot.username`、`moviepilot.password` | 空 | 補全缺集需要；舊版刮削 API 也需要 |
| 全量同步後自動補全 | `moviepilot.fill_after_full_sync` | 關 | 在「補全缺集」卡片，切換後馬上儲存 |
| （只在設定檔） | `moviepilot.timeout` | 300 | 每一項最多等幾秒；超過時當成連線失敗，這一批停下 |

### 什麼時候要填帳號密碼

帳號密碼在「連線」卡片裡摺起來的「MoviePilot 帳號密碼（補全缺集需要；舊版刮削 API 也需要）」，按一下展開。

- **補全缺集**：MoviePilot 建訂閱的 API 只接受帳號登入，不接受 API 令牌，一定要填。
- **舊版 MoviePilot**：刮削 API 只接受登入。「測試連線」出現「拒絕存取」時，填帳號密碼。

填了帳號密碼時，MoviePilot 回 401 或 403，Mi302 會用帳號密碼登入（`POST /api/v1/login/access-token`）再試一次，之後都用登入拿到的 token；token 過期了會自動重新登入。只填網址和帳號密碼、不填 API 令牌也可以。

## 路徑對應

Mi302 送給 MoviePilot 的是 Mi302 看到的檔案路徑。兩邊看到的路徑一樣（例如裝在同一台機器）就不用填。不一樣時，在「路徑對應」一行填一條：

```
Mi302 的路徑 => MoviePilot 的路徑
```

例子：

- Mi302 在 Parallels 虛擬機裡看到 `/media/psf/Vo`，MoviePilot 裝在 Mac 上看到 `/Volumes/Vo`：填 `/media/psf/Vo => /Volumes/Vo`。
- MoviePilot 用 Docker：看它容器裡的掛載路徑。主機的 `/volume1/media` 掛成 `/mnt/media`，就填 `/volume1/media => /mnt/media`。

規則：

- 比對路徑開頭的整段資料夾，多條都符合時用最長的那條。
- 反方向也用同一組規則：MoviePilot 通知 Mi302 重新掃描時送來的是 MoviePilot 的路徑，Mi302 會換回自己的路徑。
- MoviePilot 回報檔案不存在時，「刮削」卡片的錯誤裡會寫「MoviePilot 找不到 …，請檢查路徑對應」，後面是它收到的路徑。

設定檔裡的寫法：

```yaml
moviepilot:
  path_mappings:
    - from: /media/psf/Vo
      to: /Volumes/Vo
```

## 送出規則

同步後自動送出的只有這次新產生的 strm。按「刮削」卡片的「刮削缺少資料的項目」時，看整個媒體庫。兩種都照這些規則決定送什麼：

- **電影**：送 strm 檔本身。已經有同名 nfo（`X.nfo`），或電影放在自己的資料夾、裡面有 `movie.nfo` 的不送。
- **劇集**：整部劇還沒有 `tvshow.nfo` 時，送整個劇集資料夾，一次處理劇、季、集。已經有 `tvshow.nfo` 的劇，只送沒有 nfo 的那幾集。
- 已經有 nfo 的不送，避免覆蓋從 115 帶下來或之前刮好的資料。
- 按「刮削缺少資料的項目」時，有 nfo 卻沒有劇照的集也會再送一次（30 天內確定沒有劇照的除外，見下方）。
- 連線失敗、認證失敗、MoviePilot 回 HTTP 錯誤或逾時，整批停下，還沒送的算失敗，原因顯示在「刮削」卡片上。

劇集資料夾的判斷和掃描時一樣，媒體庫底下可以有分類資料夾（見[媒體庫與掃描](媒體庫與掃描)）。例如媒體庫路徑選 `电视剧`、底下是 `国产剧/庆余年 (2019)/Season 1/…`，送出的是 `庆余年 (2019)` 這部劇，tmdbid 也從它的 `tvshow.nfo` 取。

## 刮削速度

MoviePilot 的刮削 API 是同步的：每一項都要等它到 TMDB 查資料、下載圖片才回應，一部片可能要幾十秒。Mi302 這樣加快：

- **同時送好幾項**：「同時刮削幾項」預設 3，最多 8。太多時 TMDB 可能限速；MoviePilot 日誌出現 429 就調低。
- **已經刮削過的劇帶上 tmdbid**：送單集時，Mi302 從劇的 `tvshow.nfo` 取出 tmdbid，直接告訴 MoviePilot 是哪一部（`media_source=themoviedb&media_id=…`）。MoviePilot 不必再用檔名搜尋 TMDB，也不會認錯。這要 MoviePilot V3；舊版會忽略這些參數，照常用檔名辨識。

MoviePilot 本身慢的話，多半是連 TMDB 慢：在 MoviePilot 設定 TMDB 的 API 網址和圖片網址代理。

## 刮削結果

「刮削」卡片顯示上次刮削（同步後自動或手動）的數字：送出、成功、失敗、沒有劇照。

MoviePilot 回報完成後，Mi302 會看它有沒有真的寫出 nfo：

- 送單一檔案時看 `X.nfo`，送劇集資料夾時看 `tvshow.nfo`。沒寫出來就算失敗。
- MoviePilot 認不出集數時什麼都不寫，卻回報完成。這種集會記成失敗，並說明原因：檔名要有 `S01E01` 這類集號。
- 劇集的一集有 nfo、沒有劇照時算成功，另外計入「沒有劇照」。

### 單集沒有劇照

MoviePilot 的單集圖片只來自 TMDB 那一集的劇照，存成和影片同名的 `X.jpg`（Mi302 也認 `X-thumb.jpg`）。沒有圖片通常是：

1. **TMDB 沒有這集的劇照**：國產劇、綜藝、剛播出的集很常見，MoviePilot 也寫不出來。這種集 Mi302 改用劇的橫幅圖（`thumb`、`landscape`），沒有就用背景圖，播放器不會一片空白。
2. **MoviePilot 下載圖片失敗**：MoviePilot 日誌有「图片下载失败」，多半是連不到 `image.tmdb.org`，要在 MoviePilot 設定 TMDB 圖片代理。修好之後按「刮削缺少資料的項目」，有 nfo 卻沒有劇照的集會再送一次。

送過、確定沒有劇照的集，30 天內按「刮削缺少資料的項目」不再重送。

## 補全缺集

媒體庫裡的劇少了幾集時，可以讓 MoviePilot 去下載補齊。設定在「MoviePilot」分頁的「補全缺集」卡片。

### 事前準備

1. 把 Mi302 加成 MoviePilot 的媒體伺服器（見下方「讓 MoviePilot 把 Mi302 當成 Emby」），MoviePilot 才知道哪些集已經有了。
2. 在「連線」卡片填 MoviePilot 的帳號密碼並儲存。
3. 劇要先刮削過，`tvshow.nfo` 裡有 tmdbid。沒有 tmdbid 的劇不會送。

### 卡片上的劇集清單

「補全缺集」卡片列出媒體庫裡所有的劇和每一季有幾集：

- 集號有空洞的季（例如有第 2、4 集，沒有第 3 集）各佔一行，標出缺幾集、缺哪幾集。有空洞的劇排在前面。
- 在「搜尋劇名」輸入就能搜尋，片名、原名、年份、拼音、首字母都認。勾選「只看集號有空洞的」只列有空洞的劇。
- 「全部年份」下拉選單可以只看某一年的劇，選單裡只列媒體庫裡有的年份。
- 每頁顯示幾部可以選 20、50、100 或 200 部，瀏覽器會記住。清單下方顯示第幾頁、共幾部，用「上一頁」「下一頁」翻頁。搜尋、換年份或換篩選時回到第 1 頁。
- 每部劇右邊的「補全」只送那一部。沒有 tmdbid 的劇標著「沒有 tmdbid，要先刮削」，按鈕按不了。
- 卡片右上角的「全部補全」送出所有有 tmdbid 的劇，會先問一次。
- 特別篇（第 0 季）不列、不送。

「集號有空洞」只是提示。最後幾集沒下到的情況，下面的檢查一樣算得出來。

### 每一季怎麼判斷

補全一季一季來，只處理媒體庫裡已經有集的季。TMDB 上有、媒體庫整季都沒有的季不會自動訂閱。

1. 向 MoviePilot 查 TMDB 上這一季的集和播出日期（`GET /api/v1/tmdb/{tmdbid}/{季}`）。
2. 判斷哪些集已經播出：
   - 有播出日期的，日期不晚於今天（Mi302 主機的日期）就算播過。
   - 沒有日期的常是還沒播的佔位集。集號不超過媒體庫裡這一季最後一集的算播過（都有第 10 集了，第 3 集一定播過）。
   - 在最後一集之後、又沒有日期的不確定，不算缺。結果裡會註明有幾集 TMDB 沒有播出日期、沒算進去。
3. 對照 Mi302 裡這一季已有的集號。已播出的都有，就不建訂閱，記成「已經齊全」。
4. 缺集才建訂閱（`POST /api/v1/subscribe/`），用 tmdbid 指定是哪一部（V3 的 `media_source`／`media_id`），不靠劇名，不會認錯。
5. 立刻請 MoviePilot 搜尋這條訂閱（`POST /api/v1/subscribe/search/{訂閱 id}`）。MoviePilot V3 建訂閱後只是安排搜尋，有時要等到定時搜尋才開始。之前就訂閱過的，也會請它再搜一次。這一步失敗時，結果裡會註明，MoviePilot 會在定時搜尋時處理。

查不到 TMDB 的集數時，照樣建訂閱，交給 MoviePilot 判斷。

MoviePilot 下載、整理完會通知 Mi302 重新掃描。缺集的季如果還在更新，訂閱會跟著追新集。

### 結果

卡片上的數字：

| 名稱 | 意思 |
|---|---|
| 建訂閱並搜尋 | 有缺集，建了訂閱並請 MoviePilot 搜尋的季數 |
| 缺的集數 | 所有季缺的集數合計 |
| 已經齊全 | 已播出的集都有、沒建訂閱的季數；舊版 MoviePilot 回「媒体库中已存在」拒絕時也算這裡 |
| 之前訂閱過 | MoviePilot 說訂閱已經存在的季數（也請它再搜了一次） |
| 沒 tmdbid | 沒有 tmdbid 而略過的劇數 |
| 失敗 | MoviePilot 不接受訂閱，或中途停下後沒做的季數 |

展開「每一季的結果（N）」可以看每一季缺哪幾集、MoviePilot 怎麼回。連線或認證失敗時整批停下，剩下的季算失敗。同一時間只跑一批補全；已經在跑時再按，會提示「已經在補全中，等它做完」。

### 全量同步後自動補全

勾選「全量同步後自動補全」（預設關閉，切換後馬上儲存）時，每次全量同步、刮削完之後，自動把所有有 tmdbid 的劇送一次。沒填帳號密碼時不做。等刮削完才做，是因為新刮削的劇到這時才有 tmdbid。

## 讓 MoviePilot 把 Mi302 當成 Emby

MoviePilot 可以把 Mi302 加成媒體伺服器，用來判斷片子是否已經有了，整理完自動通知 Mi302 重新掃描。

1. 在 Mi302「MoviePilot」分頁的「讓 MoviePilot 把 Mi302 當成 Emby」卡片，輸入用途（例如 `MoviePilot`，不填就叫 MoviePilot），按「建立 API 金鑰」。在列表裡按複製圖示複製金鑰。
2. 在 MoviePilot 的「設定 → 媒體伺服器」新增 Emby。地址填卡片上顯示的「地址」（就是你開管理網頁用的網址，例如 `http://192.168.1.20:8096`），API 金鑰貼上剛才那把。MoviePilot 要連得到這個地址。

API 金鑰以管理員身分呼叫 Emby API，但不能用來操作管理網頁。不用了可以在列表裡刪掉；用這把金鑰的程式會連不上。

MoviePilot 通知 Mi302 某些檔案有變動時（`POST /Library/Media/Updated`），Mi302 依「路徑對應」把路徑換回自己的路徑，只掃那些檔案所在的電影或劇，不重掃整個媒體庫。通知裡沒有路徑時，才掃整個媒體庫。

## 媒體庫封面

播放器首頁每個媒體庫的封面，可以用 MoviePilot 的媒體庫封面插件產生，例如 [wio-ki/MoviePilot-Plugins](https://github.com/wio-ki/MoviePilot-Plugins) 的「Emby媒体库封面生成」：

1. 先照上一節把 Mi302 加成 MoviePilot 的 Emby 媒體伺服器。
2. 安裝插件，在插件設定的媒體伺服器選 Mi302，選要產生封面的媒體庫。
3. 在插件裡手動執行一次，或設定排程（例如每天一次）。

插件從媒體庫裡隨機挑海報組成封面，再上傳到 Mi302（`POST /Items/{id}/Images/Primary`）。上傳的封面：

- 存在資料目錄（`server.data_dir`）的 `images/` 裡，重新掃描不會被蓋掉，也比媒體庫資料夾裡的 `poster`／`folder`／`cover` 圖優先。
- 在「媒體庫」分頁看得到。也可以按「上傳封面」自己上傳，或按「改回預設」刪掉上傳的封面。見[媒體庫與掃描](媒體庫與掃描)。

插件的「入库监控」要 MoviePilot 整理完成或 Emby 的新增通知才會觸發。Mi302 從 115 同步進來的檔案不經過這兩個，所以新片進來後封面不會自動更新，請用排程或手動更新。

## 舊版 MoviePilot

Mi302 是照 MoviePilot V3 做的。在舊版（例如 V2）上：

- 刮削 API 可能不接受 API 令牌：要填帳號密碼。
- 刮削時帶的 tmdbid 參數會被忽略，照常用檔名辨識，比較慢，也可能認錯。
- 建訂閱：舊版認 `tmdbid` 欄位，V3 認 `media_source`／`media_id`，Mi302 兩種都送。舊版遇到媒體庫已經有的會拒絕（「媒体库中已存在」），Mi302 記成「已經齊全」。
- 請 MoviePilot 搜尋訂閱：V3 用 POST，舊版用 GET。收到 HTTP 405 時，Mi302 改用 GET 再試。
- 查 TMDB 集數：V3 和舊版回的格式不同，兩種都認。
- 碰到 MoviePilot 沒有的 API（HTTP 404），Mi302 會顯示「MoviePilot 沒有這個 API（…），請確認網址或升級 MoviePilot」。

## 其他用到 MoviePilot 的地方

「演職人員顯示中文名」開啟時，Mi302 經 MoviePilot 查 TMDB 人物的別名（`GET /api/v1/tmdb/person/{id}`），挑出中文名。見[媒體庫與掃描](媒體庫與掃描)。
