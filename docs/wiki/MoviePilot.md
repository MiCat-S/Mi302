[繁體中文](MoviePilot-整合) | [简体中文](MoviePilot-集成) | **English**

Mi302 does not scrape (fetch metadata and artwork) by itself; it hands this to [MoviePilot](https://github.com/jxxghp/MoviePilot). This page covers connecting to MoviePilot, which videos are sent for scraping, how filling missing episodes decides what is missing, and how to add Mi302 to MoviePilot as an Emby media server. The web admin page is in Traditional Chinese; original labels are given in parentheses.

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
| MoviePilot username, password (MoviePilot 帳號, MoviePilot 密碼) | `moviepilot.username`, `moviepilot.password` | empty | needed to fill missing episodes, and by the scrape API of older versions |
| Fill missing episodes after full sync (全量同步後自動補全) | `moviepilot.fill_after_full_sync` | off | in the fill card; saved as soon as you toggle it |
| (config file only) | `moviepilot.timeout` | 300 | seconds to wait per item; exceeding it counts as a connection failure and stops the batch |

### When you need the username and password

The account fields are in a collapsed section of the **Connection** card, "MoviePilot account (needed to fill missing episodes; also by the scrape API of older versions)" (MoviePilot 帳號密碼（補全缺集需要；舊版刮削 API 也需要）). Click it to expand.

- **Filling missing episodes**: MoviePilot's subscription API only accepts a logged-in account, not the API token. You must fill these in.
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
- If MoviePilot lacks an API (HTTP 404), Mi302 shows "MoviePilot 沒有這個 API（…），請確認網址或升級 MoviePilot" (MoviePilot has no such API; check the URL or upgrade MoviePilot).

## Other uses of MoviePilot

When **Show cast and crew in Chinese** (演職人員顯示中文名) is on, Mi302 asks MoviePilot for TMDB person aliases (`GET /api/v1/tmdb/person/{id}`) to find Chinese names. See [Library and Scanning](Library-and-Scanning).
