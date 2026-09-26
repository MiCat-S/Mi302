[繁體中文](媒體庫與掃描) | [简体中文](媒体库与扫描) | **English**

This page covers how to lay out library folders, how Mi302 scans them, and the Chinese-specific features: pinyin sorting and search, Chinese names for cast and crew, Chinese genre names and uploaded library covers. The web admin page is in Traditional Chinese; button names are given in English with the original label in parentheses.

## Libraries and folders

You create libraries on the **Libraries** tab (媒體庫) of the web admin page: click **Add library** (新增媒體庫), give it a name, pick the type **Movies** (電影) or **TV shows** (劇集), click **Add folder** (加入資料夾) to choose a folder on the server, then click **Save and scan** (儲存並掃描). A library can have several folders.

Mi302 does not scrape (fetch metadata and artwork) itself. It only reads what is already in the folders:

- Videos: `.strm` files and regular video files (`.mkv` `.mp4` `.m4v` `.avi` `.ts` `.m2ts` `.mov` `.wmv` `.flv` `.webm` `.rmvb` `.mpg` `.mpeg` `.iso` `.3gp`).
- Metadata: nfo files and images (`.jpg` `.jpeg` `.png` `.webp`). These can be downloaded from 115 during sync, or produced by MoviePilot, see [MoviePilot](MoviePilot).

A typical layout:

```
movies/
  Inception (2010)/
    Inception (2010).strm
    Inception (2010).nfo        # or movie.nfo
    poster.jpg  fanart.jpg
tv/
  Dark (2017)/
    tvshow.nfo  poster.jpg  fanart.jpg
    Season 1/
      Dark.S01E01.strm
      Dark.S01E01.nfo
      Dark.S01E01-thumb.jpg
```

When looking for movie and series folders, folders whose names start with `.`, `@` or `#` are skipped (for example the NAS folders `@eaDir` and `#recycle`).

### Movies

- Each video file is one movie.
- If a folder holds exactly one video (and is not the library folder itself), the folder name becomes the title when it contains a year or the file name does not, for example `Inception (2010)`. Such folders are also checked for `movie.nfo`.
- Otherwise the title comes from the file name: `Title (Year)` is recognised, and words like `1080p`, `BluRay` or `x265` are stripped before taking the title and year.
- A `<title>` in the nfo always wins.

### Series

Each series folder is one series. Every video anywhere inside it is an episode of that series, whether it sits in a season folder or directly in the series folder.

## Season and episode naming

Season folder names:

| Pattern | Examples | Season |
| --- | --- | --- |
| `Season N`, `Series N` (optionally separated by a space, dot, underscore or hyphen) | `Season 1`, `Season.02`, `Series 3` | N |
| `SN` | `S01`, `s2` | N |
| `第N季`, `第N部` (Arabic or Chinese numerals) | `第1季`, `第一季`, `第十二季` | N |
| `Specials`, `Special`, `SP`, `特別篇`, `特别篇` | | 0 (specials) |

Episode numbers are taken from the file name. The patterns are tried in this order and the first match wins:

| Pattern | Examples |
| --- | --- |
| `SxxEyy` (also `SxxEPyy`, optionally separated by a space, dot, underscore or hyphen) | `Dark.S01E02.mkv`, `S01 EP02` |
| `NxNN` | `1x02` |
| `第N集`, `第N話`, `第N话` (Arabic or Chinese numerals) | `第2集`, `第十二集` |
| `Eyy`, `EPyy` (at the start, or after a space, dot, underscore, hyphen or `[`) | `EP02`, `[E05]` |

When several sources exist, this is the order of precedence:

- Season: `<season>` in the episode nfo → the season folder → `SxxEyy` in the file name → season 1.
- Episode number: `<episode>` in the episode nfo → the file name.
- Episode title: `<title>` in the episode nfo → "第 N 集" (episode N) → the file name.

Seasons are named "第 N 季" (season N) and season 0 is "特別篇" (specials). A `season.nfo` in the season folder can set the season's title and overview.

