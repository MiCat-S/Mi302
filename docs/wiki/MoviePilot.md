[繁體中文](MoviePilot-整合) | [简体中文](MoviePilot-集成) | **English**

Mi302 does not scrape (fetch metadata and artwork) by itself; it hands this to [MoviePilot](https://github.com/jxxghp/MoviePilot). This page covers connecting to MoviePilot, which videos are sent for scraping, how filling missing episodes decides what is missing, what Organise 115 needs from MoviePilot, how to clear torrents stuck at no speed in qBittorrent, and how to add Mi302 to MoviePilot as an Emby media server. The web admin page is in Traditional Chinese; original labels are given in parentheses.

Mi302 was built against MoviePilot V3. Older versions mostly work, with a few features reduced; see [Older MoviePilot versions](#older-moviepilot-versions).

The **MoviePilot** tab of the web admin page has five buttons at the top: **Connection** (連線), **Scrape** (刮削), **Fill missing episodes** (補全缺集), **qBittorrent** and **As Emby** (當成 Emby). Each one shows only its card; **As Emby** is the **Let MoviePilot treat Mi302 as Emby** card (讓 MoviePilot 把 Mi302 當成 Emby). The page reopens on the one you looked at last.

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
2. In Mi302, enter the **MoviePilot URL** (MoviePilot 網址), for example `http://192.168.1.10:3000` (it must start with `http://` or `https://`), and the **API token** (API 令牌). With a local (non-Docker) MoviePilot install, the frontend on port 3000 shuts itself down when the backend is very busy, so use the backend port 3001 instead.
3. If the two programs see different paths, fill in **Path mappings** (路徑對應); see the next section.
4. Click **Test connection** (測試連線). It saves first, then tests.

The connection test only checks the URL and the API token. It sends an empty path to the scrape API, which MoviePilot rejects with "刮削路径无效" (invalid scrape path), so nothing is actually scraped. Whether the path mappings are right only shows with the first real scrape. The top right of the card shows "configured" (已設定) or "not configured" (未設定), and after a test "connected" (連線成功) or "connection failed" (連線失敗).

| Label in the web UI | Key | Default | Notes |
|---|---|---|---|
| MoviePilot URL (MoviePilot 網址) | `moviepilot.url` | empty | a trailing `/` is removed |
| API token (API 令牌) | `moviepilot.api_token` | empty | sent in the `X-API-KEY` header, not in the URL, so it stays out of proxy and MoviePilot access logs; only if it is rejected is it retried once as the `token` query parameter, for older versions that only read that |
| Concurrent scrapes (同時刮削幾項) | `moviepilot.concurrency` | 3 | 1–8 |
| Path mappings (路徑對應) | `moviepilot.path_mappings` | none | see the next section |
| Send new strm files for scraping after sync (同步產生新的 strm 後自動送去刮削) | `moviepilot.scrape_after_sync` | on | on the **Scrape** card, saved as soon as it is switched; when off, sync only scans |
| MoviePilot username, password (MoviePilot 帳號, MoviePilot 密碼) | `moviepilot.username`, `moviepilot.password` | empty | needed to fill missing episodes and for Organise 115, and by the scrape API of older versions |
| Fill missing episodes after full sync (全量同步後自動補全) | `moviepilot.fill_after_full_sync` | off | in the collapsed Settings section (設定) of the fill card; saved as soon as you toggle it |
| Seconds between new subscriptions (兩個新訂閱之間隔幾秒) | `moviepilot.fill_interval` | `60` | in the collapsed Settings section (設定) of the fill card; click **Save settings** (儲存設定); 0 = no gap |
| Skip seasons missing more than (缺超過幾集的季不補) | `moviepilot.fill_max_missing` | `0` | in the collapsed Settings section (設定) of the fill card; click **Save settings** (儲存設定); 0 = no limit |
| (config file only) | `moviepilot.timeout` | 300 | seconds to wait per item; exceeding it counts as a connection failure and stops the batch |

### When you need the username and password

The account fields are in a collapsed section of the **Connection** card, "MoviePilot account (not needed on V3; only for older versions)" (MoviePilot 帳號密碼（V3 不用填；舊版才需要）). Click it to expand.

- **MoviePilot V3**: every API used for scraping, filling missing episodes and Organise 115 accepts the API token (which acts as MoviePilot's administrator), so you do not need the account.
- **Older MoviePilot versions (V2)**: some APIs only accept a logged-in account. Fill these in if the connection test, a fill or an organise reports "access denied" (拒絕存取); Organise 115 needs an administrator account.

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

**Stop** (停止) is available while scraping: items already sent to MoviePilot finish, the rest are not sent and wait for the next scrape.

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
2. Enter MoviePilot's URL and API token in the **Connection** card and save (add the username and password only if an older MoviePilot rejects the token).
3. The series must have been scraped, with a tmdbid in its `tvshow.nfo`. Series without a tmdbid are not sent.

### The series list

The card sorts the series in your library into tabs by what there is to do next; each tab shows how many series it holds:

| Tab | What is in it | What you can do |
| --- | --- | --- |
| 缺集 (missing) | Compared with TMDB, really missing episodes, and not subscribed yet | **Fill** (補全) on a series, or **Fill these N** (補全這 N 部) next to the tabs |
| 處理中 (in progress) | Every season with missing episodes has been handed to MoviePilot: still subscribed (it is looking for a release), or already sent to the downloader and not in the library yet | Usually nothing; **Fill again** (再補一次) if the download failed |
| 季號對不上 (season mismatch) | After every season was compared: seasons in the library that TMDB does not have (for example TMDB merges several seasons into one; series that still have uncompared seasons stay on 還沒對照 first), which MoviePilot cannot subscribe to; the tab is hidden when there are none | **Organise** (去整理): pins the series folder at the top of **115 → Organise 115** (115 網盤 → 整理 115 網盤); in the preview, set the TMDB season number (or the episode format) for the season that does not match, then organise |
| 還沒對照 (not compared) | New series and new seasons not yet compared with TMDB; series without a tmdbid are here too (scrape them first) | **Check** (檢查) on a series, or **Check these N** (檢查這 N 部) |
| 不補 (skipped) | Series you marked **Skip** | **Fill again** (恢復補全) |
| 全部 (all) | Every series; complete ones are marked 齊全 | |

- **Check missing** (檢查缺集, called 重新檢查 once something has been compared; hidden on the 還沒對照 tab, where **Check these N** (檢查這 N 部) above the list checks only the series not compared yet) at the top right asks MoviePilot once per season for the episodes TMDB says have aired and only records which ones are missing. It creates **no subscriptions and downloads nothing**, and works with just the API token. Seasons checked within the last 6 hours are not asked again. A fill records the same data as it goes. When the library later gains or loses episodes, the list is recalculated on the spot without asking again.
- Each season with missing episodes gets its own line: "has N episodes, TMDB aired M" (有 N 集・TMDB 已播出 M 集), how many are missing and which, plus 已訂閱 (subscribed) or 已送下載 (sent to download, with how long ago) when MoviePilot is handling it (see below). A season without gaps can still lack its first or last episodes; only the comparison shows that. For seasons not compared yet, only gaps in the episode numbers are visible (for example episodes 2 and 4 but not 3).
- **Fill these N** sends only the series on the 缺集 tab: series MoviePilot is already handling, skipped and complete series are not sent. It asks for confirmation first.
- **已訂閱 and 已送下載**: once MoviePilot finds a release and hands it to the downloader, it marks the subscription as completed and moves it from the subscription list to the subscription history, while the episodes are still downloading and not yet organised into the library. Mi302 marks such a season 已送下載 and keeps it on the 處理中 tab together with the seasons that are still subscribed; no fill (manual, bulk, or automatic after a full sync) subscribes to it again. After the download is organised into the library and synced, nothing is missing and the series leaves the tab.
- A season sent to download more than 3 days ago that still has not arrived (failed download, deleted torrent) goes back to 缺集 and can be filled again. To retry sooner, press **Fill again** (再補一次) on that series: it subscribes once more and asks MoviePilot to search again. Episodes that aired after the download was sent are not covered by it and still show up under 缺集.
- The 處理中 tab needs MoviePilot's subscription list and subscription history; when they cannot be read (MoviePilot unreachable or refusing access) the tab is not shown and every series with missing episodes stays on 缺集.
- Type into **Search series** (搜尋劇名) to search by title, original title, year, pinyin or pinyin initials. Next to it, the **All libraries** drop-down (全部媒體庫) shows only the series of one library (for example Chinese dramas or Chinese animation; it is hidden when there is only one series library), and **All years** (全部年份) shows only series from one year. The three can be combined.
- **The buttons act on what the filters show**: after a search or with a library or year selected, the number on each tab counts only the filtered series; **Fill these N** and **Check these N** (檢查這 N 部) handle only those, and the button at the top right turns into **Check these N** (重新檢查這 N 部 once something has been compared). The confirmation names the scope. For example, select the year 2026 and press **Fill these N** to fill only the 2026 series with missing episodes; select a library and press **Check these N** to check only that library.
- When the list does not fit on one page, paging and the page size (20, 50, 100 or 200) appear below it; the browser remembers the size. Searching or changing the library, year or tab goes back to page 1.
- **Skip** (不補) next to a series excludes it: checks, manual fills and the automatic fill after a full sync all pass over it, and it moves to the 不補 tab; **Fill again** (恢復補全) undoes it. Skipped series are remembered by tmdbid in Mi302's database, so rescans and folder renames keep them.
- While a check or fill runs, the top of the card shows the progress and a **Stop** button (停止): it stops after the current season, and subscriptions already created stay. Afterwards one line summarises the result; the per-season details can be expanded.
- Specials (season 0) are neither listed nor sent.

**Check missing** uses the same rules as a fill (see below) without creating subscriptions. Series marked **Skip** and series without a tmdbid are not checked.

### How each season is checked

Filling works season by season, and only for seasons that already have episodes in the library. A season that exists on TMDB but has no episodes at all in the library is not subscribed automatically.

1. Mi302 asks MoviePilot for the season's episodes and air dates on TMDB (`GET /api/v1/tmdb/{tmdbid}/{season}`). If TMDB has no such season (MoviePilot returns an empty list, and Mi302 confirms by asking which seasons the series has, so a temporary TMDB error does not count), no subscription is created: MoviePilot cannot get the season's episode count and would always refuse ("未获取到第 N 季的总集数"). The season is counted as "not on TMDB" and the series moves to the 季號對不上 tab.
2. It decides which episodes have aired:
   - An episode with an air date has aired if that date is not later than today (the date on the Mi302 host).
   - Episodes without a date are often placeholders for episodes not yet aired. One counts as aired if its number is not higher than the last episode of that season in the library (if you have episode 10, episode 3 has aired).
   - Undated episodes after the last one in the library are uncertain and are not counted as missing. The result notes how many there are.
3. It compares with the episode numbers Mi302 already has for that season. If every aired episode is there, no subscription is created, and the season counts as "already complete".
4. If more episodes are missing than **Skip seasons missing more than** (缺超過幾集的季不補) in Settings (default 0 = no limit), no subscription is created; the season counts as "missing too many" and the result says how many are missing. Useful for long anime where you only kept a few episodes, or shows where you only want some episodes, so hundreds of episodes are not subscribed at once.
5. Only if episodes are missing does it create a subscription (`POST /api/v1/subscribe/`), identifying the show by tmdbid (V3's `media_source` / `media_id`) rather than by name, so it cannot pick the wrong show.
6. For a new subscription, it then asks MoviePilot to search right away (`POST /api/v1/subscribe/search/{subscription id}`). MoviePilot V3 only schedules a search when a subscription is created, and sometimes the search waits for the next scheduled run. If this step fails, the result says so, and MoviePilot handles it at its next scheduled search.
7. Seasons MoviePilot is already handling (still subscribed, or sent to download within the last 3 days and not in the library yet) are not subscribed again; MoviePilot searches existing subscriptions on its own schedule (every 24 hours by default).

**Not all at once**: every new subscription makes MoviePilot search all your sites. It searches one subscription per site at a time, but starts the next one as soon as the last finishes; creating hundreds of subscriptions in one go hits the sites back to back and gets you blocked by Cloudflare. So new subscriptions are spaced out, 60 seconds apart by default (**Seconds between new subscriptions** (兩個新訂閱之間隔幾秒) in the Settings section, 0 = no gap), and the card shows when it is waiting. Only new subscriptions wait; complete seasons and existing subscriptions do not. A first fill of many series takes a while (100 new subscriptions take about 100 minutes); it runs in the background, so you can close the page.

If the TMDB episode list cannot be fetched (MoviePilot unreachable or an error), Mi302 creates the subscription anyway and leaves the decision to MoviePilot (the number missing is unknown, so the limit above does not apply).

When MoviePilot has downloaded and organised the files, it notifies Mi302 to rescan. If a season with missing episodes is still airing, the subscription keeps following new episodes.

### Cancel all subscriptions

If a fill created more subscriptions than you wanted and you would rather start over, open **MoviePilot subscriptions** (MoviePilot 的訂閱, with the current number in its title) at the bottom of the card and click **Cancel all subscriptions…** (取消所有訂閱…):

- It first shows how many subscriptions MoviePilot has (series, and movies or others) and only starts after you type 取消訂閱. Series and movie subscriptions are all cancelled, not just the ones Mi302 created.
- They are cancelled one by one in the background (`DELETE /api/v1/subscribe/{id}`). The card shows the progress, and **Stop** (停止) keeps whatever has not been cancelled yet; what is already cancelled does not come back.
- Only the subscriptions are removed. Files already downloaded and organised are untouched; MoviePilot just stops searching for and following those titles.
- Mi302 keeps the list of subscriptions it cancelled (name, year, type, season, tmdbid): when it is done the card offers **Download (JSON)** (下載（JSON）), so you can subscribe again if you cancelled by mistake.
- It does not run at the same time as a check or a fill. With the API token or an administrator account it sees and cancels everyone's subscriptions.

### Results

The numbers on the card:

| Label | Meaning |
|---|---|
| Subscribed and searched (建訂閱並搜尋) | seasons with missing episodes for which a subscription was created and a search requested |
| Missing episodes (缺的集數) | total number of missing episodes across all seasons |
| Already complete (已經齊全) | seasons where every aired episode is present, so no subscription was created; also seasons an older MoviePilot rejected with "媒体库中已存在" (already in library) |
| Subscribed before (之前訂閱過) | seasons that already have a MoviePilot subscription and were not subscribed again |
| Sent to download (已送下載) | seasons MoviePilot already found a release for and sent to the downloader, not in the library yet, and not subscribed again |
| No tmdbid (沒 tmdbid) | series skipped for lack of a tmdbid |
| Missing too many (缺太多不補) | seasons missing more than the limit, so no subscription was created |
| Not on TMDB (TMDB 沒有) | seasons TMDB does not have (season numbers do not match), so no subscription was created |
| Skipped (標了不補) | series passed over because they are marked **Skip** |
| Failed (失敗) | seasons MoviePilot refused to subscribe, or that were not processed after the run stopped |

Expand "Results per season (N)" (每一季的結果（N）) to see which episodes each season misses and what MoviePilot replied. A connection or authentication failure stops the run, and the remaining seasons count as failed. Only one fill runs at a time; clicking again during a run shows "already filling, wait for it to finish" (已經在補全中，等它做完).

### Fill after full sync

With **Fill missing episodes after full sync** (全量同步後自動補全) ticked (off by default; saved as soon as you toggle it), every full sync ends by sending all series with a tmdbid, after scraping has finished. Nothing is sent if MoviePilot is not set up. It waits for scraping because newly scraped series only have a tmdbid at that point. Series marked **Skip** and seasons missing too many are passed over here too.

## qBittorrent stalled torrents

MoviePilot hands the torrents it finds for subscriptions to qBittorrent. When qBittorrent's queue is on (a limit on how many download at once), a torrent nobody seeds sits at 0 speed but still takes a slot, and the ones queued behind it never get a turn. Mi302 can connect to qBittorrent, delete such torrents and let the queued ones start.

On the **MoviePilot** tab, click **qBittorrent**:

1. Enter the WebUI address (for example `http://127.0.0.1:8080`), click **Save** (儲存), then **Test connection** (測試連線). If qBittorrent runs on the same machine as Mi302 and its WebUI skips authentication for localhost, leave the username and password empty; otherwise enter the WebUI credentials.
2. Turn on **Delete torrents with no speed for too long** (自動刪掉太久沒速度的種子). It is off by default and asks for confirmation before turning on.

How it decides:

- The torrent list is read every 5 minutes. Only torrents that are downloading and not finished are considered (downloading, stalled, fetching metadata, forced); queued, paused, checking, seeding and finished torrents are left alone.
- Counting from when Mi302 sees a torrent downloading, if its average speed stays at or below **KB/s at or below which counts as no speed** (平均速度不超過幾 KB/s 算沒速度; default 0, meaning nothing downloaded at all) for **Minutes without speed before deleting** (連續幾分鐘沒速度就刪; default 60, 10–10080), it is deleted. Any speed in between restarts the count.
- Time spent queued or paused does not count: a torrent that waited days for its turn is timed from when it starts downloading, so it is not deleted the moment it starts. qBittorrent's own "last activity" time cannot tell these apart, so it is not used.
- The count lives in memory only: after Mi302 restarts or loses its connection to qBittorrent, it starts over.
- By default the partly downloaded files are deleted too; with **Also delete partly downloaded files** (連同下載到一半的檔案一起刪) off, only the torrent is removed from qBittorrent and the files stay in the download folder.
- With **Only delete torrents with no seeds** (只刪做種數為 0 的) on, a torrent whose tracker still reports seeders is listed but kept ("還有人做種，先不刪" on the card); it is deleted once the seed count drops to 0 and it still has no speed. A tracker that reports no seed count counts as 0. Note that on private trackers a torrent can have seeders that are unreachable, and such a torrent stays stuck.
- For every torrent deleted, the next one waiting starts (in queue order; without a queue, oldest added first). Queued torrents are started by qBittorrent itself once a slot frees up; paused ones are started by Mi302.

- **Torrents to keep downloading** (隨時要有幾個種子在下載; default 0 = do not manage): when fewer torrents than this have speed, queued ones are force-started in queue order (a force start bypasses qBittorrent's queue limits, so it works even when dead torrents fill the queue), until the queue runs out. A force-started torrent that still has nothing downloaded after **Seconds without speed after a force start** (強制開始後幾秒內沒速度就刪; default 30) is deleted if its tracker responds normally, because then the torrent itself is the problem. If the tracker does not respond (site down, Cloudflare block), or **Only delete torrents with no seeds** is on and it still has seeders, the force start is cancelled, the torrent goes back to the queue and is not retried for an hour, so a tracker outage cannot wipe the whole queue. Once it has speed it is handled by the normal rules.

The card lists the torrents currently without speed, for how long, and how long until they are deleted. **Check now** (現在看一次) checks immediately (with automatic deletion on, torrents past the limit are deleted as usual). Expand **Recently deleted** (最近刪掉的) to see what was deleted: when, how long it had no speed, how far it got and whether the files went too; each deletion is also logged.

When MoviePilot hands a torrent to qBittorrent it considers those episodes sent to download, and it does not notice the torrent being deleted. A season sent more than 3 days ago that has still not arrived goes back to 缺集 and can be filled again; to retry sooner, press **Fill again** (再補一次) on that series, see [The series list](#the-series-list).

## Organising 115

Series with wrong episode numbers and folders not named to MoviePilot's format are organised or deleted on the **Organise 115** (整理 115 網盤) card on the 115 tab; see [115 cloud sync](115-Cloud-Sync#organising-115). What concerns MoviePilot:

- MoviePilot must be v2.11.1-1 or newer. Older versions do not support preview and would really organise when asked to preview, so Mi302 checks the version first (`GET /api/v1/system/env`) and sends nothing if it is too old or unknown.
- Set up MoviePilot on the **Connection** card. On V3 the API token is enough; on older versions the organised-name query, manual transfer, episode-format recommendation and version APIs only accept an account login, so fill in an administrator's username and password.
- MoviePilot's 115 storage must be logged in to the same 115 account as Mi302. Mi302 sends the 115 folder and file IDs, which MoviePilot needs to move files.
- APIs used: `GET /api/v1/transfer/name` (what something would be called), `POST /api/v1/transfer/manual` (preview and run; a folder is sent as one `fileitem`, as in MoviePilot's own **File manager → Organise**), `POST /api/v1/transfer/episode-format/recommend` (episode format recommendation), `GET /api/v1/storage/directories` (directory settings; `/api/v1/system/setting/Directories` on V2), `POST /api/v1/transfer/manual/target-path` (where MoviePilot itself would organise to), `POST /api/v1/transfer/manual/history` (whether it has organise records).
- Mi302 turns off the **by type**, **by category**, **scrape metadata** and **reuse recognition from history** switches of MoviePilot's organise dialog and moves files, so MoviePilot only renames folders and files with its own format. With **MoviePilot's directory settings**, a folder already inside one of its library folders also stays in its current category folder (see [115 Cloud and sync](115-Cloud-Sync#what-to-check-for-each-entry)). When organising into a given folder that matches no directory setting, existing files are not overwritten.

## Mi302 Organizer plugin

Many folders in **Organise 115** (整理 115 網盤) already have the right structure and only the names are off, for example `H-画江湖之天罡-2023-[tmdb=1221210]` should become `画江湖之天罡 (2023) {tmdbid=1221210}`, `Season 1` should become `Season 01`, and the file names should follow MoviePilot's format. Those only need one rename per file and folder. MoviePilot's organise flow looks up the source, looks up the target, moves, looks up again and renames, roughly 7 to 8 requests to 115 per file at most 3 per second, so a 265-episode show takes over ten minutes.

`moviepilot-plugin/` in the Mi302 repository is a MoviePilot V3 plugin, **Mi302 整理助手** (Mi302 Organizer). Once it is installed and enabled, Mi302 hands it the rename list for folders whose preview shows they only need renaming; it renames them with MoviePilot's own 115 authorisation, one request per file, sharing MoviePilot's rate limit and its cool-down when 115 rate-limits. Anything that has to move elsewhere, gain a season folder or merge into an existing folder still goes through MoviePilot's organise flow.

From 1.1.0 the plugin also **works out names**. When the destination is already settled (the folder is already in a library folder, or **Same level** or a chosen folder is used), Mi302 sends it the file list of each part, and it calls the same MoviePilot functions MoviePilot uses to name files (parse the file name, recognise by TMDB id, keep the title from the organise history, fetch the season's episodes, apply the rename format, add the language tag to subtitles). The names are identical to what MoviePilot would produce, without running its whole organise preview (no artwork, no directory matching), and each title is recognised once. A folder with hundreds of episodes takes seconds instead of minutes, and MoviePilot no longer uses gigabytes of memory for previews. Files it cannot organise get clearer reasons (such as 未识别到文件集数, "no episode number", instead of only 整理任务处理失败). With an older plugin, or if naming fails, that part falls back to MoviePilot's organise preview and the preview says so; when MoviePilot picks the directory itself its preview is used as before. Anything that has to move is still organised by MoviePilot, which produces the same names as the preview.

Installing (MoviePilot V3):

1. Make the `moviepilot-plugin` folder visible to MoviePilot. On the same machine as Mi302 it is `moviepilot-plugin` inside the Mi302 install directory, for example `/root/Mi302/moviepilot-plugin`; otherwise copy the folder over.
2. Add `PLUGIN_LOCAL_REPO_PATHS=/root/Mi302/moviepilot-plugin` (your path) to `app.env` in MoviePilot's config directory and restart MoviePilot. The path must exist, or MoviePilot fails to load any plugin at start-up.
3. **Mi302 整理助手** appears in MoviePilot's plugin market. Install it, turn on **Enable** (啟用) and save.
4. Keep **Use MoviePilot's Mi302 Organizer plugin for names and renames** (用 MoviePilot 的「Mi302 整理助手」外掛算名字、改名) on the **Organise 115** card (on by default). Previews of rename-only folders then say so (只需要改名…).

When an update of Mi302 brings a new plugin version, update or reinstall it from MoviePilot's plugin market (for example only 1.1.0 works out names; until it is updated Mi302 keeps using MoviePilot's organise preview). From 1.2.0 the plugin first checks that this MoviePilot version still has the internal functions it uses to work out names (a MoviePilot update may remove them); if any is missing it does not work out names, Mi302 falls back to the organise preview, and the preview notes say the plugin does not match this MoviePilot version (MoviePilot's log lists what is missing).

Details:

- What counts as rename-only: for every file to be sent, the old and new locations have the same folder depth, each old folder maps to exactly one new name, and no new name collides with an existing sibling folder or a file staying behind. Movies without their own folder, and episodes sitting directly in the show folder (which need a season folder), do not qualify.
- Order: files first (videos, subtitles, audio), then inner folders, then the outer folder. If it fails or is stopped halfway, what is done stays done; previewing again treats those as already named and continues with the rest.
- Renames done through the plugin are not added to MoviePilot's organise history. Afterwards Mi302's incremental sync follows 115's activity log and moves the local strm files.
- The plugin's API is under `/api/v1/plugin/Mi302Organizer/` (`status`, `names`, `rename`, `job`, `cancel`), using a MoviePilot login or API token. It only renames what Mi302 sends and never changes anything on its own.
- When 115 rate-limits (429), MoviePilot pauses all 115 operations for an hour, and the plugin's renames wait as well.

## Mi302 Torrent Cleaner plugin

The [qBittorrent stalled torrents](#qbittorrent-stalled-torrents) rules are also available as a MoviePilot V3 plugin, **Mi302 清種助手** (Mi302 Torrent Cleaner), in the same `moviepilot-plugin/` folder as the Organizer plugin (install it the same way; it shows up as a second plugin in the market). The differences:

- The plugin uses the qBittorrent downloaders already configured in MoviePilot, so there is no URL or password to enter; with several downloaders you can pick which ones to manage (none selected = all qBittorrent downloaders).
- Scheduling is done by MoviePilot (**check every N minutes**, default 5); the quick re-check a few seconds after a force start still happens. **Run once now** (立即看一次) runs a round immediately.
- By default only torrents tagged `MOVIEPILOT` (the tag MoviePilot adds) are considered, so torrents you added by hand are left alone; clear the tag field to consider all.
- It can send a MoviePilot notification when it deletes something. The plugin's detail page lists, per downloader, what the last round saw, the torrents currently without speed and the recent deletions.
- The other options (minutes without speed, speed threshold, delete files, only torrents with no seeds, torrents to keep downloading, seconds after a force start) and the rules are the same as in Mi302 itself.

Turn on either Mi302's own **MoviePilot → qBittorrent** or this plugin, not both: they would race to delete the same torrent and each counts the time without speed separately.

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

- The scrape, subscription and manual-transfer APIs may not accept the API token. Fill in the username and password if you see "access denied" (拒絕存取).
- The tmdbid parameters sent with each scrape are ignored, so MoviePilot identifies by file name as usual. This is slower and can pick the wrong show.
- Subscriptions: older versions read the `tmdbid` field, V3 reads `media_source` / `media_id`; Mi302 sends both. Older versions reject titles already in the library ("媒体库中已存在"), which Mi302 counts as "already complete".
- Subscription search: V3 uses POST, older versions GET. On HTTP 405 Mi302 retries with GET.
- TMDB episode lists: V3 and older versions use different response formats; both are understood.
- Organise 115: preview in manual transfer exists from v2.11.1-1; older versions get nothing sent (see [Organising 115](#organising-115)). The episode-format recommendation exists only in V3, so older versions fall back to Mi302's guess from the file names, or you fill in the format yourself.
- If MoviePilot lacks an API (HTTP 404), Mi302 shows "MoviePilot 沒有這個 API（…），請確認網址或升級 MoviePilot" (MoviePilot has no such API; check the URL or upgrade MoviePilot).

## Other uses of MoviePilot

When **Show cast and crew in Chinese** (演職人員顯示中文名) is on, Mi302 asks MoviePilot for TMDB person aliases (`GET /api/v1/tmdb/person/{id}`) to find Chinese names. See [Library and Scanning](Library-and-Scanning).
