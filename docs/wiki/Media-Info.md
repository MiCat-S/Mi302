[繁體中文](媒體資訊與探測) | [简体中文](媒体信息与探测) | **English**

This page explains how Mi302 gets a video's media info (resolution, HDR, audio tracks, subtitle tracks, chapters): it reads an existing `X-mediainfo.json` next to the video, or probes the file itself with ffprobe while staying inside 115's rate limits. The web admin page is in Traditional Chinese; original labels are given in parentheses.

## Why media info matters

Players need a video's resolution, HDR / Dolby Vision, audio tracks, embedded subtitles and chapters to show 4K and HDR badges, and to let you pick audio and subtitle tracks before playback starts. A `.strm` file is a single line with a URL, so none of this is in it. It has to be read from the video's header.

Without media info, Mi302 returns an empty list of media streams. The video still plays, but the badges and track choices are missing.

For a video with media info, players additionally receive:

- Media streams: video, audio, subtitle (with language, title, default and forced flags) and attachment streams.
- Container, resolution, whether it has subtitles, and the default audio track.
- Bitrate, file size and runtime, only where they are otherwise missing. If the nfo has no runtime, the probed runtime is used.
- Chapters. Learned intro and credits markers are added after them; see [Intro and Credits](Intro-and-Credits).

Playback itself does not change: it is still a 302 redirect to the 115 direct link, with no transcoding.

## X-mediainfo.json

