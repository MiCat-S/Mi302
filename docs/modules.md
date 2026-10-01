# 模組說明

程式碼裡比較長的行為說明集中在這裡；每個模組開頭只留一句摘要，指到這份文件的對應段落。
給改程式的人看：做了什麼、為什麼這樣做、哪些情況要特別小心。使用者看的說明在 [Wiki](wiki/Home.md)。

改了模組的行為時，這裡的說明一起改。

## embyserver/strm_sync.py

從 115 目錄產生 strm（以及下載 nfo／圖片／字幕）。

兩種同步方式：
- 全量：比對任務目錄裡所有檔案；可以刪除 115 上已不存在的 strm。
  1. 用 115 的「導出目錄樹」一次拿到所有資料夾的路徑（只有名稱）；
  2. 一次列出任務目錄底下所有檔案（有 pickcode、大小、所在資料夾 id，每頁 1150 個）；
  3. 用資料夾裡的檔名對出「資料夾 id → 路徑」，對不上的少數資料夾再個別查詢。
  不必逐層列出每個資料夾，資料夾多時快很多。導出、列檔案或查路徑失敗（例如只用開放平台登入、
  115 正在跑別的導出任務）時改回逐層列目錄；目錄樹裡有、115 卻沒列出來的影片不當成已刪除。
  同時記下每個 115 檔案、資料夾對應到哪個本機路徑（資料表 p115_index），給增量同步用。
- 增量：
  1. 讀 115 生活事件（網盤的操作紀錄）：上傳、接收、複製、移動、改名、刪除。
     生活事件只給檔案 id，靠 p115_index 找到本機原本的位置；移動、改名時把 strm 連同
     同名的 nfo、海報（例如 MoviePilot 刮削的）一起搬過去，刪除時跟著刪（要開 delete_stale）。
  2. 再請 115 依修改時間列出任務目錄裡最近修改的檔案，補抓沒有產生事件的上傳
     （例如離線下載、第三方工具上傳）。
  生活事件需要掃碼或 cookie 登入，只用開放平台時只做第 2 步。任務還沒全量同步過、
  或事件超出 115 保留的範圍時，自動改跑全量；定期全量（預設每週）查漏補缺。

本機的 strm 或資料夾跟著 115 搬的時候（_move_file、_move_dir），媒體庫裡的項目和媒體資訊一起改路徑（_repath），
項目的 id 不變：項目是用路徑認的，不改的話掃描會把舊路徑的項目刪掉、新路徑當成新項目，觀看紀錄就不見了。

## embyserver/p115_open.py

115 開放平台（open.115.com）通道。