## Category folders

A TV library may contain category folders, such as the second-level categories MoviePilot creates: `电视剧/国产剧/庆余年 (2019)/`. Add `电视剧` as the library folder; you do not need to add each category separately.

A folder is a series if any of these is true:

- It contains `tvshow.nfo`.
- Its name is `Title (Year)`; full-width `（）` and `[]` brackets also work.
- It contains a season folder.
- It directly contains videos or strm files.

Any other folder is treated as a category and Mi302 looks one level deeper. It goes at most three category levels below the library folder.

Two layouts get misread:

- A category folder that directly contains a video is treated as a series.
- A series folder with no year, no `tvshow.nfo`, and episodes only in subfolders that are not season folders (for example `Disc1`) is treated as a category. Adding the year to series folder names is the safest option.

If you want each category to be its own row on the player's home screen, create one library per category.

## Image and nfo file names

`X` in the table is the video file name without its extension. Images can be `.jpg`, `.jpeg`, `.png` or `.webp`.

| Item | nfo | Poster | Backdrop | Thumb (landscape) | Logo |
| --- | --- | --- | --- | --- | --- |
| Movie | `X.nfo` | `X-poster`, `X` | `X-fanart`, `X-backdrop` | `X-thumb`, `X-landscape` | `X-logo`, `X-clearlogo` |
| Movie (also, when it is the only video in its folder) | `movie.nfo` | `poster`, `folder`, `cover` | `fanart`, `backdrop`, `background` | `thumb`, `landscape` | `logo`, `clearlogo` |
| Series (in the series folder) | `tvshow.nfo` | `poster`, `folder`, `cover` | `fanart`, `backdrop`, `background` | `thumb`, `landscape` | `logo`, `clearlogo` |
| Season | `season.nfo` in the season folder | `season01-poster`, `season01` in the series folder (`seasonspecials-poster`, `seasonspecials` for specials); otherwise `poster`, `folder`, `cover` in the season folder | | | |
| Episode | `X.nfo` | `X-thumb`, `X` | | | |

- An episode without its own image uses the series thumb (`thumb`/`landscape`), or the series backdrop if there is no thumb.
- Seasons and episodes without their own backdrop, thumb or logo use the series ones.
- nfo fields read: `title`, `originaltitle`, `sorttitle`, `plot` (or `outline`), `year`, `premiered` (or `aired`, `releasedate`), `rating`, `mpaa`, `genre`, `runtime` (minutes), `uniqueid` and `tmdbid`/`imdbid`/`tvdbid`, `season`, `episode`, plus cast and crew (see below).

## Scanning

A scan reads the videos, nfo files and images in the folders into the database. Players see what is in the database. You rarely need to rescan everything.

### Scanning from the web page

On the **Libraries** tab you can:

- Click **Scan** (掃描) at the top right of a library to scan only that library.
- Click **Scan** on a folder's row to scan only that folder. A missing folder is marked **Folder does not exist** (資料夾不存在) and has no scan button.
- Click **Scan folder…** (掃描資料夾…) at the top and pick any folder inside a library, for example a category `电视剧/国产剧` or a single series `电视剧/国产剧/庆余年 (2019)`. A folder outside every library is rejected with "這個位置不在任何媒體庫的資料夾裡" (this location is not in any library folder).
- Click **Rescan all** (全部重新掃描) to scan every library.

While a scan runs, the top of the page shows "掃描中" (scanning), a percentage and the title being processed. The first scan does not know the total yet and shows a sliding progress bar instead. Only one scan runs at a time; others wait in line.

### Automatic scans

Automatic scans also cover only what changed:

