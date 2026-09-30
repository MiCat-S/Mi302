[繁體中文](115-網盤與同步) | [简体中文](115-网盘与同步) | **English**

This page covers logging in to 115 Cloud (115 網盤), turning 115 folders into local `.strm` files, how incremental sync, full sync, deletion and the circuit breaker work, how to browse folders on 115 and hand a wrong one to MoviePilot, and how to empty the 115 recycle bin. The web admin page is only in Traditional Chinese, so button and field names below are given in English with the original label in parentheses.

The **115 Cloud** tab has five buttons at the top: **Account** (帳號), **Sync** (同步), **Offline download** (離線下載), **Organise 115** (整理 115 網盤) and **Recycle bin** (回收站). Each one shows only its group of cards, and the tab reopens on the one you looked at last. **Sync** holds the **Sync tasks** (同步任務), **Sync** (同步) and **Sync options** (同步選項) cards. **Browse 115** (瀏覽 115) is a separate page in the sidebar.

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
- **Stop** (停止): shown on the **Sync** card while a sync runs. It stops between two files; this run's progress is not saved and no strm file is deleted, and the next sync redoes the unfinished part. Scheduled syncs carry on.
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

## Offline download

**Offline download** (離線下載) on the **115 Cloud** tab hands magnet, ed2k, http, https and ftp links to 115, which downloads them into your cloud drive (115's cloud download). Nothing goes through this machine or uses its bandwidth.

1. Paste the links under **Links** (連結), one per line. A bare torrent info hash (40 hex characters) also works and becomes a magnet link. Lines that are not links are not sent; the result says how many were left out.
2. Under **Save to** (存到), type a 115 folder or pick one with **Choose…** (選擇…); leave it empty for 115's default cloud-download folder. The last one used is remembered in this browser.
3. Click **Add download** (加入下載). A link 115 already has a task for is reported as already existing (任務已存在).

**Download tasks** (下載任務) below lists the cloud-download tasks on 115: name, size, progress (refreshed every 5 seconds while something is downloading), finished or failed. **Open folder** (打開資料夾) on a finished task shows the files in Browse 115. The top right shows how many tasks you can still add this month (115's quota, which depends on the membership level).

- **Saved inside a sync folder**: once downloaded, the next incremental sync creates the strm files and adds them to the library (and sends them to MoviePilot for scraping if that is on).
- **Saved elsewhere**: use **Organise…** (整理…) in Browse 115 to let MoviePilot move them into the library.
- **Delete…** (刪除…) removes only the task record; the downloaded files stay on 115. To delete the files as well, type 刪檔案 ("delete files") in the confirmation.
- **Add again** (重新加入), on failed tasks, first removes that failed record (not the files; otherwise 115 says the task already exists) and then adds the original link again to the original folder.
- **Clear finished** (清除已完成) and **Clear failed** (清除失敗的) remove those task records in one go without touching files.

With [115 open platform](#115-open-platform-advanced) authorisation its cloud-download API is used; otherwise the QR-code login cookie. The cookie path uses the 115 mobile app's interface, which has no official documentation, so it fails when 115 changes it; the error message is shown as is.

## Browsing 115

The **Browse 115** (瀏覽 115) page in the sidebar shows what is inside folders on 115. The first time it opens at the 115 folder of the first sync task (or at the root when there are no sync tasks), and afterwards where you left off; **Back to the sync folder** (回到同步目錄) returns to the start. Click a folder to open it; click any part of the path at the top, or **Up** (上一層), to go back. Each folder you open costs one directory listing on 115.

- Subfolders: click the name to open one. **Organise…** (整理…) on the right adds that folder to **Organise 115** (整理 115 網盤).
- Videos: size, upload time, and what Mi302's library made of them.
- Other files (nfo, images, subtitles) are listed in grey. Files load 1000 at a time; click **Load more** (再載入) at the bottom for the rest. **Select all** (全選) only selects files already loaded, and the selection count says how many are not loaded yet.
- Every row has a checkbox, with **Select all** (全選) above. **Organise selected…** (整理選取的…) adds the ticked folders to **Organise 115** (files cannot be organised on their own and are skipped); **Delete selected…** (刪除選取的…) moves them to the 115 recycle bin (restorable on 115), folders with everything inside, and for anything inside a sync folder also removes the local strm and nfo files and library entries. Before deleting, Mi302 lists the folder again and only deletes what really is in it. Deleting needs the QR-code (cookie) login to 115.

Marks next to a video:

| Mark | Meaning |
| --- | --- |
| Title S01E10 | An episode in the library. With "number guessed from the file name" (集號是從檔名猜的) or "episode number not recognised" (認不出集號) next to it, the number may be wrong |
| Movie: Title (Year) (電影：…) | A movie in the library |
| strm exists, not scanned yet (有 strm，還沒掃描) | Synced to a strm, not yet picked up by a library scan |
| Not in the library (no strm) (不在媒體庫（沒有 strm）) | Inside a sync folder but without a strm, for example smaller than the size limit in the sync options, or not synced yet |

If a folder looks wrong, click **Organise this folder…** (整理這個資料夾…), or **Organise…** on a subfolder: it is pinned at the top of the **Organise 115** card on the 115 tab and marked **Added to organise** (已加進整理) here, so you can keep browsing and pick more. When done, **Go organise (N)** (去整理) at the top of the page takes you to that card, where MoviePilot renames, moves and scrapes them on 115, again with a preview before anything runs; see [Organising 115](#organising-115). Reorganisation progress is shown on this page too.

## Organising 115

Videos organised by MoviePilot are named after its rename format, for example `凡人修仙传 (2020) {tmdbid=106449}/Season 1/凡人修仙传 - S01E176 - 第 176 集.mp4`. Folders saved from other people's shares often look different, and their episode numbers may not be recognisable:

```
/TV/Donghua/F 凡人修仙传{tmdbid-106449} 更176｜停更｜预计第二季度更新/凡人修仙传 EP001_风起天南.mp4
/TV/Variety/康熙来了 (2004)/康熙来了 EP01.mp4      ← next to an existing "康熙来了 (2004) {tmdbid=6836}"
/TV/Drama/大道朝天 (2024) {tmdbid=…}/Season 1/10.xxx.mp4      ← the folder is fine, only file names are not
```

The **Organise 115** (整理 115 網盤) card on the 115 tab finds these, hands each whole folder to MoviePilot, or deletes what you don't want. **Recognition and naming are done by MoviePilot**: Mi302 does not judge names itself and does not guess TMDB IDs, types or seasons. It needs the MoviePilot username and password on the MoviePilot tab (these APIs only accept an account login), and previewing or organising also needs a 115 login.

### What is listed

| Reason | Meaning |
| --- | --- |
| In the library, N episodes have numbers guessed from the file name and N episodes have no recognised number (媒體庫裡 N 集的集號是從檔名猜的、N 集認不出集號) | The library scan guessed the number from names like "10.xxx" or "第10集", or could not recognise it at all. Listed without asking MoviePilot |
| Folder name differs from MoviePilot's (資料夾名稱和 MoviePilot 的不一樣) | For example it would rename "F 凡人修仙传{tmdbid-106449} 更176…" to "凡人修仙传 (2020) {tmdbid=106449}". If a folder with that name already exists next to it, the entry says it will be merged into it |
| Videos lie directly in the folder (影片直接放在資料夾裡) | MoviePilot's TV rename format has season folders, but the videos are not in one |
| File names differ from MoviePilot's (檔名和 MoviePilot 的不一樣) | The current and the new file names are shown, e.g. "1 → 凡人修仙传 - S02E01 - 第 1 集". A difference in word order alone does not count: MoviePilot reverses effect tags such as "DV HQ", so asking again about a name it produced itself flips the order |
| No folder of its own (沒有自己的資料夾) | A movie that shares its folder with other movies; listed on its own. When the folder is named after the movie but holds more than one video (often a duplicate that 115 suffixed with "(1)"), the entry says the folder holds N other videos (資料夾裡還有另外 N 支影片) |
| Inside another folder named after the title (套在另一個以片名命名的資料夾裡) | The whole show or movie folder sits inside a folder named after the title, such as "H-画江湖之天罡-2023-[tmdb=1221210]" (for example after a wrong organise); organising moves it one level above that folder |
| MoviePilot cannot recognise it (MoviePilot 認不出來) | It cannot tell which title this is; set the type and TMDB ID yourself, or rename it on 115 |

The last four need **Check with MoviePilot** (問 MoviePilot 檢查) first. For every show and movie in the library that lies inside a sync folder, Mi302 asks MoviePilot what it would be called after organising (MoviePilot's "query the organised name" API, the same logic its own UI uses): once for the folder, and once each for one or two sample videos. The check runs in the background; the card shows how far it is and how many it found, and results appear as they come in. A large library takes a while the first time (two or three questions per folder, each making MoviePilot query TMDB). Answers are stored in Mi302's database, and folders whose name and videos have not changed are not asked again. After changing MoviePilot's rename format, click **Check everything again** (全部重新檢查). The list is recalculated after each library scan or 115 sync.

You can search by name, or show only shows, only movies, or **Only wrong episode numbers** (只看集號不對的).

In **Browse 115** (瀏覽 115), **Organise this folder…** (整理這個資料夾…) or **Organise…** (整理…) on a subfolder pins that folder at the top of the list, marked **Added from Browse 115** (從瀏覽 115 加進來). It works for folders outside the library and outside the sync folders too (for example an inbox folder). **Done with it** (不看了) removes it.

### What to check for each entry

- **Organise into** (整理到): **MoviePilot's directory settings** (照 MoviePilot 的目錄設定), the default; **the same parent folder** (同一層), next to the current folder (when the item sits in a folder named after the movie, such as a movie file inside "H-画江湖之天罡-2023-[tmdb=1221210]", it goes one level above that folder, so the new movie folder is not created inside the old one); or **a given 115 folder** (指定的 115 資料夾). **Organise everything into** (全部整理到) above the list changes all entries at once; an entry you changed yourself no longer follows it. The choice is remembered in this browser.
- **MoviePilot's directory settings**: when MoviePilot picks a directory itself it only looks at download folders (the source must be under a download folder whose storage is also 115), so a folder already in the library never matches and its preview only says 整理任务处理失败 ("organise task failed"). Mi302 therefore reads MoviePilot's directory settings. A folder already inside one of its library folders on 115 stays in its current category folder, as with the same parent folder, and only the folder and file names change; the preview names the library entry. An entry limited to movies or to shows is only used for those. For a folder outside every library folder (for example an inbox inside a download folder), MoviePilot picks the directory itself; if it cannot, the preview says so, and you can choose the same parent folder or a given folder instead.
- **Options sent to MoviePilot**: the switches in its own organise dialog, **by type** (按类型分类), **by category** (按类别分类), **scrape metadata** (刮削元数据) and **reuse recognition from history** (复用历史识别信息), are always off. No type or category folders are added, nothing is scraped on 115 (the local nfo files and posters move with the strm files), and old organise records are not used for recognition. Files are moved.
- **Each part**: a folder with subfolders is sent one subfolder at a time, plus once for the videos lying directly in it (a subfolder may be a different title, like "虚天战纪.导演剪辑版 (2025) [tmdb-282348]" inside the 凡人修仙传 folder). Each part defaults to **Let MoviePilot recognise it** (讓 MoviePilot 自己認); **Set…** (指定…) lets you set the type, TMDB ID, season and episode format for that part only.
- **Episode format** (集數定位): for when MoviePilot cannot find the episode number (e.g. "10.xxx.mp4"). `{ep}` marks the episode number and `{a}`, `{b}` any text, e.g. `{ep}.{a}`. **Recommend** (推薦) asks MoviePilot first; only if it cannot recommend one does Mi302 offer its own guess from the file names, and it says which one you got.

### Preview

Each entry has **Preview** (預覽), **Organise** (整理) and **Delete…** (刪除…) on its right. **Preview** previews that entry only. **Organise** previews it first if needed and, if anything can be organised, asks before running it, so one click organises one entry. To handle several at once, tick them and use **Preview selected** (預覽選取的) and **Run** (執行) in the bar pinned to the bottom of the screen, without scrolling to the end of the list.

**Organise all** (全部整理): **Organise all (N)** in the bottom bar works through the whole list, not just this page; with a search or a kind selected, only the matching entries, plus any pinned from Browse 115. Mi302 previews them one by one in the background and organises those without problems straight away, into the place chosen under **Organise everything into**. It skips an entry when MoviePilot would not organise some file (not recognised, no episode number), when there is a "check this" note (for example two files going to the same place), when the recognised season differs from the library, or when the organise history or the target folder cannot be read. A show is skipped as a whole if one episode fails, never half organised. Files already named to the format, or whose target already has a file with that name, are still not sent. The progress bar in the bottom bar shows how far it is and how many were organised or skipped; **Stop** (停止) stops after the current entry, and closing the page does not stop it. Afterwards the top of the card lists the skipped and failed entries with the reason, and **Find in list** (在清單上找) takes you to it; details are kept for at most 2000 entries, while the counts include all of them. After 5 previews in a row fail (for example MoviePilot's directory settings do not match, or 115 cannot be read) it stops.

**Holding back, and very large folders**: **Not now** (先不整理) on the right of an entry marks it; **Organise all** skips it and it moves to the end of the list (**Organise again** (恢復整理) undoes it; organising it on its own still works). The kind **Held back only** (只看先不整理的) lists just those, and **Organise all** then does just those. In addition, **skip anything that would send more than N videos at once** (一次要送超過 N 支影片的先不整理; default 300, 0 = no limit, remembered in this browser): MoviePilot previews and organises a whole season (or folder) in one go, and a show with thousands of episodes can take several GB of memory and get the MoviePilot process killed. Do the rest first and deal with the big ones later.

**When MoviePilot goes away**: if MoviePilot cannot be reached during **Organise all** (killed by the system, restarting), Mi302 waits for it (up to 15 minutes), gives it another minute once it answers, then redoes the same entry, which does not count as skipped. If the connection drops or times out halfway through a preview or an organise, MoviePilot may still be working on that entry in the background, so it is not sent again: the entry is recorded as skipped or failed, and the next one is only sent once MoviePilot answers again, so work does not pile up. The result says how many times this happened; if it happens often, look at MoviePilot's log and the machine's memory. With a local (non-Docker) MoviePilot install, the frontend on port 3000 shuts itself down when the backend is too busy to answer its health check, and stays down; in that case point Mi302's **MoviePilot URL** at the backend on port 3001 (for example `http://127.0.0.1:3001`). While it runs, single entries cannot be organised or deleted and **Check with MoviePilot** (問 MoviePilot 檢查) is unavailable; while a check runs, **Organise all** cannot start. **Move old folders with no videos left to the 115 recycle bin** sits under **Organise everything into** and applies to both.

As with **File manager → Organise** in MoviePilot's own UI, the whole folder is sent: videos, subtitles and audio tracks inside are organised together, following MoviePilot's format. After the preview each part shows what MoviePilot recognised, such as "→ 凡人修仙传 第 2 季（174 個）" or "→ 虚天战纪（電影）（2 個）"; if it is wrong, set it for that part and preview again. A common case is 预计第二季度 in a folder name: MoviePilot takes it as season 2, and Mi302 points it out when that differs from the season in the library.

Mi302 also blocks what MoviePilot itself does not:

- **Files already named to the format** (new location equals the current one) are not sent, nor are **files that would move out of the sync folders** (they would disappear from the library). When a folder contains such files, only the other videos are sent, one by one, with subtitles following their videos. So when a folder is mostly fine and only a few file names are off, only those few are touched.
- When the overwrite mode of MoviePilot's library folder is **keep latest** (保留最新), **renames inside the same folder** are not sent. If the target does not exist yet, MoviePilot first deletes the other versions of that episode in the target folder, and the source file is one of them.
- Two files organised to the same location: a note says only one will stay.
- When MoviePilot's preview only says 整理任务处理失败，请稍后重试 ("organise task failed, try again later") for a file, the reason is only in its log, usually a directory setting that does not match; Mi302 adds this hint.
- For files that were outside the sync folders to begin with, a new location outside them only gives the note that Mi302 will not create strm files for them.

MoviePilot's preview does not check whether the target already has a file with the same name; the real run then fails with 媒体库存在同名文件 ("the library already has a file with this name"), and a file that failed before is retried in the background with the old plan. So the Mi302 preview looks in the target folder on 115. A file whose target already exists is not sent when the overwrite mode is **never** (always the case for the same parent folder or a given folder); the entry says the target already has a file with this name and this one is most likely a duplicate, and shows the size of both files. If they match, click **Delete this copy** (刪掉這支) next to that file: only this copy goes to the 115 recycle bin (restorable on 115), its local strm and library entry are removed, and the copy at the target stays. When the library folder overwrites (**overwrite**, **by size**, **keep latest**), the file is sent with a note that it will replace the existing one or keep one of them by that rule.

### Running it

Click **Organise** on an entry, or **Run** in the bottom bar. The previewed entries are organised in the background, with the settings from the preview. When MoviePilot has successful organise records for these files (the preview says how many), Mi302 reorganises them the way MoviePilot's own UI does: the old records are cleared, and for records made by copying or linking the old target files are deleted too. Otherwise MoviePilot would skip them as already organised, which the preview cannot show. Progress is shown at the top of the card (and on the **Browse 115** page). 115 only allows a few requests per second and each video needs several to move and rename, so a folder of a few hundred episodes takes ten minutes or more. Afterwards:

- With **Move old folders with no videos left to the 115 recycle bin** (整理完後，沒有影片留下的舊資料夾移到 115 回收站) on (the default), emptied old folders go to the recycle bin together with the nfo files and images left inside, restorable on 115; an outer folder named after the title (such as "H-画江湖之天罡-2023-[tmdb=1221210]") goes too once it is empty. Folders that still hold videos (skipped or failed ones) are kept; use **Duplicate files** (重複檔案) for them. Before moving a folder Mi302 checks it is still where it was; if MoviePilot already deleted it, it is left alone.
- When MoviePilot puts files into its own organise queue and works on them in the background (for example a file that failed before and is retried with the old plan), the result says **MoviePilot background** (MoviePilot 背景處理). These count neither as organised nor as failed; see MoviePilot's organise history for the outcome. The incremental sync about 20 seconds after organising still runs, but MoviePilot has usually not finished by then: the local strm files for these follow at the next incremental sync after it finishes (the automatic interval, or **Incremental sync** (增量同步) by hand). MoviePilot's notification to its media server (Mi302) after organising only rescans locally; it does not move strm files.
- Local nfo files with `-1` numbers are deleted and an incremental sync runs about 20 seconds later, moving the local strm files along. After the sync and a scan, the organised folders are gone from the list.

### Deleting

If you would rather not organise something, click **Delete…** (刪除…) on its row. Everything goes to the 115 recycle bin (restorable on 115), and the local strm and nfo files and library entries are removed too. Deleting needs the QR-code (cookie) login to 115, and waits while a 115 sync or a MoviePilot reorganisation is running.

- **Shows**: every episode of the show in the library is listed, grouped by season. By default only episodes with unrecognised numbers are ticked; episodes whose number was guessed from the file name are usually real episodes with non-standard names, better organised than deleted. The buttons tick **Unrecognised numbers** (認不出集號的), **Wrong numbers, including guessed** (集號不對的（含猜的）), **Every episode of the show** (整部劇每一集), or **Select none** (全不選). **Also move the show folder to the 115 recycle bin if no videos are left** (刪完沒有影片留下的話，劇集資料夾也移到 115 回收站), ticked by default, moves the folder with its leftover nfo files and images only after Mi302 has checked on 115 that no videos remain. **Delete the whole show…** (整部劇刪掉…) moves the show folder with everything in it, including files that never made it into the library.
- **Movies and folders added from Browse 115**: the whole folder (or the single file, for a movie without its own folder) goes to the recycle bin.

## 115 recycle bin

Files deleted on 115 first go to 115's recycle bin, where they can still be restored in 115; Mi302 also sends deleted duplicates there. The **115 recycle bin** (115 回收站) card on the 115 tab:

- The bin is read when you open **Recycle bin** (回收站) at the top of the page. It lists each item's name, size, deletion time and original folder, 50 per page. **Refresh** (重新整理) reads it again.
- **Empty recycle bin…** (清空回收站…) permanently deletes everything in the bin. After that, the files cannot be recovered in 115 either.

Emptying asks for confirmation twice:

1. Type 清空 ("empty") in the confirmation box. Anything else cancels.
2. With QR-code (cookie) login only, enter your 115 security key (安全密鑰, 6 digits). If you turned off the security-key requirement for emptying the bin in 115 (帳號安全 → 安全密鑰), leave it empty.

With [115 open platform](#115-open-platform-advanced) authorisation, Mi302 empties the bin through the open platform, which needs no security key; it falls back to the cookie only if the open platform fails. Emptying the bin is recorded in the log.

## Duplicate files

The **Duplicates and big files** card (重複和大檔案) on the **Tools** tab (整理) finds duplicate videos on 115 and deletes the extra copies, and also lists the big files that take up space. One **Scan** (掃描) looks for three kinds, shown on three tabs:

| Tab | What counts as a duplicate | Default |
| --- | --- | --- |
| Identical (完全相同) | Same SHA1 (a fingerprint of the content) and size, even with different names | One copy suggested to keep, the rest ticked |
| Versions (不同版本) | The same movie or episode in your library, but different files, for example 1080p and 2160p | Nothing ticked; you choose |
| Big files (大檔案) | Not duplicates: videos of 1 GB or more, filtered by size and by movie or episode | Nothing ticked; you choose |

### Scanning

1. **Scope**: the sync tasks' 115 folders by default. **Pick a 115 folder…** (改選 115 目錄…) lets you choose any folder, for example the whole drive `/`.
2. **Scan** (掃描): Mi302 lists every video in the scope. 115 includes the SHA1 in its file listings, so forty thousand videos take about forty requests. For files outside the sync folders, the folder path is looked up once per folder. Rate limiting trips the circuit breaker as usual.
3. **Results**: the top shows how many groups each kind of duplicate has, how much deleting per the suggestions would free, and how many videos are 1 GB or more. Each group is a card, largest savings first, 20 groups per page, searchable.

### Identical files

- One copy per group is suggested to keep: the one with a local strm first, then one with complete numbering in its file name, then the earliest upload. The others are ticked, and you can change any tick.
- Complete numbering in the file name means an episode shows both season and episode (`S01E02`, `1x02`) and a movie shows its year. Names with only an episode number (`EP05`, `第5集`, `10.xxx`) are incomplete and marked **Incomplete numbering in file name** (檔名編號不完整).
- Each copy shows its 115 path, its upload time, and the tags suggested to keep (建議保留), has strm (有 strm), outside sync folders (不在同步目錄) and has watch history (有觀看紀錄).

### Versions

- How the same title is recognised: movies by tmdbid (from a scraped nfo), otherwise by title and year; episodes by series, season and episode number. Only files in the sync folders that have a strm are considered, so the library knows what they are.
- Episodes with an unknown number are skipped: no episode number in the library, or `-1` in the nfo with nothing usable in the file name. An episode is also skipped when its 115 file name shows an episode number that differs from the library's, so nothing is deleted by mistake. Older versions treated a whole season whose episode numbers the scraper could not read as one episode; after updating, those old results are cleared and the page asks you to click **Scan** (掃描) again.
- Not treated as duplicates: split files (CD1, Part 2) and files holding several episodes (E01E02, E01-02). Different cuts such as director's cut or extended are grouped separately. Groups where every copy is identical are left to the Identical tab.
- Each version shows resolution, HDR or Dolby Vision, codec, the first audio track, the number of audio and subtitle tracks, and size. Quality comes from extracted media info (see [Media Info](Media-Info)); anything missing is guessed from the file name and marked from file name (看檔名).
- The files differ, so nothing is ticked by default. Which copy is suggested for keeping depends on **Suggest keeping** (建議保留) in the action bar:

  | Option | Suggested copy |
  | --- | --- |
  | 1080P, else 4K (1080P（沒有再 4K）), the default | 1080P; if there is none, 4K; if neither exists, the highest remaining |
  | 4K, else 1080P (4K（沒有再 1080P）) | 4K; if there is none, 1080P, then the highest remaining |
  | Highest resolution (解析度最高的) | The highest resolution |

  With equal resolution a file name with complete numbering wins, then the smaller file, then the earliest upload; copies whose resolution cannot be told come last. Changing the option recalculates the existing results at once, without finding duplicates again. **Tick as suggested** (照建議勾選) on a group ticks that group, and **Tick all as suggested** (全部照建議勾選) at the top ticks every group, including pages you have not opened. For example, choose 1080P, else 4K and click **Tick all as suggested** to keep only the 1080P copy in every group.

### Big files

- The same scan records every video of 1 GB or more (115 includes the size in its listings, so this costs no extra requests), largest first, 20 per page.
- At the top, choose **Larger than** (大於) 1, 2, 5, 10, 20, 40, 60 or 100 GB (remembered in this browser) and the kind: movies and episodes, movies only, episodes only, or not in the library; you can also search file names and paths. The top of the list shows how many match and their total size. The default is larger than 20 GB; the **1 GB or more (all)** count in the summary covers everything the scan recorded, while the list shows only what matches. The delete confirmation repeats the conditions.
- Each file shows its size, resolution, HDR, codec and audio (from media info when available, otherwise from the file name), whether it is a movie or an episode, **N other versions** (還有 N 個其他版本; the same movie or episode has other files, see Versions above) and **Has watch history** (有觀看紀錄).
- These are not duplicates, so nothing is ticked and nothing has to be kept. Tick files one by one, or click **Tick everything that matches** (勾選符合條件的全部), which includes pages you have not opened; changing the size, kind or search cancels it, so nothing you have not seen gets ticked.
- Deleted files leave the library. For files marked as having other versions, watch history moves to another version; otherwise it is gone.

### Deleting

- A bar above the list has **Tick all as suggested** (全部照建議勾選; on Big files, **Tick everything that matches**) and **Untick all** (全部取消勾選), which work on every tab and include pages you have not opened, plus a **Delete ticked** button that shows how many files are ticked and their total size (刪除勾選的 N 個). The same button is repeated below the list.
- **Delete ticked** (刪除勾選的) handles every group on the current tab, including pages you have not opened; **Delete ticked in this group** (刪這一組勾選的) handles one group. The confirmation shows the count and size, and more than 50 files asks a second time. Every duplicate group must keep at least one copy (big files need not).
- Files go to the 115 recycle bin and can be restored there, until the bin is emptied (see [115 recycle bin](#115-recycle-bin)). One request handles up to a hundred files.
- The local strm and its same-name nfo, posters, subtitles and `X-mediainfo.json` are deleted too (regardless of the follow-deletions setting), and only the affected series or movies are rescanned.
- Watch history of the deleted copy (played, resume position, favourite) moves to the kept copy, per user. For big files it moves to another version of the same title or an identical copy; if there is none, it is gone.
- **Recently deleted** (最近刪掉的) at the bottom of the card lists what was deleted (duplicates and big files) and where, to find it in the recycle bin.
- **Stop** (停止) is available while scanning or deleting: a stopped scan keeps the previous results; deleting stops after the current batch (up to 100 files), and files already in the recycle bin stay there.
- Deleting needs QR-code (cookie) login; it is not available with only the open platform.
- Results reflect the moment you clicked **Scan**. If files were moved, renamed or deleted on 115 since, scan again before deleting.

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
