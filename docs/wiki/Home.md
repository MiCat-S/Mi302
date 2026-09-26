[繁體中文](#繁體中文) · [简体中文](#简体中文) · [English](#english)

Mi302 是 API 與 Emby 相容的影片伺服器，給 115 網盤的影片用：把 115 的資料夾同步成 `.strm`，播放時 302 到 115 直鏈。

Mi302 是 API 与 Emby 兼容的视频服务器，给 115 网盘的视频用：把 115 的文件夹同步成 `.strm`，播放时 302 到 115 直链。

Mi302 is an Emby-compatible video server for 115 Cloud: it syncs 115 folders into `.strm` files and plays them by redirecting to 115 direct links.

## 繁體中文

| 頁面 | 內容 |
| --- | --- |
| [安裝](安裝) | 一鍵安裝、手動安裝、`mi302` 管理指令、更新與移除 |
| [首次設定](首次設定) | 網頁上的設定步驟、使用者、設定檔怎麼運作 |
| [115 網盤與同步](115-網盤與同步) | 登入、同步任務、增量與全量同步、熔斷 |
| [媒體庫與掃描](媒體庫與掃描) | 資料夾結構、部分掃描、拼音排序搜尋、演職人員中文化 |
| [播放與外網連線](播放與外網連線) | 302 播放流程、播放網址的登入、下載、反向代理 |
| [片頭片尾跳過](片頭片尾跳過) | 怎麼學出片頭片尾、播放器拿到什麼、怎麼測試 |
| [媒體資訊與探測](媒體資訊與探測) | `X-mediainfo.json`、ffprobe 探測、115 的限速 |
| [MoviePilot 整合](MoviePilot-整合) | 刮削、補全缺集、把 Mi302 當成 Emby、媒體庫封面 |
| [備份與還原](備份與還原) | 自動備份、下載備份、還原步驟 |
| [設定檔參考](設定檔參考) | `config.yaml` 每一項的說明和預設值 |
| [日誌與常見問題](日誌與常見問題) | 日誌、連不上、權限、國內網路、忘記密碼 |
| [技術細節與開發](技術細節與開發) | 請求流程、已實作的 Emby 端點、程式結構、測試 |

## 简体中文

| 页面 | 内容 |
| --- | --- |
| [安装](安装) | 一键安装、手动安装、`mi302` 管理命令、更新与卸载 |
| [首次设置](首次设置) | 网页上的设置步骤、用户、配置文件如何工作 |
| [115 网盘与同步](115-网盘与同步) | 登录、同步任务、增量与全量同步、熔断 |
| [媒体库与扫描](媒体库与扫描) | 文件夹结构、部分扫描、拼音排序搜索、演职人员中文化 |
| [播放与外网访问](播放与外网访问) | 302 播放流程、播放地址的登录、下载、反向代理 |
| [片头片尾跳过](片头片尾跳过) | 怎样学出片头片尾、播放器拿到什么、怎样测试 |
| [媒体信息与探测](媒体信息与探测) | `X-mediainfo.json`、ffprobe 探测、115 的限速 |
| [MoviePilot 集成](MoviePilot-集成) | 刮削、补全缺集、把 Mi302 当作 Emby、媒体库封面 |
| [备份与还原](备份与还原) | 自动备份、下载备份、还原步骤 |
| [配置文件参考](配置文件参考) | `config.yaml` 每一项的说明和默认值 |
| [日志与常见问题](日志与常见问题) | 日志、连不上、权限、国内网络、忘记密码 |
| [技术细节与开发](技术细节与开发) | 请求流程、已实现的 Emby 接口、代码结构、测试 |

## English

| Page | What it covers |
| --- | --- |
| [Installation](Installation) | Installer, manual install, the `mi302` command, updating and uninstalling |
| [First Setup](First-Setup) | Setting up in the web page, users, how the config file works |
| [115 Cloud Sync](115-Cloud-Sync) | Signing in, sync tasks, incremental and full sync, the circuit breaker |
| [Library and Scanning](Library-and-Scanning) | Folder layout, partial scans, pinyin sort and search, Chinese names |
| [Playback](Playback) | The 302 flow, playback authentication, downloads, reverse proxies |
| [Intro and Credits](Intro-and-Credits) | How intros and credits are learned, what players receive, how to test |
| [Media Info](Media-Info) | `X-mediainfo.json`, ffprobe probing, 115 rate limits |
| [MoviePilot](MoviePilot) | Scraping, filling missing episodes, Mi302 as an Emby server, library covers |
| [Backup and Restore](Backup-and-Restore) | Daily backups, downloading a backup, restoring |
| [Configuration Reference](Configuration-Reference) | Every `config.yaml` key with its default |
| [Logs and FAQ](Logs-and-FAQ) | Logs, connection problems, permissions, networks in mainland China, lost passwords |
| [Technical Details](Technical-Details) | Request flow, implemented Emby endpoints, code layout, tests |
