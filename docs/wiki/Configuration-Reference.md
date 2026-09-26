[繁體中文](設定檔參考) | [简体中文](配置文件参考) | **English**

This page lists every key in the config file `config.yaml`: what it does, its default, the allowed range, whether you can change it in the web admin page, and whether it needs a restart. Normally you set everything in the web admin page and never edit the file. The web admin page is only in Traditional Chinese; labels below are translated, with the original in parentheses.

## Where the config file is

| Installation | Location |
| --- | --- |
| One-line installer (Linux) | `/opt/mi302/config/config.yaml` |
| One-line installer (macOS) | `~/Mi302/config/config.yaml` |
| Manual Python install | `config.yaml` in the folder you run the command from |

Use `-c <path>` at startup to choose a file. Without it, Mi302 uses the environment variable `EMBYSERVER_CONFIG`, and otherwise `config.yaml` in the current folder. The top of the **Advanced settings** tab (進階設定) shows which file is in use.

## How the config file works

- **Created automatically**: if there is no config file at the first start, Mi302 starts with defaults and writes a commented config file.
- **Written back from the web page**: saving settings in the web admin page regenerates the whole file from a template. The previous file is kept as `config.yaml.bak`. Your own comments and unknown keys are not kept.
- **Editing the file by hand**: after editing, open or refresh the web admin page. Mi302 notices that the file changed, reads it again and applies it.
- **Mistakes**: if the file is broken at startup (invalid YAML, a required key missing), Mi302 does not start. The reason is printed to the startup output, not to the log file: `journalctl -u mi302` for the Linux service, `data/logs/console.log` on macOS and on systems without systemd, or the terminal for a manual start. If you break it while Mi302 is running, the web page shows an error at the top and Mi302 keeps using the last good settings.
- **Unknown keys**: typos and keys left over from old versions are ignored with a warning (設定檔 … 底下不認得 …，已略過, "unknown key … under …, ignored"). Startup continues.

### Keys that need a restart

These are only read at startup. Restart Mi302 after changing them (`mi302 restart` with the one-line installer):

- `server.host`, `server.port`, `server.data_dir`
- `users`
- `p115.cookies`, `p115.timeout`
- `redirect.resolve_timeout`

Every other key takes effect as soon as you save it in the web page, or when you refresh the web page after editing the file.

### Checks and ranges

When you save in the web page, or when Mi302 reads an edited file, it checks the settings. Invalid settings are not applied and the reason is shown. Numbers outside their range are changed to the nearest allowed value:

| Key | Allowed range |
| --- | --- |
| `server.backup_keep` | 0–90 |
| `server.log_level` | `info` or `debug`; anything else becomes `info` |
| `moviepilot.concurrency` | 1–8 |
| `mediainfo.concurrency` | 1–3 |
| `mediainfo.interval` | 0.5–60 |
| `mediainfo.timeout` | 10–3600 |
| `mediainfo.hourly_limit` | 0–100000 |

Other checks:

- Libraries: the name must not be empty or repeated, and each library needs at least one folder. Any `type` other than `tvshows` becomes `movies`.
- Sync tasks: both the 115 folder and the local folder are required; the local folder must be an absolute path; two tasks cannot use the same local folder or folders inside each other.
- `moviepilot.url` must start with `http://` or `https://`; a trailing `/` is removed.
- Path mappings and path rules with an empty `from` or `to` are dropped.
- An empty `mediainfo.ffprobe` becomes `ffprobe`.

## Data that is not in the config file

These live in the database `<data_dir>/library.db`, not in the config file, and are included in the daily backup (see [Backup and Restore](Backup-and-Restore)):

- Accounts and passwords (stored as hashes only). Once an account exists, adding users, changing passwords or deleting users on the **Users** tab (使用者) never writes back to the config file.
- The 115 login: the QR or pasted cookie, and the 115 open-platform token.
- API keys created in the web admin page.
- 115 sync progress and index, and the automatically detected server address for strm files.
- Watch history, media info, learned intros and credits, and so on.

## server

