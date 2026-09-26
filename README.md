# Mi302

Mi302 是一個 API 與 Emby 相容的影片伺服器，主要給放在 115 網盤的影片用：

- 自己掃碼登入 115，把 115 的資料夾同步成本機的 `.strm` 檔，之後依 115 的「生活事件」增量同步。
- 支援 Emby 的播放器（Infuse、VidHub、SenPlayer、Emby 官方 App 等）可以直接登入觀看。
- 播放時用 **HTTP 302** 把播放器導向 115 直鏈，影片流量不經過伺服器。
- 刮削交給 MoviePilot；媒體庫封面可以用 MoviePilot 的封面插件產生。
- 所有設定都在網頁 `/web` 上完成，並和 `config.yaml` 保持一致。

目錄：[安裝](#安裝) · [第一次設定](#第一次設定) · [115 網盤](#115-網盤) · [刮削：交給 MoviePilot](#刮削交給-moviepilot) · [媒體庫結構](#媒體庫結構) · [媒體資訊](#媒體資訊) · [掃描](#掃描) · [日誌](#日誌) · [常見問題](#常見問題)

## 安裝

兩種方式選一種：

| 方式 | 適合 | 開機自動啟動 |
| --- | --- | --- |
| [一鍵安裝腳本](#一鍵安裝腳本建議)（建議） | Linux、macOS | 自動設定 |
| [手動用 Python](#手動用-python) | 想自己控制每一步，或 Windows | 自己設定 |

需要 Python 3.10 以上，一鍵安裝腳本會自動安裝。

### 一鍵安裝腳本（建議）

在要跑 Mi302 的機器上執行：

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash
```

腳本會先問完問題，再自動安裝：

1. **用哪個使用者執行**：預設是你自己（執行 sudo 的帳號）。這個帳號要能讀寫媒體資料夾，115 同步產生的 strm 也是用它的身分寫入。
2. **埠號**：預設 8096。

接著它會下載程式、安裝 Python、ffmpeg 和相依套件、設定開機自動啟動、啟動 Mi302 並確認網頁有回應，最後印出管理網頁的網址。打開網址，照[第一次設定](#第一次設定)做。

裝好的檔案（Linux 在 `/opt/mi302`，macOS 在 `~/Mi302`）：

```
/opt/mi302/
  config/               設定和資料，更新、重裝都不會動到
    config.yaml         設定檔（網頁上的設定也寫在這裡）
    data/               資料庫、115 登入狀態、上傳的封面
      logs/mi302.log    日誌
  .env                  安裝時選的選項
  .venv/                Python 虛擬環境
  embyserver/ …         程式
```

不想一題一題回答，可以把選項一次給完，`-y` 表示其他都用預設值：

```bash
# 以 cat 這個帳號執行，埠號 8097
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash -s -- --user cat --port 8097 -y
```

| 選項 | 說明 |
| --- | --- |
| `--user 帳號` | 用哪個 Linux 帳號執行 |
| `--port 埠號` | 網頁和播放器用的埠號 |
| `--dir 資料夾` | 安裝位置 |
| `--mirror` | pip 改用清華鏡像（連不上 PyPI 時會自動改用） |
| `-y` | 不詢問，沒給的都用預設值 |

其他情況：

- **已經自己 `git clone` 下來跑過**：在那個資料夾裡執行 `sudo bash install.sh`，會直接裝在原地，原本的 `config.yaml` 和 `data/` 照用。記得先把手動開的 Mi302 關掉，不然埠號會被佔用（腳本會提醒）。程式資料夾裡自己改過的檔案，更新時會備份成 `local-changes-*.patch` 再還原成最新版。
- **以前用 Docker 版的**：這個版本不再提供 Docker。先 `docker compose down`，把 `config/` 資料夾留著，在同一個資料夾執行 `sudo bash install.sh`，設定和資料會原樣沿用。
- **macOS**：不要加 sudo，執行 `curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | bash`。沒有 Python 3.10 以上時會用 Homebrew 安裝。Mi302 在你登入 macOS 後自動啟動（launchd）。
- **沒有 systemd 的環境**（例如 WSL）：改成在背景執行，重新開機後要自己執行 `mi302 start`。

#### 管理指令

裝好後可以用 `mi302` 指令管理，需要 root 的操作會自動加 sudo：

| 指令 | 作用 |
| --- | --- |
| `mi302` 或 `mi302 status` | 是否在執行、網址、版本 |
| `mi302 logs` | 即時看日誌（Ctrl+C 離開） |
| `mi302 restart`、`mi302 stop`、`mi302 start` | 重新啟動、停止、啟動 |
| `mi302 update` | 更新到最新版並重新啟動，設定和資料不動 |
| `mi302 reset-password admin 新密碼` | 忘記密碼時重設（帳號不存在會建立成管理員） |
| `mi302 uninstall` | 移除開機自動啟動和 `mi302` 指令，程式、設定和資料留著；要全部刪掉再執行 `sudo rm -rf /opt/mi302` |

要換選項（例如埠號、執行的帳號），重新執行安裝腳本並加上新選項，例如 `sudo bash /opt/mi302/install.sh --port 8097`；已經裝好的部分會沿用，等於順便更新。

Linux 上服務是 systemd 的 `mi302.service`，也可以用 `systemctl status mi302`、`journalctl -u mi302 -f` 查看。

### 手動用 Python

需要 Python 3.10 以上（`python3 --version` 查看）。Debian、Ubuntu 沒有 venv 時先 `sudo apt install python3-venv`。

```bash
git clone https://github.com/MiCat-S/Mi302.git
cd Mi302
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m embyserver
```

然後開 `http://<主機>:8096/web`。

- 設定檔 `config.yaml` 和資料 `data/` 會放在**執行指令時所在的資料夾**，上面的例子就是 `Mi302/`。
- 更新：`git pull && .venv/bin/pip install -r requirements.txt`，然後重新啟動。
- 改埠號：改 `config.yaml` 的 `server.port`，然後重新啟動。
- 國內網路 pip 很慢時，加上 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

開機自動啟動可以用 systemd。把下面的 `cat` 和路徑換成你的，存成 `/etc/systemd/system/mi302.service`，再執行 `sudo systemctl enable --now mi302`：

```ini
[Unit]
Description=Mi302
Wants=network-online.target
After=network-online.target remote-fs.target

[Service]
User=cat
WorkingDirectory=/home/cat/Mi302
ExecStart=/home/cat/Mi302/.venv/bin/python -m embyserver -c /home/cat/Mi302/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 從外網連線

在前面加一層反向代理（例如 Nginx、Caddy）提供 HTTPS，並在網頁「進階設定」把 strm 的伺服器網址填成對外網址，再同步一次讓 strm 更新。

### 常見問題

- **其他裝置連不上**：確認防火牆放行埠號，例如 `sudo ufw allow 8096/tcp`，或 `sudo firewall-cmd --permanent --add-port=8096/tcp && sudo firewall-cmd --reload`。一鍵安裝腳本發現防火牆開著時會提醒。
- **埠號被佔用**：多半是之前手動開的 Mi302 還在跑，關掉它（或換埠號）再裝。
- **網路磁碟、共用資料夾開機後才掛上**（NAS 的 NFS/SMB、Parallels 的 `/media/psf/...` 等）：Mi302 發現媒體庫資料夾不存在或是空的，會保留原本的項目和觀看紀錄，不會當成影片全被刪了。掛好之後在網頁「媒體庫」按「全部重新掃描」即可。
- **strm 寫不進去、掃描不到影片**：執行 Mi302 的帳號沒有媒體資料夾的權限。直接用 Python 的可以用 `sudo -u 帳號 ls 資料夾` 測試，或重新安裝時用 `--user` 換成有權限的帳號。
- **國內網路**：
  - pip：一鍵安裝腳本連不上 PyPI 時會自動改用清華鏡像，也可以加 `--mirror`。
  - GitHub：`raw.githubusercontent.com` 連不上時，到 GitHub 網頁按「Code → Download ZIP」下載，解壓後在資料夾裡執行 `sudo bash install.sh`。之後 `mi302 update` 仍然需要連上 GitHub。
- **忘記管理員密碼**：`mi302 reset-password admin 新密碼`；手動安裝的見上面各自的說明。

## 第一次設定

不需要改任何設定檔，全部在網頁上完成。用瀏覽器開 `http://<主機>:8096/web`：

1. **建立管理員**：第一次打開會請你設定帳號密碼。登入後的「概覽」頁有設定步驟清單，照著點「前往」就好。
2. **媒體庫**：按「新增媒體庫」，取名（例如「電影」）、選類型，再按「加入資料夾」點選伺服器上的資料夾，最後按下方的「儲存並掃描」。
3. **115 網盤**：
   - 按「掃碼登入」，用手機 115 App 掃描確認。
   - 在「同步任務」按「瀏覽」選 115 目錄和要放 strm 的本機資料夾，按「新增任務」。本機資料夾要在某個媒體庫裡，建議用子資料夾，例如 `/media/movies/115`。
   - 按「增量同步」。第一次會自動先跑一次全量，同步完會自動掃描媒體庫。
4. **刮削**（選用）：有 MoviePilot 的話，在「MoviePilot」分頁填網址和 API 令牌，之後新產生的 strm 會自動送去刮削。見下面「[刮削：交給 MoviePilot](#刮削交給-moviepilot)」。
5. **播放器**（Infuse、VidHub、SenPlayer、Emby 官方 App 等）新增 Emby 伺服器，位址填 `http://<主機>:8096`，用剛才的帳號登入。

家人的帳號在「使用者」分頁新增；自動同步間隔、strm 網址等在「115 網盤」的同步選項和「進階設定」。

### 設定檔

網頁上的設定都存在設定檔 `config.yaml`（一鍵安裝的在 `config/config.yaml`），兩邊保持一致：

- 第一次啟動時自動產生，每一項都附說明註解，不用自己建立。
- 在網頁上儲存設定時自動寫回，覆寫前把舊檔留成 `config.yaml.bak`。檔案每次都依範本重新產生，自己加的註解不會保留。
- 也可以直接改檔案，網頁重新整理後就會套用；`server` 的 `host`、`port`、`data_dir` 要重新啟動才生效。檔案格式寫錯時，網頁上方會顯示錯誤，並繼續用上次讀到的設定。
- 帳號密碼、115 登入狀態、網頁上建立的 API 金鑰存在資料庫（`data/`），不寫進設定檔。檔案裡的 `users` 只在第一次啟動時用來建立帳號。
- 舊版存在資料庫裡的網頁設定和同步任務，更新後第一次啟動會自動搬進設定檔。

各項目的說明見 `config.example.yaml`。

### 備份

Mi302 每天自動備份一次資料庫（使用者、觀看紀錄、115 登入狀態、同步索引、媒體資訊）和設定檔，放在 `data/backups`，檔名是 `mi302-年月日-時分秒.db`／`.yaml`，預設留最新 7 份（設定檔的 `server.backup_keep`，0 = 不自動備份）。「進階設定」頁可以立即備份、下載備份。備份含 115 登入資訊和密碼雜湊，請妥善保管。

還原：停止 Mi302，把要還原的 `.db` 複製成 `data/library.db`，刪掉 `library.db-wal`、`library.db-shm`；需要的話把同時間的 `.yaml` 換成 `config.yaml`，再啟動。

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
| 依據 | 115 的生活事件（網盤的操作紀錄），再用修改時間補抓 | 115 的導出目錄樹，加上一次列出所有檔案，重新比對 |
| 處理 | 上傳、接收、複製、移動、改名、刪除 | 補齊漏掉的檔案；勾選「跟著刪」時清掉已刪除的項目 |
| 速度 | 快，只讀上次同步之後的事件 | 不必逐層列目錄，資料夾多也不慢；要等 115 產生目錄樹 |
| 適合 | 每隔幾分鐘自動跑 | 預設每週自動跑一次，查漏補缺 |

增量同步分兩步：

1. **讀生活事件**。115 會記錄網盤裡的每個操作，Mi302 讀上次同步之後的事件，同一個檔案只看最後一個事件：
   - 上傳、接收、複製、從外面移進同步目錄：產生 strm；整個資料夾移進來時會列出裡面的檔案。
   - 在同步目錄裡移動、改名（檔案或資料夾）：本機的 strm 跟著搬，同名的 nfo、海報、字幕（例如 MoviePilot 刮削的）也一起搬，不必重新刮削。
   - 刪除、移出同步目錄：勾選「跟著刪」時，刪掉 strm 和它的刮削資料。
2. **依修改時間補抓**。請 115 列出同步目錄裡最近修改的檔案，補上沒有產生事件的上傳（例如離線下載、第三方工具上傳）。只需要一次請求，看到舊檔案就停。

全量同步分三步：

1. **導出目錄樹**。請 115 產生整個同步目錄的目錄樹（跟 115 網頁上「導出目錄樹」一樣），一次拿到所有資料夾和檔案的路徑。檔案會暫時放在 115 根目錄，讀完就刪掉。
2. **列出所有檔案**。一次列出同步目錄底下所有檔案的 pickcode、大小和所在資料夾，每次請求 1150 個。
3. **對上路徑**。目錄樹只有名稱，檔案清單只有資料夾 id，Mi302 用資料夾裡的檔名把兩邊對起來。檔名太普通對不出來的資料夾（例如兩季都只有 `01.mkv`），以及只放子資料夾的資料夾（例如只放各季的劇集資料夾），才另外查一次路徑；查一個資料夾會順便拿到它所有上層資料夾，查到的都記下來，下次全量不必再查。記下只放子資料夾的資料夾，是為了增量同步遇到整部劇改名或搬移時，能把本機整個資料夾連同刮削資料一起搬。

以前的做法是一層一層列出每個資料夾，資料夾有幾千個就要發幾千次請求。現在第一次全量大約是「列檔案的頁數＋只放各季的劇集數」，之後每次只要十幾次。導出、列檔案或查路徑失敗（例如只用開放平台登入、115 上正在跑別的導出目錄樹任務）或列出的檔案明顯比目錄樹少時，會自動改回逐層列目錄；目錄樹裡有、115 卻沒列出來的少數影片，不會當成已刪除，strm 照舊保留。同步結果上都會註明。

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
   - MoviePilot 用 Docker：看它容器裡的掛載路徑，例如主機的 `/volume1/media` 掛成 `/mnt/media`，就填 `/volume1/media => /mnt/media`。
2. 在 MoviePilot 的「設定 → 系統」複製 **API 令牌**。
3. 在 Mi302 網頁的「MoviePilot」分頁填 MoviePilot 網址（例如 `http://192.168.1.10:3000`）和 API 令牌，按「儲存」再按「測試連線」。
4. 測試出現「拒絕存取」時，表示你的 MoviePilot 版本較舊、刮削 API 只接受登入，請展開「舊版 MoviePilot」填帳號密碼。

「測試連線」只檢查網址和 API 令牌：它送一個空路徑給刮削 API，MoviePilot 會回「刮削路径无效」拒絕，所以不會真的刮削。路徑對應對不對，要看第一次刮削的結果；MoviePilot 找不到檔案時，「刮削」卡片會列出它收到的路徑。

### 送出規則

- 電影：送 strm 檔本身。
- 劇集：整部劇還沒有 `tvshow.nfo` 時送整個劇集資料夾，一次處理劇、季、集；已經刮削過的劇只送新的那幾集。
- 已經有 nfo 的影片不送（包括單片資料夾裡的 `movie.nfo`），避免覆蓋從 115 帶下來或之前刮好的資料。
- 連線或認證失敗時會停下整批，網頁上顯示原因。

「同步產生新的 strm 後自動送去刮削」預設開啟；關掉時同步完直接掃描。已經存在的媒體庫可以按「刮削缺少資料的項目」，把所有還沒有 nfo 的影片、以及有 nfo 卻沒有劇照的集送一次。

### 刮削速度

MoviePilot 的刮削 API 是同步的：每一項都要等它到 TMDB 查資料、下載圖片才回應。Mi302 這樣加快：

- **同時送好幾項**：「同時刮削幾項」預設 3，最多 8。太多時 TMDB 可能限速，MoviePilot 日誌出現 429 就調低。
- **已經刮削過的劇帶上 tmdbid**：送單集時直接告訴 MoviePilot 是哪一部（`media_source=themoviedb&media_id=…`），不必再用檔名搜尋 TMDB，也不會認錯。這需要 MoviePilot V3；舊版會忽略這些參數，照常用檔名辨識。

MoviePilot 本身慢的話，多半是連 TMDB 慢：在 MoviePilot 設定 TMDB 的 API 網址和圖片網址代理。

### 單集沒有劇照

MoviePilot 的單集圖片只來自 TMDB 那一集的劇照，存成和影片同名的 `X.jpg`。沒有圖片通常是：

1. **TMDB 沒有這集的劇照**：國產劇、綜藝、剛播出的集很常見，MoviePilot 也寫不出來。這種集 Mi302 會改用劇的橫幅圖（`landscape`/`thumb`，沒有就用背景圖），播放器不會一片空白。
2. **MoviePilot 下載圖片失敗**：日誌有「图片下载失败」，多半是連不到 `image.tmdb.org`，要在 MoviePilot 設定 TMDB 圖片代理。修好之後按「刮削缺少資料的項目」，有 nfo 卻沒有劇照的集會再送一次。

刮削結果會分開計數「沒有劇照」的集；送過確定沒有劇照的集 30 天內不再重送。MoviePilot 認不出集數時什麼都不寫、卻回報完成，Mi302 會檢查有沒有寫出 nfo，沒有就算失敗並說明原因（檔名要有 `S01E01` 這類集號）。

### 補全缺集

媒體庫裡的劇少了幾集時，可以讓 MoviePilot 去下載補齊。「MoviePilot」分頁的「補全缺集」卡片列出媒體庫裡所有的劇和每一季有幾集，集號有空洞的（例如有第 2、4 集沒有第 3 集）會標出來；可以搜尋劇名，按「補全」只補那一部，或按「全部補全」把所有的劇都送出去。

每一季的流程：

1. 向 MoviePilot 查 TMDB 上這一季的集和播出日期。有日期的看日期；沒有日期的常是還沒播的佔位集，只有集號不超過媒體庫裡最後一集的才算播過（都有第 10 集了，第 3 集一定播過）。比最後一集後面又沒有日期的不確定，不算缺，結果裡會註明。
2. 對照 Mi302 裡已有的集號。已播出的都有就不建訂閱，結果記成「已經齊全」。所以「集號有空洞」只是提示，最後幾集沒下到（TMDB 有播出日期）的情況這一步也算得出來。
3. 缺集才建訂閱，用 tmdbid 指定是哪一部（V3 的 `media_source`/`media_id`），不會靠劇名認錯。
4. 立刻請 MoviePilot 搜尋這條訂閱。MoviePilot V3 建訂閱後只是「安排」搜尋，有時要等到定時搜尋才開始；之前就訂閱過的也會請它再搜一次。

下載整理完 MoviePilot 會通知 Mi302 重新掃描。缺集的季如果還在更新，訂閱會跟著追新集。只處理媒體庫裡已經有集的季，TMDB 上有、媒體庫整季都沒有的季不會自動訂閱。

要先做兩件事：

1. 把 Mi302 加成 MoviePilot 的媒體伺服器（見下一節），MoviePilot 才知道哪些集已經有了。
2. 在「MoviePilot」分頁填 MoviePilot 的帳號密碼：建訂閱的 API 只接受帳號登入，不接受 API 令牌。

沒有 tmdbid 的劇（還沒刮削過）不會送，先刮削再補全。勾選「全量同步後自動補全」時，每次全量同步、刮削完之後會自動把所有有 tmdbid 的劇送一次。

### 讓 MoviePilot 把 Mi302 當成 Emby

MoviePilot 可以把 Mi302 加成媒體伺服器，用來判斷片子是否已經有了、整理完自動通知 Mi302 重新掃描：

1. 在 Mi302 網頁的「MoviePilot」分頁建立一把 API 金鑰。
2. 在 MoviePilot 的「設定 → 媒體伺服器」新增 Emby，地址填 `http://<Mi302 主機>:8096`，API 金鑰貼上剛才那把。

MoviePilot 通知 Mi302 某些檔案有變動時，Mi302 只掃那些檔案所在的劇或電影（路徑會依「路徑對應」換回 Mi302 的路徑），不會重掃整個媒體庫。

### 媒體庫封面（MoviePilot 封面插件）

播放器首頁每個媒體庫的封面可以用 MoviePilot 的媒體庫封面插件產生（例如 [wio-ki/MoviePilot-Plugins](https://github.com/wio-ki/MoviePilot-Plugins) 的「媒體庫封面生成」）：

1. 先照上一節把 Mi302 加成 MoviePilot 的 Emby 媒體伺服器。
2. 安裝插件，在插件設定的媒體伺服器選 Mi302，選要產生封面的媒體庫。
3. 按插件的「立即更新」，或設定排程（例如每天一次）。

插件會從媒體庫裡隨機挑海報組成封面，再上傳到 Mi302（Emby 的 `POST /Items/{id}/Images/Primary`）。上傳的封面：

- 存在 `data/images/`，重新掃描不會被蓋掉，也比媒體庫資料夾裡的 `poster.jpg` 優先。
- 在網頁「媒體庫」分頁看得到，也可以自己上傳圖片，或按「改回預設」刪掉上傳的封面。

插件的「入庫監控」要 MoviePilot 整理完成或 Emby 的新增通知才會觸發；Mi302 從 115 同步進來的檔案不經過這兩個，所以新片進來後封面不會自動更新，請用排程或手動更新。

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

### 片頭片尾跳過

Mi302 不讀影片、不碰 115，而是從播放行為學：

- 有人在開頭 8 分鐘內往前跳過 15 秒以上（不是倍速播放），就記成這一集的片頭，從跳走的位置到跳到的位置。
- 在最後 25% 停下、切下一集（離結尾還有 20 秒以上），就記成片尾開始的位置。
- 同一季的其他集沒有自己的紀錄時，套用這一季的中位數；片尾按「距離結尾多久」套用。每個使用者對每一集只留最後一次紀錄，多人多集取中位數。

播放器拿到的是 Emby 的章節標記（`MarkerType` 為 `IntroStart`／`IntroEnd`／`CreditsStart`，Emby 系列、SenPlayer 等會顯示「跳過片頭」），另外也提供 Jellyfin Intro Skipper 外掛的介面（`/Episode/{id}/IntroTimestamps`、`/Episode/{id}/Timestamps`）和 Jellyfin 10.10 的 `/MediaSegments/{id}`，播放器認哪種用哪種。「媒體庫」頁的「片頭片尾」卡片看得到學到哪些季，可以清除重學；設定 `server.intro_skip` 可以關掉。

### 演職人員與中文化

- **演職人員**：MoviePilot 刮削的 nfo 裡有演員、導演、編劇，Mi302 讀進來給播放器顯示，點人物可以看他參與的作品，搜尋也找得到人。集沒有自己的演員時用劇的。
- **中文名**（預設開）：TMDB 的人名常是拼音或英文（Chen He）。Mi302 在背景把每部作品前 20 位演員和導演、編劇的中文名查出來：先經 MoviePilot 的人物介面拿 TMDB 別名挑中文的（陈赫），沒有再批次問 Wikidata。用原名、中文名都搜得到；查過沒有的 30 天後再查。「進階設定」頁看得到進度和來源。
- **類型中文化**（預設開）：Action → 动作、Sci-Fi & Fantasy → 科幻奇幻，繁體換成簡體。關掉後重新掃描即還原（來源是 nfo）。

### 中文片名的排序和搜尋

- 排序：中文片名按拼音排（流浪地球2 → liu lang di qiu 2），播放器按字母跳轉也有效。集的順序照季、集號，不受影響。
- 搜尋：全拼（qingyunian）、首字母（qyn）、簡體、繁體（慶餘年）都找得到「庆余年」。繁體轉簡體有時用字不同，兩個字以上的中文搜尋詞會再用拼音比一次。
- 更新後第一次啟動的整庫掃描會把舊資料補齊。

## 媒體資訊

播放器要知道影片的解析度、HDR／杜比視界、音軌、內封字幕和章節，才能在列表上標出 4K、HDR，播放前讓人選音軌和字幕。strm 只是一行網址，這些資訊要另外從影片檔頭讀出來（ffprobe）。

Mi302 讀影片旁邊的 `X-mediainfo.json`（影片是 `X.strm` 時），格式和 Emby 神醫助手（[StrmAssistant](https://github.com/sjtuross/StrmAssistant)）的「媒體資訊持久化」一樣。所以：

- 用過 Emby＋神醫、或 [emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo) 產生過的檔案，放在 strm 旁邊就能直接用，掃描時讀進來。
- 有媒體資訊的影片，播放資訊會帶上媒體流、碼率、大小和片長（nfo 沒寫片長時用它的）；播放方式照舊是 302 直連，不轉碼。
- 同步時 strm 改名、搬移、刪除，`X-mediainfo.json` 跟著一起搬、一起刪。
- 神醫設定了「媒體資訊根目錄」（json 集中放在另一個資料夾）的話，要把檔案搬到 strm 旁邊才讀得到。

### 用 ffprobe 探測

沒有現成 json 的影片，Mi302 可以自己用 ffprobe 探測，在「媒體庫」頁的「媒體資訊」卡片設定：

- **打開影片時自動探測**（預設開）：播放器打開某部片或某一集時，在背景排隊探測它，不等、不拖慢播放，下次打開就有。一次打開整季也不會卡住，只排被打開的那幾項。不想整庫探測的話，開這個就夠了。
- **整庫探測**（預設關）：開了才能按「提取缺少的媒體資訊」一次補齊整個媒體庫；勾選「同步產生新的 strm 後自動探測」時，新同步的影片會在背景自動探測。

- 每一項向 115 取一次直鏈，ffprobe 讀檔頭（通常幾 MB），結果寫成 `X-mediainfo.json` 放在 strm 旁邊，Emby＋神醫那邊也能共用。媒體資料夾唯讀時只存在 Mi302 的資料庫。
- 需要 ffmpeg：安裝腳本會試著裝，沒裝成就自己 `apt install ffmpeg` 或 `brew install ffmpeg`。沒有 ffprobe 時仍會讀現成的 json。
- 115 的限制：同時最多 3 條連線（「同時探測幾項」最多 3，預設 2），取直鏈至少間隔 0.5 秒（預設 1 秒），每小時最多 300 次（可改，0 = 不限）。首次整庫探測上萬支影片時會分幾天慢慢補齊，卡片上看得到這一小時用了幾次。取直鏈和 ffprobe 用同一個一般瀏覽器 UA，並重用連線，少觸發 CDN 限流。
- 115 限流或登入失效時熔斷：探測和同步都先停，45 分鐘後自動再試，恢復後的一小時先放慢（間隔拉長、上限調低）；登入失效要重新登入。播放不受影響，「115 網盤」頁會顯示原因。
- 115 上的檔案被換掉（pickcode 變了）時，同步會刪掉舊的 `X-mediainfo.json` 並重新探測；只改伺服器網址而重寫的 strm 不受影響。
- 失敗的會列出原因（網址會抹掉）；下次按「提取缺少的媒體資訊」會再試。

ffprobe → Emby 欄位的對照、限速與熔斷的做法改寫自 emby-mediainfo（MIT 授權，版權聲明保留在 `embyserver/mediainfo.py`）。

## 掃描

不一定要整個重新掃描。網頁「媒體庫」分頁可以：

- 按某個媒體庫的「掃描」，只掃這個媒體庫。
- 按某個資料夾那一行的「掃描」，只掃這個資料夾。
- 按上方的「掃描資料夾…」，選任何一個媒體庫裡的資料夾，例如一個分類 `电视剧/国产剧` 或一部劇 `电视剧/国产剧/庆余年 (2019)`。
- 按「全部重新掃描」，掃全部媒體庫。

自動掃描也只掃有變動的地方：

- 115 同步完成後，只掃新增、改名、移動或刪除的檔案所在的劇或電影。
- MoviePilot 刮削完成後，只掃刮削過的那些。
- MoviePilot 或其他工具通知 `Library/Media/Updated` 時，只掃通知裡的路徑；播放器對單一項目按「重新整理」時，只掃那一項。
- 改了媒體庫設定（新增、改路徑）時，只掃有改的媒體庫；刪掉的媒體庫會連同裡面的項目一起移除。

掃描單位是一部劇或一部電影：給一集的路徑會重掃整部劇，給分類資料夾會掃裡面所有劇。一次超過 300 個單位時改成掃整個媒體庫。部分掃描不會改變劇集和電影的 id，觀看紀錄不會掉；已經刪掉的檔案在掃到時會從媒體庫移除。

## 日誌

網頁「日誌」分頁顯示最近 3000 筆紀錄（同步、刮削、掃描、播放、錯誤），可以按等級篩選、搜尋，打開「自動更新」時新紀錄會即時出現。

- 完整紀錄寫在 `data/logs/mi302.log`（一鍵安裝的在 `config/data/logs/mi302.log`），滿 5 MB 換新檔，保留 5 份舊檔。日誌頁可以直接下載。
- 播放器連不上或播不了時，打開日誌頁下方的「詳細模式」，會另外記錄每個播放器請求，找到原因後記得關掉。也可以在 `config.yaml` 設定 `server.log_level: debug`。
- 網址裡的 `api_key`、`token`、密碼等憑證在寫入前一律遮成 `***`。

## 播放流程（技術細節）

1. 掃描設定中的媒體庫資料夾，把 `.strm` 與一般影片檔、NFO、海報寫入 SQLite。
2. 播放器呼叫 `POST /Items/{id}/PlaybackInfo`。strm 項目回傳 `Protocol=Http`、`IsRemote=true`，並強制 DirectPlay（關閉轉碼），`DirectStreamUrl` 指回 `/videos/{id}/stream.{container}`。
3. 播放器請求 `/videos/{id}/stream`、`/videos/{id}/original.xxx`、`/items/{id}/download` 時：
   - strm 項目：讀取 strm 內容，套用 `path_rules`，可選擇先跟隨上游重導向鏈，然後回 `302 Location: <真實網址>`。
   - 一般影片檔：直接送檔，支援 Range。
4. 路徑不分大小寫，`/emby`、`/mediabrowser` 前綴可有可無。

302 流程參考自 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 `embyreverseproxy` 外掛。差別在於這裡不需要背後有真的 Emby，Emby API 由本專案自行實作。

## 已實作的端點

- 系統：`System/Info/Public`、`System/Info`、`System/Ping`、`System/Endpoint`
- 使用者：`Users`、`Users/AuthenticateByName`、`Users/Public`、`Users/{id}`、`Sessions/Logout`
- 媒體庫：`Users/{id}/Views`、`Library/MediaFolders`、`Library/VirtualFolders`、`Library/VirtualFolders/Query`、`Library/SelectableMediaFolders`、`Library/Refresh`、`Library/Media/Updated`
- 項目：`Users/{id}/Items`、`Items`（ParentId、Recursive、IncludeItemTypes、SortBy、SearchTerm、Filters、分頁）、`Users/{id}/Items/{itemId}`、`Items/Latest`、`Items/Resume`、`Shows/{id}/Seasons`、`Shows/{id}/Episodes`、`Shows/NextUp`、`Genres`、`Items/Counts`、`Items/{id}/Refresh`
- 播放：`Items/{id}/PlaybackInfo`、`Videos/{id}/*`、`Items/{id}/Download`、`Sessions/Playing[/Progress|/Stopped]`
- 使用者資料：`PlayedItems`、`FavoriteItems`
- 圖片：`Items/{id}/Images/{type}`（讀取、上傳、刪除）

## 開發

```bash
pip install -r requirements.txt pytest
python -m pytest
```

`install.sh` 改完可以用 `shellcheck install.sh` 檢查。