| When | What is scanned |
| --- | --- |
| Every time Mi302 starts | All libraries |
| A 115 sync finishes (if **Rescan libraries after sync** (同步完自動重新掃描媒體庫) is on, the default) | The series or movies containing files that were added, renamed, moved or deleted |
| MoviePilot finishes scraping | The items that were scraped |
| MoviePilot or another tool calls `POST /Library/Media/Updated` | The paths in the notification (mapped back to Mi302 paths with the MoviePilot path mappings); all libraries if the notification has no paths |
| A player refreshes one item (`POST /Items/{id}/Refresh`, admin account required) | That movie or series; for a library, the whole library |
| `POST /Library/Refresh` | All libraries |
| **Save and scan** on the Libraries tab | New or changed libraries; deleted libraries are removed together with their items |

### Scan units

Partial scans work on whole series and movies:

- TV libraries: a path to an episode or season rescans the whole series; a category folder scans every series inside it; files that are not inside any series folder are ignored.
- Movie libraries: the whole folder containing the change is rescanned, because the number of videos in a folder affects how titles are chosen; a video placed directly in the library folder is handled on its own.
- Deleted paths: the database items under that path are removed.
- Duplicate or nested paths are scanned once.
- More than 300 units at once (for example after the first full sync): Mi302 scans the affected libraries completely instead.

### Watch history

Items are matched by file path. Rescanning, partial or full, keeps each item's id, so watch history, favourites and intro/credits records survive. A renamed or moved file becomes a new item; the old item and its watch history are removed by the next scan that covers it.

## Network drives mounted late

Folders on NFS/SMB shares or Parallels shared folders (`/media/psf/...`) may be mounted after Mi302 starts. If a library folder does not exist, cannot be read or is completely empty, Mi302 treats it as not mounted yet:

- Full library scans (including the one at startup) keep the folder's existing items and watch history instead of treating every video as deleted, and log the warning "媒體庫路徑不存在或是空的" (library path does not exist or is empty).
- Partial scans skip the folder.

Once the share is mounted, click **Rescan all** on the Libraries tab.

The flip side: if you really empty a folder, its old items stay. Remove the folder from the library (or delete the library) and click **Save and scan**.

## Sorting and searching Chinese titles

Sorting:

- Movies and series sort by the nfo `<sorttitle>`, or by the title if there is none.
- Chinese is converted to Simplified and then to pinyin, one syllable per character separated by spaces: `流浪地球2` → `liu lang di qiu 2`. Other text is lower-cased. This makes alphabetical jump lists in players work.
- Episodes keep their season and episode order.

Search:

- Both the title and the original title (nfo `<originaltitle>`) are searched.
- A Chinese title can also be found by full pinyin (`qingyunian`), initials (`qyn`), Simplified or Traditional characters (`慶餘年` finds `庆余年`). Pinyin is generated from the title only, not the original title.
- Traditional-to-Simplified conversion sometimes picks different characters (`慶餘年` becomes `庆馀年`), so search terms with two or more Chinese characters are also compared by pinyin.

The sort names and search fields are built during scans. Mi302 scans everything at every start, so after an update older items are filled in automatically.

## Cast and crew

Mi302 reads cast and crew from nfo files and shows them in players:

- `<director>`: directors.
- `<credits>`, `<writer>`: writers.
- `<actor>`: actors, reading `name`, `role`, `thumb` and `tmdbid`; `type` set to `GuestStar` marks a guest star.

The order is directors, writers, then actors. Clicking a person lists their titles in Mi302, and people can be found by search. Episodes and seasons without their own cast use the series cast.

Person photos are the `<thumb>` URLs from the nfo (usually on TMDB). When a player asks for a photo, Mi302 redirects it there with a 302, so the player must be able to reach that image host.

### Chinese names

TMDB often has names in pinyin or English (Chen He). When **Show Chinese names for cast and crew** (演職人員顯示中文名, `server.chinese_people`, on by default) is enabled in the **Chinese localisation** card (中文化) on the **Advanced settings** tab (進階設定), Mi302 looks up Chinese names in the background:

