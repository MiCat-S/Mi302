[繁體中文](MoviePilot-整合) | [简体中文](MoviePilot-集成) | **English**

Mi302 does not scrape (fetch metadata and artwork) by itself; it hands this to [MoviePilot](https://github.com/jxxghp/MoviePilot). This page covers connecting to MoviePilot, which videos are sent for scraping, how filling missing episodes decides what is missing, how to reorganise series with wrong episode numbers, and how to add Mi302 to MoviePilot as an Emby media server. The web admin page is in Traditional Chinese; original labels are given in parentheses.

Mi302 was built against MoviePilot V3. Older versions mostly work, with a few features reduced; see [Older MoviePilot versions](#older-moviepilot-versions).

## How it works

Mi302 only reads nfo files and images that are already in the folders. They can be downloaded along with the 115 sync (see [115 Cloud Sync](115-Cloud-Sync)), or produced by MoviePilot:

1. After a sync creates new strm files, Mi302 first scans the changed places, so new titles show up in players at once (still without posters).
2. Mi302 sends the paths of the new strm files to MoviePilot's scrape API (`POST /api/v1/media/scrape/local`).
3. MoviePilot identifies each video, looks it up on TMDB and other sources, and writes nfo files, posters and backdrops into the same folder.
4. When scraping is done, Mi302 rescans only the scraped places, and players show the posters and plots.

So MoviePilot and Mi302 must see the same files: for example, both run on the same machine, or both mount the same network drive.

## Connection settings

The settings are in the **Connection** card (連線) on the **MoviePilot** tab.

1. In MoviePilot, copy the **API token** from Settings → System.
2. In Mi302, enter the **MoviePilot URL** (MoviePilot 網址), for example `http://192.168.1.10:3000` (it must start with `http://` or `https://`), and the **API token** (API 令牌).
3. If the two programs see different paths, fill in **Path mappings** (路徑對應); see the next section.
4. Click **Test connection** (測試連線). It saves first, then tests.

The connection test only checks the URL and the API token. It sends an empty path to the scrape API, which MoviePilot rejects with "刮削路径无效" (invalid scrape path), so nothing is actually scraped. Whether the path mappings are right only shows with the first real scrape. The top right of the card shows "configured" (已設定) or "not configured" (未設定), and after a test "connected" (連線成功) or "connection failed" (連線失敗).

| Label in the web UI | Key | Default | Notes |
|---|---|---|---|
| MoviePilot URL (MoviePilot 網址) | `moviepilot.url` | empty | a trailing `/` is removed |
| API token (API 令牌) | `moviepilot.api_token` | empty | sent in the `X-API-KEY` header and the `token` query parameter |
| Concurrent scrapes (同時刮削幾項) | `moviepilot.concurrency` | 3 | 1–8 |
| Path mappings (路徑對應) | `moviepilot.path_mappings` | none | see the next section |
| Send new strm files for scraping after sync (同步產生新的 strm 後自動送去刮削) | `moviepilot.scrape_after_sync` | on | when off, sync only scans |
| MoviePilot username, password (MoviePilot 帳號, MoviePilot 密碼) | `moviepilot.username`, `moviepilot.password` | empty | needed to fill missing episodes and to reorganise episode numbers, and by the scrape API of older versions |
| Fill missing episodes after full sync (全量同步後自動補全) | `moviepilot.fill_after_full_sync` | off | in the collapsed Settings section (設定) of the fill card; saved as soon as you toggle it |
| (config file only) | `moviepilot.timeout` | 300 | seconds to wait per item; exceeding it counts as a connection failure and stops the batch |

### When you need the username and password

The account fields are in a collapsed section of the **Connection** card, "MoviePilot account (needed to fill missing episodes and reorganise episode numbers; also by the scrape API of older versions)" (MoviePilot 帳號密碼（補全缺集、整理集號需要；舊版刮削 API 也需要）). Click it to expand.

- **Filling missing episodes**: MoviePilot's subscription API only accepts a logged-in account, not the API token. You must fill these in.
- **Reorganising series with wrong episode numbers**: the manual transfer, episode-format recommendation and version APIs only accept a logged-in account, and it must be an admin.
- **Older MoviePilot versions**: the scrape API only accepts a logged-in account. Fill these in if the connection test reports "access denied" (拒絕存取).

With an account filled in, when MoviePilot answers 401 or 403, Mi302 logs in with it (`POST /api/v1/login/access-token`) and retries, then keeps using the token from that login. When the token expires, it logs in again. You can also leave the API token empty and use only the URL and the account.

## Path mappings

Mi302 sends MoviePilot the file paths as Mi302 sees them. If both see the same paths (for example on the same machine), leave this empty. Otherwise, add one mapping per line under **Path mappings**:

```
Mi302 path => MoviePilot path
```

Examples:

- Mi302 runs in a Parallels VM and sees `/media/psf/Vo`; MoviePilot runs on the Mac and sees `/Volumes/Vo`: enter `/media/psf/Vo => /Volumes/Vo`.
- MoviePilot runs in Docker: use the mount path inside its container. If the host's `/volume1/media` is mounted as `/mnt/media`, enter `/volume1/media => /mnt/media`.

Rules:

- A mapping matches whole folders at the start of the path. If several match, the longest wins.
- The same mappings are used in reverse: when MoviePilot tells Mi302 to rescan, it sends MoviePilot's paths, and Mi302 maps them back to its own.
- When MoviePilot reports that a file does not exist, the error in the **Scrape** card reads "MoviePilot 找不到 …，請檢查路徑對應" (MoviePilot cannot find …, check the path mappings), followed by the path MoviePilot received.

In the config file:

```yaml
moviepilot:
  path_mappings:
    - from: /media/psf/Vo
      to: /Volumes/Vo
```

## What gets sent

After a sync, only the strm files that sync created are sent. The **Scrape missing items** button (刮削缺少資料的項目) in the **Scrape** card (刮削) looks at the whole library instead. Both follow these rules:

- **Movies**: the strm file itself is sent. It is skipped if it already has an nfo with the same name (`X.nfo`), or if the movie sits in its own folder that contains a `movie.nfo`.
- **Series**: if the series has no `tvshow.nfo` yet, the whole series folder is sent, so series, seasons and episodes are handled in one go. For a series that already has a `tvshow.nfo`, only the episodes without an nfo are sent.
- Anything that already has an nfo is not sent, so metadata downloaded from 115 or scraped earlier is not overwritten.
- With **Scrape missing items**, episodes that have an nfo but no still image are sent again (except those confirmed to have no still within the last 30 days; see below).
- On a connection failure, an authentication failure, an HTTP error from MoviePilot or a timeout, the whole batch stops. Items not yet sent count as failed, and the reason is shown in the **Scrape** card.

Series folders are detected the same way as during scanning, so category folders below the library path are fine (see [Library and Scanning](Library-and-Scanning)). For example, with the library path `电视剧` and `国产剧/庆余年 (2019)/Season 1/…` below it, Mi302 sends the series `庆余年 (2019)` and takes the tmdbid from its `tvshow.nfo`.

## Scraping speed

MoviePilot's scrape API is synchronous: each item only returns after MoviePilot has looked it up on TMDB and downloaded the images, which can take tens of seconds per title. Mi302 speeds this up by:

- **Sending several items at once**: **Concurrent scrapes** defaults to 3, at most 8. Too many can hit TMDB's rate limit; lower it if MoviePilot's log shows 429.
- **Passing the tmdbid for series already scraped**: when sending a single episode, Mi302 takes the tmdbid from the series' `tvshow.nfo` and tells MoviePilot which show it is (`media_source=themoviedb&media_id=…`). MoviePilot does not have to search TMDB by file name and cannot pick the wrong show. This needs MoviePilot V3; older versions ignore these parameters and identify by file name as usual.

If MoviePilot itself is slow, the cause is usually a slow connection to TMDB. Set proxies for the TMDB API and image URLs in MoviePilot.

## Scrape results

The **Scrape** card shows the numbers for the last run (after sync or manual): sent (送出), succeeded (成功), failed (失敗) and no still (沒有劇照).

After MoviePilot reports success, Mi302 checks that an nfo was actually written:

- For a single file it looks for `X.nfo`; for a series folder, `tvshow.nfo`. If it is missing, the item counts as failed.
- When MoviePilot cannot work out the episode number, it writes nothing but still reports success. Such episodes are counted as failed with an explanation: the file name needs an episode number such as `S01E01`.
- An episode with an nfo but no still counts as succeeded, and is also counted under "no still".

### Episodes without a still

MoviePilot takes episode images only from TMDB's still for that episode, saved as `X.jpg` next to the video (Mi302 also accepts `X-thumb.jpg`). A missing image usually means:

1. **TMDB has no still for the episode**: common for Chinese series, variety shows and newly aired episodes; MoviePilot cannot write one either. For such episodes Mi302 shows the series' landscape image (`thumb`, `landscape`), or the backdrop if there is none, so players do not show a blank tile.
2. **MoviePilot failed to download the image**: MoviePilot's log shows "图片下载失败" (image download failed), usually because it cannot reach `image.tmdb.org`. Set a TMDB image proxy in MoviePilot. After fixing it, click **Scrape missing items**; episodes with an nfo but no still are sent again.

An episode that was sent and confirmed to have no still is not sent again by **Scrape missing items** for 30 days.

## Fill missing episodes

When series in your library are missing episodes, MoviePilot can download them. This is the **Fill missing episodes** card (補全缺集) on the **MoviePilot** tab.

### Before you start

1. Add Mi302 to MoviePilot as a media server (see "Add Mi302 to MoviePilot as Emby" below), so MoviePilot knows which episodes you already have.
2. Enter MoviePilot's username and password in the **Connection** card and save.
3. The series must have been scraped, with a tmdbid in its `tvshow.nfo`. Series without a tmdbid are not sent.

### The series list

The card lists every series in the library and how many episodes each season has:

- Seasons with gaps in the episode numbers (for example episodes 2 and 4 but not 3) get their own line with how many and which episodes are missing. Series with gaps are listed first.
- Type into **Search series** (搜尋劇名) to search by title, original title, year, pinyin or pinyin initials. Tick **Only series with gaps** (只看集號有空洞的) to list only those.
- The **All years** drop-down (全部年份) shows only series from one year; it lists only years that exist in your library.
- Choose 20, 50, 100 or 200 series per page (每頁 20 部 and so on); the browser remembers your choice. Below the list you see the page number and the total, with **Previous** (上一頁) and **Next** (下一頁) buttons. Searching or changing the year or filter goes back to page 1.
- The **Fill** button (補全) next to a series sends only that series. Series without a tmdbid are marked "no tmdbid, scrape first" (沒有 tmdbid，要先刮削) and the button is disabled.
- **Fill all** (全部補全) at the top right of the card sends every series with a tmdbid, after asking for confirmation.
- Specials (season 0) are neither listed nor sent.

Gaps in episode numbers are only a hint. Missing final episodes are caught by the check below as well.

### How each season is checked

Filling works season by season, and only for seasons that already have episodes in the library. A season that exists on TMDB but has no episodes at all in the library is not subscribed automatically.

1. Mi302 asks MoviePilot for the season's episodes and air dates on TMDB (`GET /api/v1/tmdb/{tmdbid}/{season}`).
2. It decides which episodes have aired:
   - An episode with an air date has aired if that date is not later than today (the date on the Mi302 host).
   - Episodes without a date are often placeholders for episodes not yet aired. One counts as aired if its number is not higher than the last episode of that season in the library (if you have episode 10, episode 3 has aired).
   - Undated episodes after the last one in the library are uncertain and are not counted as missing. The result notes how many there are.
3. It compares with the episode numbers Mi302 already has for that season. If every aired episode is there, no subscription is created, and the season counts as "already complete".
4. Only if episodes are missing does it create a subscription (`POST /api/v1/subscribe/`), identifying the show by tmdbid (V3's `media_source` / `media_id`) rather than by name, so it cannot pick the wrong show.
5. It then asks MoviePilot to search for that subscription right away (`POST /api/v1/subscribe/search/{subscription id}`). MoviePilot V3 only schedules a search when a subscription is created, and sometimes the search waits for the next scheduled run. Existing subscriptions are searched again too. If this step fails, the result says so, and MoviePilot handles it at its next scheduled search.

If the TMDB episode list cannot be fetched, Mi302 creates the subscription anyway and leaves the decision to MoviePilot.

When MoviePilot has downloaded and organised the files, it notifies Mi302 to rescan. If a season with missing episodes is still airing, the subscription keeps following new episodes.

### Results

The numbers on the card:

| Label | Meaning |
|---|---|
| Subscribed and searched (建訂閱並搜尋) | seasons with missing episodes for which a subscription was created and a search requested |
| Missing episodes (缺的集數) | total number of missing episodes across all seasons |
| Already complete (已經齊全) | seasons where every aired episode is present, so no subscription was created; also seasons an older MoviePilot rejected with "媒体库中已存在" (already in library) |
| Subscribed before (之前訂閱過) | seasons MoviePilot reported as already subscribed (a new search was requested) |
| No tmdbid (沒 tmdbid) | series skipped for lack of a tmdbid |
| Failed (失敗) | seasons MoviePilot refused to subscribe, or that were not processed after the run stopped |

Expand "Results per season (N)" (每一季的結果（N）) to see which episodes each season misses and what MoviePilot replied. A connection or authentication failure stops the run, and the remaining seasons count as failed. Only one fill runs at a time; clicking again during a run shows "already filling, wait for it to finish" (已經在補全中，等它做完).

### Fill after full sync

With **Fill missing episodes after full sync** (全量同步後自動補全) ticked (off by default; saved as soon as you toggle it), every full sync ends by sending all series with a tmdbid, after scraping has finished. Nothing is sent if no MoviePilot account is filled in. It waits for scraping because newly scraped series only have a tmdbid at that point.

## Reorganise series with wrong episode numbers

When the scraper cannot tell which episode a file is, the nfo has no episode number, or `-1`. If the file name is not a standard `S01E02` either, Mi302 can only guess from names such as "10.xxx" or "第10集", and some it cannot read at all. The card **Series with wrong episode numbers: reorganise with MoviePilot** (集號不對的劇：交給 MoviePilot 整理) lists these seasons and hands them to MoviePilot's manual transfer. MoviePilot renames the files on 115 to the standard names from its own naming settings (for example "Title - S01E10 - Episode title") and scrapes them, and Mi302 syncs the result. From then on any tool can read the episode number from the file name.

This really renames and moves files on 115, so you always preview first and check every file's new path before running it.

### Before you start

- MoviePilot must be v2.11.1-1 or later. Older versions do not support preview, so a "preview" would really reorganise the files. Mi302 therefore checks the version first (`GET /api/v1/system/env`) and sends nothing if the version is too old or cannot be read.
- Fill in the MoviePilot username and password on the **Connection** card, with an admin account. The manual transfer, episode-format recommendation and version APIs only accept a logged-in account.
- MoviePilot's 115 storage must be logged in to the same 115 account as Mi302. Mi302 sends 115 file ids, and MoviePilot moves the files by id.
- The episodes must be inside a 115 sync task and known to its sync records. If they are not found, run a full sync first.

### The list

Each season gets a row that says how many episodes have a guessed number and how many cannot be read. Only strm files are listed. Episodes whose number comes from the nfo, or whose file name is `S01E02` or `1x02`, are not listed. After reorganising, the new standard names drop off the list at the next scan.

The list needs a scan to know where each episode number came from. After upgrading, it stays empty until the first scan after startup has finished.

### Preview

Click **Preview reorganisation…** (預覽整理…) to open the dialog. Mi302 lists the files on 115 and splits them into batches by naming style:

| Batch | Episode format |
| --- | --- |
| Same naming as "10.潘玮柏…" (和「…」同一種寫法) | Generated by Mi302 from where the episode number sits in the name, for example `{ep}.{a}`, `{ep}-{a}` or `{b}第{ep}集{a}` |
| A style MoviePilot recognises itself (MoviePilot 自己認得的寫法) | None needed (`EP02`, `第十二集` and so on) |
| Episode number not recognised (認不出集號) | Recommended by MoviePilot; if it has no suggestion, the batch is off by default and you fill in the format yourself |

The episode format is MoviePilot's syntax: `{ep}` is the episode number, `{a}` and `{b}` stand for any text, and the pattern must match the whole file name including the extension. Each batch can be switched off or given a different format.

Other settings:

- **TMDB ID** (TMDB 編號): read from `tvshow.nfo`. Fill it in if it is missing or wrong.
- **Destination** (整理到): **Follow MoviePilot's directory settings** (照 MoviePilot 的目錄設定) lets MoviePilot use its library directory with type and category folders. **A 115 folder you specify** (指定的 115 資料夾) puts the files under the folder you enter, without adding type or category folders. It defaults to the folder above the series folder (for example `/cms/电视剧/综艺`), and **Choose…** (選擇…) lets you pick another. Either way, MoviePilot's naming settings decide the series folder and file names.
- **Scrape after reorganising** (整理後刮削): on by default. MoviePilot writes the nfo and stills to 115, and Mi302 downloads them during sync, as long as the 115 sync option **Also download nfo, posters and subtitles from 115** (一併下載 115 上的 nfo、海報、字幕) is on.

Click **Preview** (預覽). MoviePilot only works out the result without changing anything, and lists each file's new path and episode number. Mi302 adds its own checks and marks each file:

| Mark | Meaning |
| --- | --- |
| OK (可以) | No problem found |
| Check this (要看一下) | MoviePilot's episode number differs from what the file name suggests, or the file moves to a different series folder |
| Not sent (不會送) | MoviePilot's preview failed, the format does not match the file name, or the new location is outside Mi302's 115 sync folders (the episode would disappear from the library) |

Changing any setting discards the preview; preview again.

### Running it

Click **Run reorganisation (N)** (執行整理（N 個）) and confirm:

- Only files marked OK or Check this are sent, with exactly the settings of the preview. A preview is valid for 30 minutes and can be run once.
- MoviePilot moves and renames the files on 115, and scrapes them if enabled. The card shows progress and each file's result.
- Afterwards Mi302 deletes the old local nfo files that have no episode number, so they do not follow the strm to its new name. About 20 seconds later it runs an incremental 115 sync, the strm files move to their new names, and the affected series is rescanned.
- If a 115 sync is already running, the changes are picked up by the next sync.

### Reorganising a folder on 115

Besides the seasons listed on this card, you can pick any folder in **Browse 115** on the 115 tab (see [115 Cloud Sync](115-Cloud-Sync#browsing-115)) and hand it to MoviePilot, for example a folder you just saved and have not organised yet, or a series that looks wrong. The flow is the same: preview first, then run. The differences:

- All videos in the folder, including subfolders, are sent, up to 500. For larger folders, pick a smaller one.
- There are two more fields, **Type** (類型: let MoviePilot decide, TV series or movie) and **Season** (季). The TMDB ID and season can be left empty so MoviePilot identifies the files by name.
- Mi302 fills in what it can guess. Type and season come from what the library made of these videos, or from a folder name such as `Season 1`. The TMDB ID comes from the library, or from `[tmdb=…]` in a folder name.
- With the type set to movie, no season or episode format is sent.
- The batch whose file names show no episode number is sent too (movies usually look like that), and MoviePilot identifies those files itself.
- Files that are currently inside a sync folder may not move outside the sync folders, or they would disappear from the library. For files that were outside the sync folders to begin with, a new location outside them only gives the note that Mi302 will not create strm files for them.
- Afterwards only local nfo files with `-1` numbers are deleted; movie nfo files and others stay.

## Add Mi302 to MoviePilot as Emby

MoviePilot can add Mi302 as a media server. It uses it to check what you already have, and to notify Mi302 to rescan after organising files.

1. On Mi302's **MoviePilot** tab, in the card **Let MoviePilot treat Mi302 as Emby** (讓 MoviePilot 把 Mi302 當成 Emby), type a purpose (for example `MoviePilot`; left empty, it is named MoviePilot) and click **Create API key** (建立 API 金鑰). Copy the key with the copy icon in the list.
2. In MoviePilot, add an Emby server under Settings → Media servers. For the address, use the **Address** (地址) shown on the card, which is the URL you used to open the admin page, for example `http://192.168.1.20:8096`. Paste the API key you just created. MoviePilot must be able to reach that address.

An API key calls the Emby API as an administrator, but cannot be used for the admin page. You can delete a key from the list when it is no longer needed; programs using it will then fail to connect.

When MoviePilot notifies Mi302 that files have changed (`POST /Library/Media/Updated`), Mi302 maps the paths back with **Path mappings** and rescans only the movies or series containing those files, not the whole library. Only a notification without paths triggers a full library scan.

## Library covers

The cover of each library on a player's home screen can be generated by a MoviePilot library cover plugin, for example "Emby媒体库封面生成" (Emby library cover generator) from [wio-ki/MoviePilot-Plugins](https://github.com/wio-ki/MoviePilot-Plugins):

1. Add Mi302 to MoviePilot as an Emby media server, as in the previous section.
2. Install the plugin, choose Mi302 as the media server in the plugin settings, and choose the libraries to make covers for.
3. Run the plugin once by hand, or set a schedule (for example once a day).

The plugin builds a cover from random posters in the library and uploads it to Mi302 (`POST /Items/{id}/Images/Primary`). Uploaded covers:

- are stored in `images/` inside the data directory (`server.data_dir`). Rescans do not overwrite them, and they take priority over `poster` / `folder` / `cover` images in the library folder.
- are visible on the **Library** tab. There you can also upload your own with **Upload cover** (上傳封面), or delete the uploaded cover with **Reset to default** (改回預設). See [Library and Scanning](Library-and-Scanning).

The plugin's library monitoring (入库监控) is triggered only when MoviePilot finishes organising files or when Emby sends a new-item notification. Files that Mi302 syncs from 115 go through neither, so covers do not update automatically when new titles arrive. Use a schedule or update manually.

## Older MoviePilot versions

Mi302 was built against MoviePilot V3. On older versions (for example V2):

- The scrape API may not accept the API token. Fill in the username and password.
- The tmdbid parameters sent with each scrape are ignored, so MoviePilot identifies by file name as usual. This is slower and can pick the wrong show.
- Subscriptions: older versions read the `tmdbid` field, V3 reads `media_source` / `media_id`; Mi302 sends both. Older versions reject titles already in the library ("媒体库中已存在"), which Mi302 counts as "already complete".
- Subscription search: V3 uses POST, older versions GET. On HTTP 405 Mi302 retries with GET.
- TMDB episode lists: V3 and older versions use different response formats; both are understood.
- Reorganising episode numbers: preview in manual transfer exists from v2.11.1-1; older versions get nothing sent (see [Before you start](#before-you-start-1)). The episode-format recommendation exists only in V3, so on older versions you fill in the format yourself.
- If MoviePilot lacks an API (HTTP 404), Mi302 shows "MoviePilot 沒有這個 API（…），請確認網址或升級 MoviePilot" (MoviePilot has no such API; check the URL or upgrade MoviePilot).

## Other uses of MoviePilot

When **Show cast and crew in Chinese** (演職人員顯示中文名) is on, Mi302 asks MoviePilot for TMDB person aliases (`GET /api/v1/tmdb/person/{id}`) to find Chinese names. See [Library and Scanning](Library-and-Scanning).