For `X.strm` (or a local video `X.mkv`), Mi302 reads `X-mediainfo.json` from the same folder. The format is the one used by the "media info persistence" feature of the Emby plugin [StrmAssistant](https://github.com/sjtuross/StrmAssistant) (神醫助手):

```json
[{"MediaSourceInfo": {"Container": "mkv", "MediaStreams": [...]}, "Chapters": [...]}]
```

- Files produced by Emby with StrmAssistant, or by [emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo), work as they are. Put them next to the strm file.
- They are read during scanning, and a copy is kept in Mi302's database. When a json file's modification time changes, the next scan reads it again.
- The other way round, json files written by Mi302's probing can be read by Emby with StrmAssistant.
- If StrmAssistant is set to keep the json files in a separate media info root folder, Mi302 cannot find them. Move them next to the strm files.
- When a video is renamed, moved or deleted on 115, sync renames, moves or deletes the local `X-mediainfo.json` together with the strm file.
- json files larger than 8 MB, or in the wrong format, are skipped with a warning in the log.

## Probing with ffprobe

For videos without a json file, Mi302 can probe the file itself with ffprobe. The settings are in the collapsed **Settings** section (設定) of the **Media info** card (媒體資訊) on the **Tools** tab (整理). There are two modes, and both can be on.

### Probe on open

**Probe automatically when a video is opened** (打開影片時自動探測) is on by default. When a player opens a movie or an episode (its detail page, or to start playback) and that item has no media info yet, it goes into a background queue. The request is answered right away without waiting; the next time the item is opened, the media info is there.

- Only strm items that are actually opened are queued. Browsing a list does not trigger probing, and opening a season does not queue the whole season.
- The queue holds at most 500 items. Items that do not fit are queued the next time they are opened.
- Items that failed or were skipped are not queued again for one hour.
- On an error that would make the rest fail too (115 rate limiting, not logged in to 115, ffprobe not found), the whole queue is dropped. Those items are queued again when opened after an hour.

If you do not want to probe a large batch at once, this mode is enough: the videos you actually watch gain media info over time.

### Picking what to probe

To probe a batch, use **Pick videos to probe** (挑選要提取的影片) in the card. First turn on **Batch probing** (批次探測) in the **Settings** section at the bottom of the card and click **Save settings** (儲存設定); it is off by default. Until then, **Start** (開始提取) and the per-title **Probe** buttons (提取) are disabled.

Filters; anything left empty is not limited:

| Filter | What it does |
|---|---|
| Search titles (搜尋片名) | Matches the title, original title, Simplified or Traditional characters, full pinyin and pinyin initials; `qyn` and `慶餘年` both find 庆余年 |
| All libraries (全部媒體庫) | Limit to one library |
| Movies and series (電影和劇集) | Switch to movies only (只看電影) or series only (只看劇集) |
| Year from / to (年份從、到) | Only these years; for series, the series' year. Fill in one side for "from year X on" or "up to year X". With a year filter set, titles without a year are left out |

**Order** decides both the list order and the probing order; within a series, episodes go by season and episode number:

- Recently added first (最近加入的先做, the default): what entered the library most recently goes first.
- Newest year first (年份新的先做) or oldest year first (年份舊的先做): by the movie's or series' year.
- Highest rated first (評分高的先做): by the rating in the nfo.
- By title (照片名): Chinese titles sort by pinyin.

**At most N this time** (這次最多 … 支) caps how many videos one run probes. The default is 100; 0 means everything that matches. The order and this number are remembered in your browser.

The list below shows movies and series that still lack media info, 20 per page. A series shows how many episodes it lacks (缺 X / Y 集); a movie shows "missing" (還沒有). A line above the list says how many movies and episodes match and still lack media info, and which ones **Start** would probe.

- **Start** (開始提取): probes the first N videos, using the filters and the order.
- **Probe** (提取) next to a title: probes only that movie or series (the episodes it lacks), again at most N.

Only one run happens at a time, and **Start** and **Probe** are disabled while it runs. During a run:

- Changing **At most N this time** applies to the running batch right away: a higher number adds videos using the same filters and order, a lower one drops videos still waiting in the queue. This works for runs you started by hand, not for runs started automatically after a sync.
- **Stop probing** (停止提取) next to the progress bar stops the run: queued videos are dropped, a video already in ffprobe is finished, and one still waiting for its direct link is abandoned. The result says stopped (已停止); videos not done do not count as failed, and the next **Start** picks up what is still missing.

When it finishes, the list and the numbers refresh by themselves. The result says how the run was picked, for example "manual, newest year first, at most 100" (手動・年份新的先做・最多 100 支) or "manual, 庆余年, at most 100".

### Probing after sync

**Probe automatically after sync creates new strm files** (同步產生新的 strm 後自動探測) is on by default and only works with batch probing on. It probes, in the background, the strm files each sync creates, and strm files whose file on 115 was replaced (see below). Probing runs alongside scanning and scraping; they do not wait for each other.

### What probing one item does

1. Reads the strm file (after applying `redirect.path_rules`).
2. For a 115 strm, fetches one direct link from 115. 115 direct links are usually tied to the User-Agent that requested them, so the link fetch and ffprobe use the same ordinary browser User-Agent. ffprobe reuses one connection while reading, which triggers 115's CDN rate limiting less often.
3. If the strm holds another URL (for example alist), ffprobe reads that URL directly. If it holds a local path, or the item is a local video, ffprobe reads the local file. Neither case fetches a 115 direct link or counts towards the hourly cap.
4. ffprobe reads only the header, usually a few MB.
5. The result is written as `X-mediainfo.json` next to the strm file and stored in the database. Chapters come from the video if it has any; otherwise, as in Emby, one chapter is generated every 5 minutes. If the media folder is read-only, the result is kept in the database only.
6. If the video had no runtime, the probed runtime is filled in.

Each item may take at most `mediainfo.timeout` seconds (default 300); after that it counts as failed.

## Settings

| Label in the web UI | Key | Default | Range |
|---|---|---|---|
| Probe automatically when a video is opened (打開影片時自動探測) | `mediainfo.on_demand` | on | |
| Batch probing (批次探測) | `mediainfo.enabled` | off | |
| Probe automatically after sync creates new strm files (同步產生新的 strm 後自動探測) | `mediainfo.after_sync` | on | needs batch probing |
| Concurrent probes (同時探測幾項) | `mediainfo.concurrency` | 2 | 1–3 |
| Direct-link interval in seconds (取直鏈間隔（秒）) | `mediainfo.interval` | 1.0 | 0.5–60 |
| Maximum per hour (每小時最多幾次) | `mediainfo.hourly_limit` | 300 | 0–100000, 0 = unlimited |
| (config file only) | `mediainfo.timeout` | 300 | 10–3600 seconds |
| (config file only) | `mediainfo.ffprobe` | `ffprobe` | path to ffprobe |

After changing them in the web UI, click **Save settings** in the **Settings** section at the bottom of the card. Changes apply immediately; no restart is needed. For the config file syntax, see [Configuration Reference](Configuration-Reference).

## 115 limits

Every 115 strm file needs one direct-link fetch from 115, and 115 is sensitive to this. Mi302 protects the account like this:

- **Connections**: 115 allows at most 3 connections at once, so concurrent probes are capped at 3 (default 2).
- **Interval**: at least 0.5 seconds between two direct-link fetches (default 1 second). All probing shares this interval.
- **Hourly cap**: at most 300 fetches per hour by default; 0 = unlimited. When the cap is reached, probing waits until the oldest fetch is an hour old; raising the cap (or changing the interval) and clicking **Save settings** while it waits resumes probing straight away. Probing tens of thousands of videos at once takes several days; you can also split it up with **At most N this time**.
- **Circuit breaker**: when 115 responds with rate limiting or an invalid login, probing and sync stop, and the rest of the current batch is not done. A breaker tripped by rate limiting clears itself after 45 minutes. An invalid login needs a new QR-code login on the **115 Cloud** tab (115 網盤) with the **Scan QR code** button (掃碼登入). An interrupted run does not restart by itself: click **Start** again once the breaker has cleared. Clicking it while the breaker is tripped stops the run straight away. For the first 30 minutes after the cool-down, the interval is 4 times longer and the hourly cap a quarter; for the next 30 minutes, twice as long and half; then back to normal.

Probe-on-open and batch probing share the interval, the hourly cap and the circuit breaker. Playback is not subject to these limits and is not stopped by the circuit breaker. The breaker's reason is shown in the **Media info** card and on the **115 Cloud** tab. For details on the circuit breaker, see [115 Cloud Sync](115-Cloud-Sync).

## When the file on 115 is replaced

Each file on 115 has a pickcode, an access code that the strm file uses to get a direct link. If a sync finds that the file at the same place on 115 was replaced (for example by another release), the pickcode changes. The old media info then belongs to a different file, so Mi302 deletes `X-mediainfo.json` and the database record:

- With batch probing and "probe automatically after sync" on, the file is probed again right after the sync.
- Otherwise it is probed the next time the item is opened, or when you probe it from **Pick videos to probe**.

A strm file rewritten only because the server URL (`p115.strm.base_url`) or the strm URL format changed keeps the same pickcode, so its media info stays.

## What the card shows

The **Media info** card shows:

- "N / M videos have media info" (N / M 支影片有媒體資訊).
- A warning when ffprobe is not found, and the reason when the 115 circuit breaker is tripped.
- The probe-on-open queue: how many are queued, done and failed.
- How many direct links were fetched from 115 in the last hour and the cap; how much probing is slowed down right after the breaker recovers; and, when the cap is reached, the time probing continues.
- The last batch run (after sync or manual; a manual run shows how it was picked): to probe, succeeded, failed, skipped, and the first few failure reasons.

Why items fail or are skipped:

- **Skipped**: the strm file is empty, or it holds neither a URL nor an existing file.
- **Whole run stopped**: ffprobe not found, not logged in to 115, or the 115 circuit breaker tripped. Items not yet done count as failed.
- **Single item failed**: ffprobe could not read the file (for example 403, timeout, decoding error), the item took longer than the per-item timeout, or no streams were found. URLs are removed from error messages before they reach the card and the log.

Failed videos still have no media info. The next run tries them again.

## Installing ffmpeg

ffprobe is part of ffmpeg.

- The one-line installer tries to install ffmpeg with the system package manager (apt, dnf, yum, apk, pacman, zypper, brew) during installation and on `mi302 update`. If that fails, it only prints a warning; everything else works. See [Installation](Installation).
- To install it yourself:

```bash
sudo apt install ffmpeg   # Debian / Ubuntu
brew install ffmpeg       # macOS
```

- No restart is needed after installing; Mi302 finds ffprobe within a minute.
- If ffprobe is not on the service's PATH, set `mediainfo.ffprobe` in the config file to its full path.

Without ffprobe, Mi302 still reads existing `X-mediainfo.json` files. The card shows a warning, **Start** and **Probe** are disabled, and neither probe-on-open nor probing after sync runs.

## Credits

The rules that map ffprobe output to Emby fields, and the approach to rate limiting and the circuit breaker, are adapted from [emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo) (MIT license). The original license notice is kept in [embyserver/mediainfo.py](https://github.com/MiCat-S/Mi302/blob/main/embyserver/mediainfo.py). The file format follows [StrmAssistant](https://github.com/sjtuross/StrmAssistant).