與 cookie 通道的差別：用 OAuth PKCE 裝置碼授權取得 access_token（約 2 小時）與 refresh_token，
呼叫 proapi.115.com/open/* 官方授權介面，比較不容易被風控或被其他登入踢下線。
需要先在 115 開放平台申請應用，取得 AppID（client_id）。

授權流程：
1. POST passportapi.115.com/open/authDeviceCode（client_id + code_challenge）→ uid、time、sign
2. 用 uid 顯示二維碼，輪詢 qrcodeapi.115.com/get/status/
3. 確認後 POST passportapi.115.com/open/deviceCodeToToken（uid + code_verifier）→ token
4. 過期前 POST passportapi.115.com/open/refreshToken 續期

## embyserver/webdav.py

/dav/ 的 WebDAV（只能讀）：Infuse、VidHub、Kodi 這類播放器直接瀏覽 115，播放時 302 到 115 直鏈，不必產生 strm。

- 路徑就是 115 上的完整路徑：/dav/影視/電影/xxx.mkv 是 115 的 /影視/電影/xxx.mkv。
- 只露出 webdav.root（空的 = 同步任務的 115 目錄）底下；它們的上層資料夾只列出通往它們的那一層（虛擬的）。
- 列目錄向 115 請求，每個資料夾的內容快取 2 分鐘；子資料夾的 id 從上一層的清單拿，不必每層都查路徑。
  記住的 id 也只算 2 分鐘，上一層重新列時不見了的子資料夾連同底下的 id 一起忘掉：在 115 上被移出露出範圍的資料夾，
  舊路徑最多 2 分鐘就找不到，不會靠記住的 id 繼續列它裡面的東西。
  同步索引沒有檔案大小和 pickcode，所以不能只查本機。
- 播放和 strm 一樣：用播放器自己的 User-Agent 向 115 取直鏈，302 過去（115 的直鏈綁 UA）。
- 登入用 HTTP Basic（Mi302 的帳號密碼）。密碼雜湊算一次要約 0.1 秒，播放器每個請求都帶，驗過的記 5 分鐘。

## embyserver/prober.py

用 ffprobe 探測 strm 指向的影片，寫出 X-mediainfo.json 並存進資料庫。

做法參考 xiao-vvv/emby-mediainfo（MIT）：
- 取直鏈用一般瀏覽器的 UA（115 的直鏈通常綁定取得時的 UA；115Browser 的 UA 會被 CDN 要 cookie）。
- 網路上的影片不讓 ffprobe 自己去讀：它會開好幾條連線來回跳著讀（mp4 的 moov 常在檔尾），115 的 CDN
  常常拒絕，結果是「moov atom not found」「Invalid data found」。改成 Mi302 用一條連線、一段一段讀
  需要的部分（檔頭幾 MB；mp4 照 box 找到 moov；其他格式讀檔尾一段），帶著取直鏈的 UA 和 cookie，
  寫進和原檔一樣大的稀疏暫存檔（沒讀的地方不佔空間），ffprobe 讀這個本機檔。
  115 回的不是影片（錯誤網頁、空的）時，錯誤訊息直接寫出 115 回了什麼。
- 伺服器不支援分段讀取（Range）時，才照舊讓 ffprobe 直接讀網址。
- 115 同時最多 3 條連線；取直鏈有全域間隔（用單調時鐘，系統時間往回跳也不會卡住）；
  被限流就熔斷（P115Service.breaker），剩下的這次先不做。
- 錯誤訊息裡的直鏈網址抹掉，不寫進日誌。

## embyserver/mediainfo.py

媒體資訊（解析度、HDR、音軌、字幕軌、章節）：ffprobe 結果 → Emby 的 MediaSourceInfo。

存在影片旁邊的 X-mediainfo.json，格式和 Emby 神醫助手（StrmAssistant）「媒體資訊持久化」一樣：
[{"MediaSourceInfo": {...}, "Chapters": [...]}]。所以用過神醫、emby-mediainfo 產生的檔案可以直接讀，
Mi302 自己探測的結果 Emby 那邊也能共用。讀到的結果另外存一份在資料表 media_info，回給播放器時不必讀檔。

ffprobe → MediaSourceInfo 的對照改寫自 xiao-vvv/emby-mediainfo 的 app/mapper3.py，
規則來自大量神醫產出的統計，並逐欄位對過帳。原專案授權：

    MIT License

    Copyright (c) 2026 xiao-vvv

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

## embyserver/dupes.py

115 上重複的影片：找出來、建議保留哪一份、刪掉多的。

兩種重複，一次「找重複」同時找：
- 完全相同（exact）：SHA1 和大小都一樣。115 列檔案時本來就附上 SHA1，四萬多個檔案大約四十次請求。
  建議保留本機有 strm 的那份（刮削資料、觀看紀錄都在它身上），其次檔名編號格式完整的，再來最早上傳的；
  其餘預先勾選。
- 不同版本（versions）：媒體庫裡同一部電影（tmdbid，沒有就片名＋年份）或同一部劇的同一集，檔案卻不同，
  例如 1080p 和 2160p。只看同步任務裡、有 strm 的檔案。不知道第幾集的、檔名集號和媒體庫對不上的不比；
  分段檔（CD1、Part 2）、一個檔案好幾集的不算；
  導演剪輯版、加長版這類版本名不同的分開算。建議保留哪一份看使用者選的偏好（預設 1080P 優先，沒有再 4K，
  再沒有就留剩下最高的），解析度一樣時保留檔名編號格式完整的，再來檔案小的（省空間）；預設不勾，由使用者挑。
  改偏好時已找到的結果當場重算。

檔名編號格式完整：劇集要有 S01E02（或 1x02）這種季和集都寫明的編號，電影要有年份。

大檔案（big）：同一次找重複順便記下 1 GB 以上的影片（115 列檔案時就有大小，不多花請求），可以照大小、
電影或劇集篩選，挑了才刪。不必留一份；同一部片還有別的版本（或完全相同的另一份）時，觀看紀錄轉過去。

刪除：送進 115 回收站（在 115 還原得回來），重複的每組至少留一份。本機的 strm 和同名的中繼資料跟著刪，
觀看紀錄轉到保留的那份，再重新掃描受影響的劇或電影。刪過的記在 dup_deleted，方便到回收站找回。

## embyserver/emptydirs.py

115 上的空資料夾：底下沒有影音檔的資料夾找出來，勾選後移到 115 回收站。

「空」是整個資料夾（含子資料夾）裡一支影片、一首音樂都沒有：完全是空的，或只剩 nfo、海報、字幕、文字檔這類東西
（常是整理後留下的舊資料夾）。只列最外面那一層：一部劇的資料夾整個是空的，就列劇的資料夾，不再列裡面的季。

掃描（每個範圍；範圍是整個網盤時，最上層的資料夾一個一個掃）：
1. 用 115 的「導出目錄樹」一次拿到所有資料夾和檔案的名稱，再列一次範圍裡的所有檔案（附所在資料夾 id 和大小）：
   目錄樹分不出最底層的是檔案還是空資料夾，115 列出的檔案裡有那個名稱的才是檔案。要用 cookie；導出失敗時那個範圍
   這次不掃，不改成逐層列目錄（幾千個資料夾一個一個列，會被 115 限流）。
2. 從名稱算出底下沒有影音檔的資料夾，只留最外層的；裡面有什麼、多大也從這兩份資料算，不必一個一個列。
3. 每個上一層列一次，拿到這些資料夾的 id 和修改時間。只有子資料夾裡也有檔案的，才把它整個列一遍算大小。
   一個一個列資料夾時至少間隔 PACE 秒：上一版每個都列一遍、一秒好幾次，碰上定時同步就被 115 回 405 熔斷。
不列：範圍本身、115 最上層的資料夾、同步任務的目錄、MoviePilot 目錄設定裡存儲是 115 的下載目錄和媒體庫目錄
（以及它們的上層）、藍光和 DVD 原盤裡的資料夾（路徑上有 BDMV、VIDEO_TS 這類，或和它們放在同一層）、
一小時內剛有變動的（可能還在整理或下載）。

刪除：勾的資料夾在背景照上一層一組一組處理。刪之前再到 115 上確認一次：每個上一層重新導出一次目錄樹、列一次，
還在原本的上一層、名稱沒變、一小時內沒有變動、裡面還是沒有影音檔，才送進 115 回收站（在 115 還原得回來）；
確認不過的不刪，記下原因。同步目錄裡的，本機對應的
資料夾裡 Mi302 下載的 nfo、圖片跟著拿掉，再重新掃描那些位置。和整理、刪除共用 Reorganizer 的鎖：MoviePilot 正在
整理時不刪，免得把它剛建好、影片還沒搬進去的資料夾刪掉。

## embyserver/organize115.py

整理 115 網盤：媒體庫裡命名不照 MoviePilot 格式、或集號不對的，整個資料夾交給 MoviePilot 整理；不要的直接刪。

Mi302 自己不判斷名稱對不對，也不猜 TMDB 編號、類型和季，全部問 MoviePilot：
- 檢查：每個劇集、電影資料夾問 MoviePilot「整理後叫什麼」（GET /api/v1/transfer/name；資料夾問一次，再挑一兩支影片
  問檔名），它用自己的辨識和重命名格式回答。和現在的名稱不一樣就列出來，附上 MoviePilot 給的名稱；旁邊已經有那個
  名稱的資料夾，就是會併進去。MoviePilot 的劇集格式有季資料夾、影片卻直接放在劇集資料夾裡的，也列出來。
  只差詞的順序不算不一樣：MoviePilot 解析檔名時會把「DV HQ」這類效果倒過來排，它自己取的名稱再問一次會變成「HQ DV」。
  問過的記在資料庫（organize_checks），資料夾名稱和裡面的影片沒變就不再問。幾千個資料夾第一次要問一陣子，在背景跑。
- 集號不對的劇：媒體庫掃描時集號是從檔名猜的、或認不出來的（items.ep_from），不用問 MoviePilot 也列出來。
- 瀏覽 115 裡挑的任何一個資料夾（folder_unit）：釘在清單最上面，一樣整理或刪除；不在同步目錄裡的（例如「待整理」）
  預設整理到 MoviePilot 目錄設定的媒體庫。
- 預覽：整個資料夾交給 MoviePilot（有子資料夾的一個子資料夾一次，直接放著的影片一次），和它網頁「檔案管理 → 整理」
  一樣，影片、字幕、音軌一起整理。預設什麼都不指定，讓它自己認；它認錯的（例如名稱裡的「预计第二季度」會被認成
  第 2 季）再在那一部分指定類型、TMDB 編號、季或集數定位（可以請 MoviePilot 推薦）。
- 送給 MoviePilot 時，它網頁整理對話框的「按類型分類」「按類別分類」「刮削元數據」「複用歷史識別信息」都關掉：
  留在原本的分類資料夾裡，只改資料夾和檔名；本機的 nfo、海報跟著 strm 搬（增量同步處理搬移）。
- 「照 MoviePilot 的目錄設定」：MoviePilot 自己挑目錄時只看「下載目錄」（來源要在下載目錄底下，下載目錄的存儲也要是
  115），已經在媒體庫裡的資料夾對不上，預覽時它只說「整理任务处理失败」。已經在它的媒體庫目錄（存儲是 115）裡的，
  留在現在的分類資料夾（和「同一層」一樣）；不在任何媒體庫目錄裡的（例如下載目錄）才讓它自己挑
  （先問 /transfer/manual/target-path 挑不挑得出來），放在媒體庫目錄底下。
- MoviePilot 沒有擋「新位置和原本一樣」，也不知道同步目錄在哪，所以 Mi302 自己擋：已經照格式命名的檔案、會把同步目錄裡
  的檔案搬出同步目錄的，那幾個檔案不送；覆蓋模式是「保留最新」時，在同一個資料夾裡改名的也不送（它會先刪掉同一集的
  其他版本，連來源檔案一起刪）。它的預覽也不看目標是不是已經有同名檔案：Mi302 到 115 上看，覆蓋模式是「不覆蓋」時
  那幾個不送（它會失敗「媒体库存在同名文件」，多半是重複的檔案），會覆蓋的只提醒。
  一個資料夾裡有不送的檔案時，改成只送其他檔案（一個一個送）。
- 執行：MoviePilot 有成功整理過的紀錄時，和它的網頁一樣帶 reorganize（清掉舊紀錄重新整理）；不帶的話它會當成
  「已整理過」跳過，預覽卻看不出來。
執行、清掉搬空的舊資料夾、之後的增量同步、刪除沿用 reorganize.Reorganizer。

## embyserver/reorganize.py

整理的執行和刪除（「整理 115 網盤」預覽過的交給這裡執行），以及集數定位模板的小工具。

- 執行（execute_in_background）：照一個或幾個預覽代碼送 MoviePilot 整理（每一批是一個資料夾或幾個檔案），
  結果照它回的每個檔案記；完成後刪掉本機寫著 -1 的舊 nfo（免得同步時它跟著 strm 搬到新名字），
  沒有影片留下的來源資料夾移到 115 回收站（cleanup，先確認資料夾 id 還在原本的路徑），再跑增量同步。
- 刪除：劇的任何一集（delete_episodes）、整部劇（delete_series）、一個資料夾或檔案（delete_item），
  都是送進 115 回收站（可以還原），本機 strm、nfo 和媒體庫跟著拿掉。
- 集數定位（episode_template）：MoviePilot 推薦不出來時的備用，依 Mi302 在檔名裡找到集號的位置產生模板
  （「10.潘玮柏…」是 {ep}.{a}、「03-比赛…」是 {ep}-{a}）。

## embyserver/moviepilot.py

刮削交給 MoviePilot：把需要刮削的 strm 路徑送到 MoviePilot 的刮削 API。

MoviePilot 的 POST /api/v1/media/scrape/local 會依路徑辨識影片、到 TMDB 等來源查資料，
在同一個資料夾寫入 nfo 與圖片；Mi302 之後重新掃描就讀得到。兩邊必須看得到同一批檔案，
路徑不同時用 path_mappings 轉換。

送出的單位：
- 電影：strm 檔本身（MoviePilot 會寫 nfo 和同資料夾的海報）。
- 劇集：整部劇還沒有 tvshow.nfo 時送劇集資料夾（一次處理劇、季、集）；
  已經刮削過的劇只送新的那幾集。
已經有 nfo 的項目不送，避免覆蓋 115 上帶下來或之前刮好的資料；手動刮削時，有 nfo 卻沒有劇照的集也會再送。

加快速度：同時送好幾項（MoviePilot 的刮削 API 是同步的，一項要等 TMDB 搜尋、取資料、下載圖片）；
已經刮削過的劇，送單集時直接帶上 tmdbid，MoviePilot 不必再用檔名搜尋 TMDB。
MoviePilot 說完成之後再看一次有沒有真的寫出 nfo、劇照：認不出集數時它也會回報完成。

補全缺集：先向 MoviePilot 查 TMDB 上每一季的集和播出日期（GET /api/v1/tmdb/{tmdbid}/{季}），
對照媒體庫裡的集號，只替真的缺集的季建訂閱（POST /api/v1/subscribe/），再請它立刻搜尋
（POST /api/v1/subscribe/search/{訂閱 id}）。MoviePilot V3 建訂閱時不檢查媒體庫、不保證馬上搜尋，
所以這兩步 Mi302 自己做。建訂閱和搜尋的 API 只接受帳號登入，不接受 API 令牌。

## embyserver/intro.py

片頭片尾：從播放行為學出來，給播放器「跳過片頭」「跳過片尾」用。

不碰 115、不讀影片：Mi302 本來就會收到播放器的進度回報（每幾秒一次的位置），從裡面看得出
「在開頭跳過了一段」和「片尾停下來、切下一集」。做法參考 Emby 神醫助手的「片頭探測 ‐ 播放行為」。

- 片頭：開頭 10 分鐘內（短的集是前 25%），位置往前跳了 15 秒到 3 分鐘、而且跳得比實際經過的時間多很多
  （不是倍速播放），就記下「從哪跳到哪」。只看跳的那一下，之後怎麼播不管。
  同一季的其他集沒有自己的紀錄時，套用這一季所有紀錄的中位數。
- 片尾：最後 5 分鐘（短的集是最後 25%）裡停下（切下一集、關掉）但沒播完，或往前跳了 60 秒以上、跳到結尾，
  記下位置；同一季套用「距離結尾多久」的中位數。
- 每個使用者對每一集只留最後一次紀錄，多人多集時取中位數，偶爾亂跳不會蓋掉。

也可以在網頁上手動設定某一季的片頭、片尾（或標成這一季沒有）；設了的部分不用學到的值，學的紀錄照留，
改回「自動」就恢復。

給播放器的格式（三種都給，播放器認哪種用哪種）：
- Emby：項目的 Chapters 裡加 MarkerType 為 IntroStart／IntroEnd／CreditsStart 的章節。
- Jellyfin Intro Skipper 外掛：GET /Episode/{id}/IntroTimestamps（/v1）、/Episode/{id}/Timestamps。
- Jellyfin 10.10 的媒體片段：GET /MediaSegments/{id}。

## embyserver/updater.py

網頁上的「檢查更新」「更新到最新版」「重新啟動」。

程式資料夾是 git clone 下來的（install.sh 裝的都是），更新的做法和 install.sh update 一樣：
1. 檢查：git fetch 遠端的同一個分支，比較 HEAD 和 origin/分支，新的提交標題就是更新內容。
   啟動一分鐘後查一次，之後每 6 小時一次（server.update_check 關掉就只在網頁上按了才查）。
2. 更新：自己改過的程式檔備份成 local-changes-*.patch 再還原，切到 origin/分支；requirements.txt 有變就用
   目前這個 Python（.venv 裡的）安裝相依套件；再用新程式試著 import 一次。任何一步失敗就退回原本的版本、
   不重新啟動，網頁照常可用。
3. 重新啟動：請 uvicorn 停下（進行中的請求最多等 5 秒），__main__ 再用同一個指令 exec 自己。程序編號不變，
   systemd、launchd、背景執行都不用另外處理。

install.sh 本身管的東西（服務設定、ffprobe、Python 版本）網頁更新不會動，那些要在終端機執行 mi302 update。

連不上 GitHub 時（國內網路），server.update_proxy 是 git 和安裝相依套件用的代理（http:// 或 socks5://），
server.update_github_proxy 是 GitHub 加速網址（例如 https://ghfast.top/），下載時接在 GitHub 網址前面；
和 MoviePilot 的「網路代理」「GitHub 加速代理」一樣的用法。pip 鏡像照 install.sh 記在 .env 的 PIP_MIRROR。

## embyserver/routes/web/__init__.py

網頁管理介面 /web 和它用的管理 API，照分頁分成幾個檔案，這裡組成一個 router 給 app 用。

- setup：網頁本身、首次設定、設定、使用者、選資料夾、API 金鑰、日誌、備份、中文化
- scan：媒體庫掃描、批量新增媒體庫的建議
- intro：片頭片尾
- moviepilot：刮削、補全缺集
- organize：整理 115 網盤（整理、集號不對的劇、刪除）
- p115：瀏覽 115、回收站、重複檔案、媒體資訊
- server：版本、檢查更新、更新、重新啟動