| Key | Default | Meaning | In the web page |
| --- | --- | --- | --- |
| `name` | `Emby Server` | Server name shown in players | Advanced settings → Server (伺服器) → **Server name** (伺服器名稱) |
| `host` | `0.0.0.0` | Address to listen on. `0.0.0.0` means all network interfaces; `127.0.0.1` means only this machine can connect | File only; restart |
| `port` | `8096` | Port for the web admin page and players | File only; restart. With the one-line installer you can also rerun the installer with `--port`; see [Installation](Installation) |
| `data_dir` | `./data` | Folder for the database, logs, backups and uploaded covers. A relative path is relative to the working folder Mi302 starts in (`config/` for the installer's service), not to the config file's folder | File only; restart |
| `public_users` | `true` | Whether players' login screens list the user names | Advanced settings → Server → **List user names on the player login screen** (播放器登入畫面列出使用者名稱) |
| `log_level` | `info` | `info` = normal; `debug` = verbose, also logs every player request | Logs tab (日誌) → **Verbose mode** (詳細模式) |
| `backup_keep` | `7` | Daily automatic backups to keep, 0–90; 0 = no automatic backups | Advanced settings → Backup (備份) → **Keep this many** (保留幾份) |
| `chinese_people` | `true` | Show Chinese names for cast and crew | Advanced settings → Chinese (中文化) → **Show Chinese names for cast and crew** (演職人員顯示中文名) |
| `chinese_genres` | `true` | Show genres in Chinese (Action → 动作), Traditional converted to Simplified; turning it off takes effect after a rescan | Advanced settings → Chinese → **Show genres in Chinese** (類型顯示中文) |
| `intro_skip` | `true` | Learn intros and credits from playback, for players' "skip intro" | Libraries tab (媒體庫) → Intro and credits (片頭片尾) → **Learn intros and credits and send them to players** (學片頭片尾並給播放器) |
| `allow_download` | `true` | Players may download videos (login required). When off, players show no download option and the download URL is refused | Advanced settings → Server → **Allow players to download videos** (允許播放器下載影片) |

Related pages: [Logs and FAQ](Logs-and-FAQ), [Library and Scanning](Library-and-Scanning), [Intro and Credits](Intro-and-Credits), [Playback](Playback), [Backup and Restore](Backup-and-Restore).

## users

Accounts created at startup. The default is `users: []`, and it is best left empty: the first time you open the web admin page, it asks you to create an administrator.

```yaml
users:
  - name: admin
    password: your-password
    admin: true
```

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | Account name |
| `password` | empty | Password, in plaintext |
| `admin` | `false` | Administrator. Only administrators can open the web admin page |

- File only; restart.
- At every start, accounts in this list that do not exist yet are created. Existing accounts are not changed, so changing a password here has no effect.
- An account deleted in the web page is created again at the next start if it is still in this list.
- Passwords are in plaintext, also in `config.yaml.bak` and in the `.yaml` backups. Once the accounts exist you can delete this block; the accounts stay in the database.

## libraries

Libraries. The default is `libraries: []`. Create them on the **Libraries** tab with **Add library** (新增媒體庫), then **Save and scan** (儲存並掃描).

```yaml
libraries:
  - name: Movies
    type: movies
    paths:
      - /media/movies
  - name: TV
    type: tvshows
    paths:
      - /media/tv
```

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | Library name; must be unique |
| `type` | `movies` | `movies` or `tvshows` |
| `paths` | required | List of folders, at least one. The old single `path` key is still accepted |

When libraries change, only the changed libraries are rescanned; a removed library's items are removed with it. See [Library and Scanning](Library-and-Scanning).

## p115

The 115 login. Log in with the QR code in the web page; see [115 Cloud Sync](115-Cloud-Sync).

| Key | Default | Meaning | In the web page |
| --- | --- | --- | --- |
| `cookies` | empty | A 115 cookie, same as pasting one in the web page. Only read at startup, and only when the database has no 115 login yet. ASCII only, on one line | File only; restart. A cookie pasted in the web page is stored in the database, not here |
| `app` | `alipaymini` | 115 device type taken by the QR login; another login of the same type is kicked out. One of `alipaymini`, `wechatmini`, `tv`, `qandroid`, `ios`, `android`, `web` | Advanced settings → 115 and strm (115 與 strm) → **Device type used by QR login** (掃碼登入佔用的 115 裝置類型); applies at the next scan |
| `timeout` | `15.0` | Timeout in seconds for requests to 115 | File only; restart |
| `open_app_id` | empty | 115 open-platform AppID; only for people who registered their own application | Advanced settings → 115 and strm → **115 open platform AppID** (115 開放平台 AppID) |

## p115.strm

Options for syncing into strm files. On the **Sync options** card (同步選項) of the 115 Cloud tab, click **Save sync options** (儲存同步選項); for fields under Advanced settings, click **Save settings** (儲存設定) at the bottom of the page.

| Key | Default | Meaning | In the web page |
| --- | --- | --- | --- |
| `interval` | `0` | Minutes between automatic incremental syncs; 0 = off, 5 is a good value | 115 Cloud → Sync options → **Auto incremental sync interval (minutes)** (自動增量同步間隔（分鐘）) |
| `full_interval` | `168` | Hours between automatic full syncs; 0 = off, 168 = weekly | 115 Cloud → Sync options → **Auto full sync interval (hours)** (自動全量同步間隔（小時）) |
| `min_size_mb` | `0` | Videos smaller than this many MB get no strm file; 0 = no limit | 115 Cloud → Sync options → **Skip videos smaller than (MB)** (略過小於這個大小的影片（MB）) |
| `download_metadata` | `true` | Also download nfo files, images and subtitles from 115 | 115 Cloud → Sync options → **Also download nfo, posters and subtitles from 115** (一併下載 115 上的 nfo、海報、字幕) |
| `delete_stale` | `false` | When a video is deleted on 115 or moved out of the synced folder, delete its local strm file and scraped metadata too | 115 Cloud → Sync options → **Follow deletions** (跟著刪) |
| `base_url` | empty | Server address written into strm files; empty = the address you used to open the web page | Advanced settings → 115 and strm → **Server URL in strm files** (strm 裡的伺服器網址) |
| `include_name` | `false` | Append `?/original-file-name` to strm URLs, for humans | Advanced settings → 115 and strm → **Append the original file name to strm URLs** (strm 網址後附上原檔名) |
| `request_delay` | `0.2` | Seconds to wait before each request when listing 115 folders one by one or looking up a folder path | Advanced settings → 115 and strm → **Seconds to wait before listing each 115 folder** (每列一個 115 目錄前等待的秒數) |
| `scan_after_sync` | `true` | Rescan the changed places after a sync | Advanced settings → 115 and strm → **Rescan libraries after sync** (同步完自動重新掃描媒體庫) |
| `tasks` | `[]` | Sync tasks, see below | 115 Cloud → **Sync tasks** (同步任務) |

`interval` and `full_interval` have no upper limit; 0 or a negative number turns them off.

Format of `tasks`:

```yaml
p115:
  strm:
    tasks:
      - remote: /影視/電影
        local: /media/movies/115
```

- `remote`: the folder on 115. A missing leading `/` is added.
- `local`: the local folder for the strm files. It must be an absolute path inside a library.
- Two tasks cannot use the same `local` folder, or folders inside each other.

## moviepilot

Scraping is delegated to MoviePilot. See [MoviePilot](MoviePilot).

| Key | Default | Meaning | In the web page |
| --- | --- | --- | --- |
| `url` | empty | MoviePilot address, for example `http://192.168.1.10:3000`; must start with `http://` or `https://` | MoviePilot tab → Connection (連線) → **MoviePilot URL** (MoviePilot 網址) |
| `api_token` | empty | MoviePilot's API token (Settings → System → API token) | MoviePilot → Connection → **API token** (API 令牌) |
| `username` | empty | MoviePilot account. Needed to fill missing episodes (creating subscriptions), and for older MoviePilot versions whose scrape API does not accept the token | MoviePilot → Connection → **MoviePilot account** (MoviePilot 帳號) |
| `password` | empty | MoviePilot password, in plaintext | MoviePilot → Connection → **MoviePilot password** (MoviePilot 密碼) |
| `scrape_after_sync` | `true` | Send new strm files to MoviePilot for scraping after a sync | MoviePilot → Connection → **Scrape new strm files after sync** (同步產生新的 strm 後自動送去刮削) |
| `fill_after_full_sync` | `false` | After each full sync, send every series with a tmdbid to MoviePilot as a subscription to fill missing episodes | MoviePilot → Fill missing episodes (補全缺集) → **Fill after full sync** (全量同步後自動補全) |
| `timeout` | `300` | Maximum seconds to wait for each scraped item | File only |
| `concurrency` | `3` | Items sent to MoviePilot at the same time, 1–8. Too many may hit TMDB's rate limit | MoviePilot → Connection → **Items to scrape at once** (同時刮削幾項) |
| `path_mappings` | `[]` | When the two see different paths: Mi302's path (`from`) → MoviePilot's path (`to`) | MoviePilot → Connection → **Path mappings** (路徑對應), one per line as `Mi302 path => MoviePilot path` |

```yaml
moviepilot:
  path_mappings:
    - from: /media/psf/Vo
      to: /Volumes/Vo
```

## mediainfo

Probing the videos behind strm files with ffprobe and writing `X-mediainfo.json`. On the **Media info** card (媒體資訊) of the Libraries tab, click **Save** (儲存). See [Media Info](Media-Info).

| Key | Default | Meaning | In the web page |
| --- | --- | --- | --- |
| `enabled` | `false` | Whole-library probing (the manual button and probing after sync). When off, existing `X-mediainfo.json` files are still read | Libraries → Media info → **Whole-library probing** (整庫探測) |
| `after_sync` | `true` | Probe new strm files after a sync; needs whole-library probing | Libraries → Media info → **Probe new strm files after sync** (同步產生新的 strm 後自動探測) |
| `on_demand` | `true` | Probe a movie or episode in the background when a player opens it | Libraries → Media info → **Probe when a video is opened** (打開影片時自動探測) |
| `concurrency` | `2` | Items probed at the same time, 1–3. 115 allows at most 3 connections at once | Libraries → Media info → **Items to probe at once** (同時探測幾項) |
| `interval` | `1.0` | Minimum seconds between direct-link requests to 115, 0.5–60 | Libraries → Media info → **Direct-link interval (seconds)** (取直鏈間隔（秒）) |
| `hourly_limit` | `300` | Maximum direct-link requests to 115 per hour; 0 = no limit, at most 100000 | Libraries → Media info → **Maximum per hour** (每小時最多幾次) |
| `timeout` | `300` | Maximum seconds per item, 10–3600 | File only |
| `ffprobe` | `ffprobe` | Path to ffprobe | File only |

## redirect

How strm contents are handled at playback, mainly for strm files made by other tools (for example alist URLs or local paths). For fields under Advanced settings, click **Save settings** at the bottom of the page. See [Playback](Playback).

| Key | Default | Meaning | In the web page |
| --- | --- | --- | --- |
| `resolve_redirects` | `false` | Let the server follow the upstream redirects to the end and give the player the final URL | Advanced settings → strm from other tools (其他工具產生的 strm) → **Follow upstream redirects on the server** (由伺服器先跟著上游的重導向走到底) |
| `resolve_timeout` | `10.0` | Timeout in seconds when following redirects | File only; restart |
| `cache_ttl` | `90` | Seconds to cache the final URL | File only |
| `require_auth` | `true` | Require login for playback URLs, as official Emby does. Turn it off only if a player cannot play | Advanced settings → strm from other tools → **Require login for playback URLs** (播放網址要求登入) |
| `default_container` | `mkv` | Container format to assume when a strm file does not reveal it | Advanced settings → strm from other tools → **Default video format** (看不出格式時預設的影片格式) |
| `path_rules` | `[]` | Replace a leading `from` in strm contents with `to` before redirecting the player | Advanced settings → strm from other tools → **Path rewriting** (路徑替換), one per line as `original prefix => replacement` |

```yaml
redirect:
  path_rules:
    - from: /mnt/115
      to: http://alist:5244/d/115
```

## api_keys

Fixed API keys that let other programs call the Emby API as an administrator, sent in the `X-Emby-Token` header or the `api_key` query parameter.

```yaml
api_keys:
  - a-long-random-string
```

- File only. Takes effect when you refresh the web admin page; no restart needed.
- API keys cannot be used for the web admin page's own functions (settings, backups, the 115 login, accounts).
- Keys created with **Create API key** (建立 API 金鑰) on the MoviePilot tab are stored in the database and do not appear here. Those are usually the better choice; see [MoviePilot](MoviePilot).
