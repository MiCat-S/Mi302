[繁體中文](115-網盤與同步) | [简体中文](115-网盘与同步) | **English**

This page covers logging in to 115 Cloud (115 網盤), turning 115 folders into local `.strm` files, and how incremental sync, full sync, deletion and the circuit breaker work. The web admin page is only in Traditional Chinese, so button and field names below are given in English with the original label in parentheses.

## Logging in to 115

Mi302 logs in to 115 by itself. You do not need MoviePilot or any other 115 tool. The login is stored in the database (`data/library.db`), not in the config file.

### QR-code login

1. Open the **115 Cloud** tab (115 網盤) of the web admin page.
2. On the **Account** card (帳號), click **Scan QR code** (掃碼登入).
3. Scan the QR code with the 115 mobile app and confirm on the phone.

The status next to the code changes from "waiting for scan" (等待掃描…) to "scanned, confirm on the phone" (已掃描，請在手機上確認), and a "115 login succeeded" (115 登入成功) message appears. If the code expires, click the button again. Once logged in, the button reads **Scan again** (重新掃碼登入).

A QR-code login takes one of 115's device slots. 115 allows one login per device type, so another login of the same type is kicked out. The default type is Alipay mini program. If you use that type yourself, expand **Advanced: device type used by QR login, 115 open platform** (進階：掃碼佔用的裝置類型、115 開放平台) on the **Account** card, change **Device type used by QR login** (掃碼登入佔用的 115 裝置類型), click **Save device type and AppID** (儲存裝置類型和 AppID), then **Scan again**. The new type only applies from the next scan.

| Option | Value in the config file (`p115.app`) |
| --- | --- |
| Alipay mini program (支付寶小程式, default) | `alipaymini` |
| WeChat mini program (微信小程式) | `wechatmini` |
| 115 TV (115 TV 版) | `tv` |
| 115 Manager, Android (115 管理（Android）) | `qandroid` |
| 115, iOS (115（iOS）) | `ios` |
| 115, Android (115（Android）) | `android` |
| Web (網頁版) | `web` |

### Pasting a cookie

You can also use the cookie of a browser that is logged in to 115:

1. On the **Account** card, expand **Paste a cookie instead** (改用貼上 cookie).
2. Paste the cookie into **115 cookie**. It looks like `UID=…; CID=…; SEID=…`.
3. Click **Save cookie** (儲存 cookie).

The cookie may only contain ASCII letters, digits and symbols, on a single line. If it contains Chinese characters, an ellipsis (…) or a line break, Mi302 rejects it and asks you to copy it from the browser again. Mi302 does not test the cookie when saving it; the **Cookie** row on the Account card shows whether it works.

A cookie can also go into `p115.cookies` in the config file. Mi302 only reads it at startup, and only when the database has no 115 login yet, so a change needs a restart. If you log out of 115 while `p115.cookies` is still set, the next start logs in with it again.

### Logging out

Click **Log out of 115** (登出 115) and confirm. After logging out, Mi302 cannot sync or play anything from 115 until you log in again. Logging out does not remove an open-platform authorization; use **Revoke** (取消授權) for that.

## Account card

After login, the **Account** card on the 115 Cloud tab shows:

- Avatar, account name and UID.
- VIP level and expiry date, or "permanent VIP" (永久 VIP).
- Storage: used, total and free, with a usage bar.
- Whether the cookie is still valid. If not, the reason 115 gave is shown; scan the QR code again.
- How you logged in (QR code, pasted cookie, or cookie from the config file), the device type taken by the QR login, and the login time.
- With an open-platform authorization: its status and when the token expires.
- **Devices logged in to this account** (目前登入這個帳號的裝置): every device logged in to the account, with Mi302 marked as "this server" (本伺服器). This shows whether another login of the same type has kicked Mi302 out.

Account data is cached for one minute. The **Refresh** button (重新整理) in the card's corner queries 115 right away. The **Overview** tab (概覽) has a summary card and shows used storage as a percentage.

