# Mi302

**繁體中文** | [简体中文](README.zh-CN.md) | [English](README.en.md)

Mi302 是一個 API 與 Emby 相容的影片伺服器，給放在 115 網盤的影片用。它把 115 的資料夾同步成本機的 `.strm` 檔，播放器照 Emby 的方式登入觀看；播放時用 HTTP 302 把播放器導向 115 直鏈，影片流量不經過伺服器。

不需要背後有真的 Emby，也不需要其他 115 工具。完整說明在 [Wiki](https://github.com/MiCat-S/Mi302/wiki)。

## 功能

- **115 同步**：掃碼登入 115，把資料夾同步成 strm。之後讀 115 的生活事件做增量同步，每週再全量同步一次查漏補缺。
- **Emby 相容**：Infuse、VidHub、SenPlayer、Emby 官方 App 等支援 Emby 的播放器，新增伺服器就能登入觀看。
- **302 直連播放**：用播放器自己的 User-Agent 向 115 取直鏈再轉過去，伺服器不轉發影片、不轉碼。
- **刮削交給 MoviePilot**：新同步的影片自動送去刮削。媒體庫缺集時，可以讓 MoviePilot 訂閱補齊。
- **媒體資訊**：讀 `X-mediainfo.json`（神醫助手的格式），也能用 ffprobe 探測，播放器看得到 4K、HDR、音軌和字幕軌。
- **片頭片尾跳過**：從播放行為學出片頭片尾，SenPlayer 等播放器會出現「跳過片頭」。
- **中文友善**：中文片名按拼音排序，拼音、首字母、繁簡體都搜得到；演職人員顯示中文名，類型中文化。
- **網頁管理**：所有設定都在網頁 `/web` 完成，和 `config.yaml` 保持一致；每天自動備份資料庫。
- **保護 115 帳號**：被 115 限流或登入失效時自動熔斷，同步和探測先停下，播放不受影響。

## 運作方式

```mermaid
flowchart LR
    Cloud[115 網盤] -- 同步 --> Strm[本機 .strm]
    Strm -- 掃描 --> Mi302[Mi302]
    Player[播放器] -- Emby API --> Mi302
    Mi302 -- 302 到 115 直鏈 --> Player
    Player -- 直接讀取影片 --> Cloud
```

## 快速開始

Linux 上用一鍵安裝腳本：

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash
```

macOS 不要加 sudo：

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | bash
```

腳本會安裝 Python、ffmpeg 和相依套件，設定開機自動啟動，最後印出管理網頁的網址。Windows 或想自己控制每一步的，見 [安裝](https://github.com/MiCat-S/Mi302/wiki/安裝) 的手動安裝。

裝好後用瀏覽器開 `http://<主機>:8096/web`：

1. 建立管理員帳號。
2. 新增媒體庫，選伺服器上的資料夾。
3. 在「115 網盤」分頁掃碼登入，新增同步任務，按「增量同步」。第一次會先自動全量同步。
4. 有 MoviePilot 的話，在「MoviePilot」分頁填網址和 API 令牌。
5. 在播放器新增 Emby 伺服器，位址填 `http://<主機>:8096`，用剛才的帳號登入。

每一步的細節見 [首次設定](https://github.com/MiCat-S/Mi302/wiki/首次設定)。

## 文件

| 頁面 | 內容 |
| --- | --- |
| [安裝](https://github.com/MiCat-S/Mi302/wiki/安裝) | 一鍵安裝、手動安裝、`mi302` 管理指令、更新與移除 |
| [首次設定](https://github.com/MiCat-S/Mi302/wiki/首次設定) | 網頁上的設定步驟、使用者、設定檔怎麼運作 |
| [115 網盤與同步](https://github.com/MiCat-S/Mi302/wiki/115-網盤與同步) | 登入、同步任務、增量與全量同步、熔斷 |
| [媒體庫與掃描](https://github.com/MiCat-S/Mi302/wiki/媒體庫與掃描) | 資料夾結構、部分掃描、拼音排序搜尋、演職人員中文化 |
| [播放與外網連線](https://github.com/MiCat-S/Mi302/wiki/播放與外網連線) | 302 播放流程、播放網址的登入、下載、反向代理 |
| [片頭片尾跳過](https://github.com/MiCat-S/Mi302/wiki/片頭片尾跳過) | 怎麼學出片頭片尾、播放器拿到什麼、怎麼測試 |
| [媒體資訊與探測](https://github.com/MiCat-S/Mi302/wiki/媒體資訊與探測) | `X-mediainfo.json`、ffprobe 探測、115 的限速 |
| [MoviePilot 整合](https://github.com/MiCat-S/Mi302/wiki/MoviePilot-整合) | 刮削、補全缺集、把 Mi302 當成 Emby、媒體庫封面 |
| [備份與還原](https://github.com/MiCat-S/Mi302/wiki/備份與還原) | 自動備份、下載備份、還原步驟 |
| [設定檔參考](https://github.com/MiCat-S/Mi302/wiki/設定檔參考) | `config.yaml` 每一項的說明和預設值 |
| [日誌與常見問題](https://github.com/MiCat-S/Mi302/wiki/日誌與常見問題) | 日誌、連不上、權限、國內網路、忘記密碼 |
| [技術細節與開發](https://github.com/MiCat-S/Mi302/wiki/技術細節與開發) | 請求流程、已實作的 Emby 端點、程式結構、測試 |

## 常用指令

一鍵安裝後可以用 `mi302` 管理：

| 指令 | 作用 |
| --- | --- |
| `mi302 status` | 是否在執行、網址、版本 |
| `mi302 logs` | 即時看日誌 |
| `mi302 restart` | 重新啟動 |
| `mi302 update` | 更新到最新版，設定和資料不動 |
| `mi302 reset-password admin 新密碼` | 忘記密碼時重設 |

## 致謝

- 302 播放流程參考 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 `embyreverseproxy` 外掛。
- 媒體資訊檔案與 [StrmAssistant（神醫助手）](https://github.com/sjtuross/StrmAssistant) 相容；ffprobe 到 Emby 欄位的對照改寫自 [xiao-vvv/emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo)（MIT 授權，版權聲明保留在 `embyserver/mediainfo.py`）。
- 刮削和訂閱交給 [MoviePilot](https://github.com/jxxghp/MoviePilot)。
- 拼音用 [pypinyin](https://github.com/mozillazg/python-pinyin)，繁簡轉換用 [zhconv](https://github.com/gumblex/zhconv)。

## 開發

```bash
pip install -r requirements.txt pytest
python -m pytest
```

程式結構和測試方式見 [技術細節與開發](https://github.com/MiCat-S/Mi302/wiki/技術細節與開發)。Wiki 的原始檔在 [`docs/wiki`](docs/wiki)，改文件請改那裡，再用 `docs/publish-wiki.sh` 發布。