- Only people with a numeric TMDB person id in the nfo are looked up: each title's directors, writers, guest stars, and the top-billed actors (the code allows 20 actors plus 10 places for directors and writers listed before them).
- First it asks MoviePilot's person endpoint (`/api/v1/tmdb/person/{id}`) and picks a Chinese name from the TMDB aliases (陈赫): names containing Japanese kana are skipped, names already in Simplified are preferred, otherwise a Traditional one is converted to Simplified. At most 2 requests per second.
- People MoviePilot has no Chinese name for, or everyone when MoviePilot is not set up or unreachable, are looked up on Wikidata by TMDB person id in batches of 100. Chinese labels are converted to Simplified.
- People with no result are retried after 30 days. If MoviePilot is configured but was unreachable this time, people that were only checked on Wikidata do not count as checked and are retried on the next run.
- Mi302 checks for new people every 30 minutes (the first check is 1 minute after start), up to 500 people per run. **Look up Chinese names now** (現在查中文名) starts a run immediately.

Players then show the Chinese name, and the person record carries the original name as `OriginalTitle`. Both the original and the Chinese name work in search.

The card shows counts of people, people with a Chinese name, not yet checked and checked without result, how many names came from each source, the last run and any error. Without MoviePilot it notes that only Wikidata can be used.

After you turn the switch off and click **Save settings** (儲存設定), players show original names right away and background lookups stop. Names already found stay in the database.

## Chinese genre names

With **Show genres in Chinese** (類型顯示中文, `server.chinese_genres`, on by default) in the same card, genres from nfo files are rewritten during scans:

- English TMDB and IMDb genres are replaced using the table below (case-insensitive), with the names TMDB uses in Simplified Chinese.
- Chinese genres in Traditional characters are converted to Simplified, so genres always appear in Simplified Chinese.
- English genres not in the table are kept. Duplicates after conversion are dropped.

Because this happens during scans, a change to the switch takes effect after a rescan. Turning it off and rescanning restores the genres exactly as they are in the nfo files.

| English | Chinese | English | Chinese | English | Chinese |
| --- | --- | --- | --- | --- | --- |
| Action | 动作 | Adventure | 冒险 | Animation | 动画 |
| Comedy | 喜剧 | Crime | 犯罪 | Documentary | 纪录 |
| Drama | 剧情 | Family | 家庭 | Fantasy | 奇幻 |
| History | 历史 | Horror | 恐怖 | Music | 音乐 |
| Mystery | 悬疑 | Romance | 爱情 | Science Fiction | 科幻 |
| Sci-Fi | 科幻 | TV Movie | 电视电影 | Thriller | 惊悚 |
| War | 战争 | Western | 西部 | Action & Adventure | 动作冒险 |
| Kids | 儿童 | News | 新闻 | Reality | 真人秀 |
| Sci-Fi & Fantasy | 科幻奇幻 | Soap | 肥皂剧 | Talk | 脱口秀 |
| War & Politics | 战争政治 | Biography | 传记 | Sport | 运动 |
| Sports | 运动 | Musical | 歌舞 | Film-Noir | 黑色电影 |
| Short | 短片 | Game-Show | 游戏节目 | Reality-TV | 真人秀 |
| Talk-Show | 脱口秀 | Anime | 动画 | | |

## Library covers

The cover of each library on the player's home screen comes from, in order:

1. An uploaded cover.
2. A `poster`, `folder` or `cover` image in the library folder (the first folder that has one, if the library has several).

Each library card on the **Libraries** tab shows the cover and where it comes from:

- **Upload cover** (上傳封面) accepts a jpg, png, gif or webp image of up to 30 MB.
- When an uploaded cover exists, **Restore default** (改回預設) deletes it and goes back to the folder image.

MoviePilot's library cover plugin uploads through the Emby endpoint `POST /Items/{id}/Images/Primary`, which has the same effect as uploading on the web page. See [MoviePilot](MoviePilot) for the setup.

Uploaded images:

- Are stored in `images/` inside the data folder (`data/images/` by default, `config/data/images/` for the one-line installer). A cover is named `{item id}-primary_image.{extension}`.
- Are not overwritten by rescans and take priority over images in the folder.
- Work the same way for posters, backdrops, thumbs and logos of other items such as movies and series. When an item leaves the library, its uploaded images are deleted.