## 115 open platform (advanced)

Most users can skip this. It is only for people who registered their own application on the [115 open platform](https://open.115.com) and have an AppID.

1. On the **Account** card, expand **Advanced: device type used by QR login, 115 open platform** (進階：掃碼佔用的裝置類型、115 開放平台).
2. Enter the AppID and click **Authorize** (授權).
3. Scan and authorize with the 115 mobile app.

Once authorized, folder lookups, folder listings, new-file listings and direct links go through the open platform first and fall back to the QR or cookie login when it fails. The token lasts about two hours; Mi302 renews it automatically when it is about to expire. If 115 refuses the renewal, authorize again. **Revoke** (取消授權) removes the authorization. You can pre-fill the AppID and keep it with **Save device type and AppID** in the same section (`p115.open_app_id`).

Reading life events and exporting the directory tree need a QR or cookie login. With only the open platform, incremental sync only picks up new files by modification time, and full sync lists folders one level at a time.

## Sync tasks

A sync task maps one 115 folder to one local folder. On the **Sync tasks** card (同步任務) of the 115 Cloud tab:

1. **115 folder** (115 目錄): a path on 115, for example `/影視/電影`, or click **Browse** (瀏覽).
2. **Local folder** (放到本機資料夾): a folder on the server, for example `/media/movies/115`, or click **Browse**.
3. Click **Add task** (新增任務).

Rules:

- The local folder must be an absolute path starting with `/`.
- Two tasks cannot use the same local folder, or folders inside each other. Otherwise cleaning up stale strm files for one task would delete the other's files.
- The local folder must be inside a library, or players will not see it. If it is not, the task shows "not in any library, players cannot see it" (不在任何媒體庫裡，播放器看不到). A subfolder of a library folder works well, for example `/media/movies/115`.
- The folder structure on 115 is kept as it is.

The task list shows when each task last ran an incremental and a full sync. The trash icon deletes a task; strm files it already created stay. Tasks are stored in `p115.strm.tasks` in the config file.

## strm files

For every video on 115, sync creates a `.strm` file with the same name. These extensions count as video: `.mkv`, `.mp4`, `.m4v`, `.avi`, `.ts`, `.m2ts`, `.mov`, `.wmv`, `.flv`, `.webm`, `.rmvb`, `.mpg`, `.mpeg`, `.iso`, `.3gp`.

A strm file holds a short link back to Mi302:

```
http://192.168.1.10:8096/d/abcdefghijklmnopq.mkv
```

- The middle part is the pickcode, the 17-character code 115 uses to identify a file. At playback Mi302 uses it to fetch a direct link from 115 and redirects the player there with HTTP 302. See [Playback](Playback).
- The extension is the original file's extension, so players and the scanner know the container format.
- With **Append the original file name to strm URLs** (strm 網址後附上原檔名) on the **Sync options** card (同步選項), the link ends with `?/original-file-name`. This is only for humans; Mi302 ignores it.
- **Skip videos smaller than (MB)** (略過小於這個大小的影片（MB）, `p115.strm.min_size_mb`) in the sync options skips small videos when set above 0. For example, 50 skips most trailers.

### Server address in strm files

Mi302 picks the address in this order:

1. **Server URL in strm files** (strm 裡的伺服器網址, `p115.strm.base_url`) on the Sync options card, if set.
2. The address in your browser's address bar the last time you opened the 115 Cloud tab or started a sync. So open the admin page with an address players can reach, such as `http://192.168.1.10:8096`, not `localhost`.
3. Otherwise `http://127.0.0.1:<port>`.

The address in use is shown on the **Sync** card (同步) as "server address in strm" (strm 內的伺服器位址). Behind a reverse proxy, or for access from outside your network, set the public address. See [Playback](Playback).

After you change the server URL or the file-name option and save, Mi302 rewrites the existing strm files in the background, and the save message says so. This only reads and writes local files: it makes no requests to 115 and needs no sync or rescan. The pickcode does not change, so existing media info is kept. Only strm files that Mi302 created inside the sync task folders are changed; strm files from other tools (other URLs, local paths) are left alone.

Next to the address on the **Sync** card there is **Rewrite existing strm to this address** (把現有 strm 改成這個網址), to run it by hand at any time, for example when the setting is empty and you opened the admin page from a new address. The line below it shows how many files the last rewrite changed.

When a file on 115 is replaced (another file in the same place, so the pickcode changes), sync rewrites the strm file and deletes the old `X-mediainfo.json` so it gets probed again. See [Media Info](Media-Info).

## Metadata download

With **Also download nfo, posters and subtitles from 115** (一併下載 115 上的 nfo、海報、字幕, `p115.strm.download_metadata`, on by default), these files on 115 are downloaded to the matching local location: `.nfo`, `.jpg`, `.jpeg`, `.png`, `.webp`, `.srt`, `.ass`, `.ssa`, `.sup`, `.vtt`.

- A local file with the same size is not downloaded again.
- Failed downloads appear in the sync errors and are retried by the next full sync.
- Mi302 does not scrape by itself. When 115 has no nfo files and posters, let MoviePilot do the scraping (fetching metadata and artwork). See [MoviePilot](MoviePilot).

## Incremental sync and full sync

| | Incremental sync | Full sync |
| --- | --- | --- |
| Based on | 115 life events, plus a scan by modification time | 115 directory-tree export, plus one listing of all files |
| Handles | uploads, received files, copies, moves, renames, deletions | compares the whole folder again and fills gaps |
| Speed | fast, reads only the events since the last sync | fine with many folders, but waits for 115 to build the tree |
| Use for | running every few minutes | running weekly (the default) to catch anything missed |

### Incremental sync

Life events are 115's activity log: a record of every operation in your drive. Incremental sync has two steps:

1. **Read life events.** Mi302 reads the events since the last sync. If a file has several events, only the last one counts, because that is its current state.
   - Upload, receive, copy, or move into the synced folder from outside: create the strm file. When a whole folder moves in, Mi302 lists its contents.
   - Move or rename inside the synced folder (files or folders): the local strm file moves too, together with the nfo, posters, subtitles and `X-mediainfo.json` that share its name (for example the ones MoviePilot scraped), so nothing needs to be scraped again. When a folder moves onto a folder that already exists, the two are merged; files already at the destination are not overwritten.
   - Delete, or move out of the synced folder: with **Follow deletions** (跟著刪) on, delete the local strm file and its metadata; otherwise leave it.
   - Events that do not change files, such as views, stars and tags, are ignored.
2. **Scan by modification time.** Mi302 asks 115 for files in the synced folder modified since the last sync (with 10 extra minutes of overlap). This catches uploads that produce no event, such as offline downloads or uploads by third-party tools. The list is newest first and stops at the first old file, so one request is usually enough.

Life events only carry file ids. So during full sync Mi302 records which local path each 115 file and folder maps to, and incremental sync uses that index to find the local copy.

### Full sync

Full sync has three steps:

1. **Export the directory tree.** Mi302 asks 115 to build a tree of the whole synced folder, the same as "export directory tree" (导出目录树) on the 115 website. This returns the names of every folder and file at once. 115 puts the export file in your drive's root folder; Mi302 deletes it after reading. If deleting fails, the log names the file so you can delete it yourself. Mi302 waits up to 15 minutes for 115 to build the tree.
2. **List all files.** One listing returns the pickcode, size and parent folder of every file under the synced folder, 1150 files per request.
3. **Match paths.** The tree has only names and the file list has only folder ids, so Mi302 matches them by the file names inside each folder. Only folders whose file names are too generic (for example two seasons that both contain just `01.mkv`) and folders that contain only subfolders (for example a series folder with only season folders) need a separate path lookup. Results are remembered, so the next full sync does not look them up again.

This avoids listing every folder one by one; even thousands of folders take only a few requests. Mi302 falls back to listing folder by folder in these cases, waiting **Seconds to wait before listing each 115 folder** (每列一個 115 目錄前等待的秒數, `p115.strm.request_delay`, default 0.2) before each folder:

- The export, the file listing or a path lookup fails, for example because another directory-tree export is running on 115.
- Only the open platform is logged in; the tree export needs a QR or cookie login.
- The listing has more than 10% fewer videos than the tree, which means it is incomplete.

A few videos that are in the tree but missing from 115's listing are not treated as deleted; their strm files are kept this time. The sync result mentions all of these cases.

Before a full sync starts, Mi302 turns on the "recent records" (最近记录) switch of 115's life feature (115 records no events while it is off) and remembers the newest event. Changes made during the full sync are picked up by the next incremental sync.

### When incremental sync runs a full sync instead

- The task has never had a full sync, including new tasks and tasks carried over from an older version: the first incremental sync runs a full sync to build the index.
- The last event Mi302 read is no longer in 115's window (115 keeps only about the latest 10,000 events): a full sync runs so nothing in between is missed.

The sync result lists these tasks after "switched to full sync" (已改跑全量：).

## Starting a sync

- **Buttons**: **Incremental sync** (增量同步) and **Full sync** (全量同步) on the **Sync** card of the 115 Cloud tab, or **Incremental sync** and **Full** (全量) on the **115 sync** card (115 同步) of the Overview tab. Only one sync runs at a time; clicking during a sync shows "already syncing" (已經在同步中).
- **Schedule**: two fields on the **Sync options** card (同步選項). Click **Save sync options** (儲存同步選項) after changing them.
- **Command line**: see below.

### Schedule

| Field | Config key | Default | Meaning |
| --- | --- | --- | --- |
| Auto incremental sync interval (minutes) (自動增量同步間隔（分鐘）) | `p115.strm.interval` | 0 | 0 = off; 5 is a good value |
| Auto full sync interval (hours) (自動全量同步間隔（小時）) | `p115.strm.full_interval` | 168 | 0 = off; 168 = weekly |

- Mi302 checks once a minute, so a new interval applies right away.
- The incremental interval counts from when Mi302 started.
- The full interval counts from the last full sync of the task that has gone longest without one. That time is stored in the database, so restarting does not reset it. A task must have synced once before scheduled full syncs start.
- When both are due, only the full sync runs, for all tasks.
- Nothing runs while 115 is not logged in, there are no tasks, or the circuit breaker is open.

### Command line

```bash
# full sync
.venv/bin/python -m embyserver -c config.yaml --sync-115
# incremental sync
.venv/bin/python -m embyserver -c config.yaml --sync-115 incremental
```

This syncs, scans the changed places and sends new files to MoviePilot according to your settings, then exits. It suits cron and similar schedulers. Run it as the account that runs Mi302, from the folder Mi302 normally starts in, so it uses the same data (`server.data_dir`, `./data` by default, is relative to that folder). For the one-line installer on Linux:

```bash
cd /opt/mi302/config
sudo -u <user> env PYTHONPATH=/opt/mi302 /opt/mi302/.venv/bin/python -m embyserver -c config.yaml --sync-115 incremental
```

This is a separate process that does not know whether the server is syncing at the same moment, so avoid running both at once.

## Sync result

The **Sync** card (also on the Overview tab) shows the last sync:

- Status: done (完成) or with errors (有錯誤), incremental or full, and how long ago.
- Counters: life events (生活事件) and moved (搬移), for incremental only; new files (新檔案), updated (更新), unchanged (未變), metadata (中繼資料), deleted (刪除).
- Notes, for example a switch to full sync, a fallback to folder-by-folder listing, or deletions skipped for safety.
- Errors: the first 5, the rest are in the [log](Logs-and-FAQ). An error in one task does not stop the others.

## After a sync

- **Scan**: with **Rescan libraries after sync** (同步完自動重新掃描媒體庫, `p115.strm.scan_after_sync`, on by default) on the Sync options card, only the series or movies with new, updated, moved or deleted files are rescanned. See [Library and Scanning](Library-and-Scanning).
- **Scrape**: new strm files are sent to MoviePilot (同步產生新的 strm 後自動送去刮削). See [MoviePilot](MoviePilot).
- **Probe**: with batch probing (批次探測) and probing after sync (同步產生新的 strm 後自動探測) on, new strm files and files replaced on 115 are probed for media info in the background. See [Media Info](Media-Info).
- **Fill missing episodes**: with **Fill after full sync** (全量同步後自動補全) on, every full sync sends all series that have a tmdbid to MoviePilot as subscriptions.

## Follow deletions

**Follow deletions** (跟著刪, `p115.strm.delete_stale`) in the sync options is off by default. When it is on, videos deleted on 115 or moved out of the synced folder also lose their local strm file and scraped metadata.

What gets deleted:

- The video's strm file.
- Metadata with the same name as the strm file, such as `X.nfo`, `X-poster.jpg`, `X.zh.srt` and `X-mediainfo.json`. If the folder also has `X-2.strm`, then `X-2.nfo` belongs to `X-2` and is not deleted with `X`.
- nfo files, images and subtitles in folders that no longer contain any video, and folders left empty.

Files with any other extension are never touched.

Incremental sync deletes based on events: deletion, moving out of the synced folder, or renaming to a name that is not a video. Full sync deletes local strm files that 115 no longer lists.

To avoid deleting by mistake, full sync skips deletion in these cases and says so in the sync result:

- Some folders' paths could not be resolved, so some files have an unknown location.
- 115 listed no videos at all while the local folder has videos. Usually the 115 folder is wrong, or 115 returned an incomplete answer.
- Videos in the tree but missing from 115's listing keep their strm files. They are only deleted when a later full sync finds them in neither.

When 115 returns an error or rate-limits the request, the whole task fails instead of being treated as an empty folder.

If a file on 115 only changed the letter case of its name: on a case-insensitive disk such as macOS, old and new are the same file and nothing is deleted; on a case-sensitive disk the old strm file is deleted and its nfo and posters are renamed to the new name.

Deleting a sync task never deletes the strm files it created, whether this option is on or off.

## Duplicate files

The **Duplicate files** card (重複檔案) on the **Tools** tab (整理) finds duplicate videos on 115 and deletes the extra copies. One **Find duplicates** run looks for two kinds, shown on two tabs:

| Tab | What counts as a duplicate | Default |
| --- | --- | --- |
| Identical (完全相同) | Same SHA1 (a fingerprint of the content) and size, even with different names | One copy suggested to keep, the rest ticked |
| Versions (不同版本) | The same movie or episode in your library, but different files, for example 1080p and 2160p | Nothing ticked; you choose |

### Finding duplicates

1. **Scope**: the sync tasks' 115 folders by default. **Pick a 115 folder…** (改選 115 目錄…) lets you choose any folder, for example the whole drive `/`.
2. **Find duplicates** (找重複): Mi302 lists every video in the scope. 115 includes the SHA1 in its file listings, so forty thousand videos take about forty requests. For files outside the sync folders, the folder path is looked up once per folder. Rate limiting trips the circuit breaker as usual.
3. **Results**: the top shows how many groups each kind has and how much deleting per the suggestions would free. Each group is a card, largest savings first, 20 groups per page, searchable.

### Identical files

- One copy per group is suggested to keep: the one with a local strm first, then the earliest upload. The others are ticked, and you can change any tick.
- Each copy shows its 115 path, its upload time, and the tags suggested to keep (建議保留), has strm (有 strm), outside sync folders (不在同步目錄) and has watch history (有觀看紀錄).

### Versions

- How the same title is recognised: movies by tmdbid (from a scraped nfo), otherwise by title and year; episodes by series, season and episode number. Only files in the sync folders that have a strm are considered, so the library knows what they are.
- Not treated as duplicates: split files (CD1, Part 2) and files holding several episodes (E01E02, E01-02). Different cuts such as director's cut or extended are grouped separately. Groups where every copy is identical are left to the Identical tab.
- Each version shows resolution, HDR or Dolby Vision, codec, the first audio track, the number of audio and subtitle tracks, and size. Quality comes from extracted media info (see [Media Info](Media-Info)); anything missing is guessed from the file name and marked from file name (看檔名).
- The files differ, so nothing is ticked by default. The suggestion keeps the highest resolution, then the largest file: **Tick as suggested** (照建議勾選) on a group ticks that group, and **Tick all as suggested** (全部照建議勾選) at the top ticks every group, including pages you have not opened.

### Deleting

- **Delete ticked** (刪除勾選的) handles every group on the current tab, including pages you have not opened; **Delete ticked in this group** (刪這一組勾選的) handles one group. The confirmation shows the count and size, and more than 50 files asks a second time. Every group must keep at least one copy.
- Files go to the 115 recycle bin and can be restored there. One request handles up to a hundred files.
- The local strm and its same-name nfo, posters, subtitles and `X-mediainfo.json` are deleted too (regardless of the follow-deletions setting), and only the affected series or movies are rescanned.
- Watch history of the deleted copy (played, resume position, favourite) moves to the kept copy, per user.
- **Recently deleted** (最近刪掉的) at the bottom of the card lists what was deleted and where, to find it in the recycle bin.
- Deleting needs QR-code (cookie) login; it is not available with only the open platform.
- Results reflect the moment you clicked **Find duplicates**. If files were moved, renamed or deleted on 115 since, find again before deleting.

## Circuit breaker

115's firewall is sensitive: sending more requests after being rate-limited only extends the block. So when 115 rate-limits Mi302 or the login becomes invalid, Mi302 pauses background work: sync and media-info probing.

| | Rate limiting | Login invalid |
| --- | --- | --- |
| Detected by | HTTP 405 or 429 from 115, error code 770004, or a message containing `访问上限`, `访问被阻断`, `操作太频繁`, `请求过于频繁`, `登录异常` or `Too Many Requests` | error code 99 or 990001, or a message such as `请重新登录`, `登录失效`, `登录已过期` or `登录超时` |
| Pause | 45 minutes, then resumes by itself | until you scan the QR code again, paste a new cookie, or log out |
| Shown as | a yellow notice: 115 rate-limited, background sync paused, retry in about N minutes, playback not affected | a red notice: 115 login invalid, please scan the QR code again |

While the breaker is open:

- No sync starts, and the sync result says why. A running sync stops as soon as it is rate-limited, and the remaining tasks are skipped.
- Scheduled syncs wait, and media-info probing stops.
- Playback does not go through the breaker and still fetches direct links from 115.

The notice appears on the Account cards of the 115 Cloud and Overview tabs and on the **Media info** card (媒體資訊) of the Tools tab.

For one hour after recovering from rate limiting, media-info probing runs slower: for the first 30 minutes the interval between direct-link requests is 4 times longer and the hourly cap is a quarter, for the next 30 minutes 2 times and a half, then back to normal. Sync is not slowed down. The Media info card says it is slowing down while this lasts.

## Other settings

- **Seconds to wait before listing each 115 folder** (`p115.strm.request_delay`, default 0.2): the pause before each request when listing folder by folder or looking up a folder path. Too fast may get you rate-limited. On the 115 Cloud tab → Sync options.
- `p115.timeout` (default 15 seconds): timeout for requests to 115. Config file only; needs a restart.

Every key is described in the [Configuration Reference](Configuration-Reference).
