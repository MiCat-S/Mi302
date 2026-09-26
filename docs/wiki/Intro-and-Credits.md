[繁體中文](片頭片尾跳過) | [简体中文](片头片尾跳过) | **English**

Mi302 learns where each season's intro and credits are from the way people watch, so players can offer "Skip intro". This page explains how it learns, what players receive, how to test it with SenPlayer, and where the data is kept. The web admin page is in Traditional Chinese; labels are given in English with the original in parentheses.

## How it learns

While playing, a player reports its position every few seconds. From these reports Mi302 recognises "skipped a stretch near the start" and "stopped near the end or moved on to the next episode", and records them as the episode's intro and credits.

- It does not read the video, does not touch 115 Cloud (115 網盤), and does not need ffmpeg.
- Only TV episodes are learned, not movies.
- The approach follows the playback-behaviour intro detection of StrmAssistant (Emby 神醫助手).

### Intro

| Condition | Rule |
| --- | --- |
| Where the jump starts | Inside the intro zone: the first 10 minutes, or the first 25% for episodes shorter than 40 minutes (6 minutes for a 24-minute anime episode); the first 10 minutes if the runtime is unknown |
| How far one jump goes | 15 seconds to 3 minutes forward; a longer jump is skipping story, not an intro |
| Not fast playback | The position must advance by more than 3 times the real elapsed time plus 15 seconds |
| What is recorded | The last reported position before the jump → the first reported position after it |

Only that single jump counts; what happens afterwards does not matter. Because Mi302 works from reported positions, the result can be a few seconds off.

### Credits

Credits need the runtime, which comes from the nfo `<runtime>` or from media info (`X-mediainfo.json` or ffprobe probing). Episodes without a known runtime get no credits.

| Condition | Rule |
| --- | --- |
| Credits zone | The last 5 minutes, or the last 25% for episodes shorter than 20 minutes |
| Case 1: stopping | Playback stops inside the credits zone (next episode, or closing the player) with at least 20 seconds left → the stop position is recorded |
| Case 2: jumping | A forward jump starting inside the credits zone (again, not fast playback) that lands less than 20 seconds before the end, or that covers 60 seconds or more → the position before the jump is recorded |
| Not counted | Stopping less than 20 seconds before the end (the episode was finished); jumps that start before the credits zone |

The "60 seconds or more" rule covers a preview after the ending song: you skip the song but do not necessarily land at the very end.

### Which value an episode gets

- For each user, only the latest intro record and the latest credits record per episode are kept.
- If the episode has its own records (from any user): the intro start and end are the medians of the starts and ends; the credits start is the median.
- If it has none: the records of the other episodes in the same season are used. The intro again uses the medians of starts and ends; the credits use the median "time before the end", applied to this episode's runtime (so this episode needs a runtime too).
- An episode's own records take priority and are not mixed with the season's.
- Medians mean an occasional odd jump does not overwrite the result.

## What players receive

All three formats are served; each player uses whichever it understands:

| Format | Where | Content |
| --- | --- | --- |
| Emby chapter markers | `Chapters` in the item details (`/Users/{userId}/Items/{id}`, `/Items/{id}`, or queries with `Fields=Chapters`) | Chapters with `MarkerType` `IntroStart`, `IntroEnd` and `CreditsStart`, added after the chapters from media info |
| Jellyfin Intro Skipper plugin | `GET /Episode/{id}/IntroTimestamps` (also `/v1`), `GET /Episode/{id}/Timestamps` | Intro (and credits) in seconds; `IntroTimestamps` returns 404 when there is no intro |
| Jellyfin 10.10 media segments | `GET /MediaSegments/{id}` | `Intro` and `Outro` segments |

These are computed when a player asks. A newly learned intro shows up the next time the player loads that episode.

## Testing with SenPlayer

SenPlayer reads the Emby chapter markers (the first row above). To test:

1. Make sure **Learn intros and credits and send them to players** (學片頭片尾並給播放器) in the **Intro and credits** card (片頭片尾) on the **Libraries** tab (媒體庫) is on (it is by default).
2. In SenPlayer, open an episode of a series that has an intro and let it play normally for a few seconds.
3. Inside the intro zone (the first 10 minutes, or the first 25% for short episodes), skip forward past the intro in a single jump of 15 seconds to 3 minutes, for example by dragging the progress bar.
4. Keep playing for 10 to 20 seconds after the jump so SenPlayer reports the new position.
5. In the web admin page, the **Intro and credits** card should now list the season with "片頭 X → Y" (intro X → Y).
6. In SenPlayer, open another episode of the same season. When it reaches the intro, the skip-intro button should appear.

If nothing was learned, search the **Logs** tab (日誌) for `學到片頭` (intro learned; the log is in Traditional Chinese, so type those characters). Common causes: a jump longer than 3 minutes, a jump that started after the intro zone, or the player not reporting a position just before or after the jump.

To test credits, stop playback or move to the next episode within the last 5 minutes while at least 20 seconds remain. The card then shows "片尾在結尾前 …" (credits start … before the end). The episode needs a known runtime.

## The Intro and credits card

On the **Libraries** tab:

- The numbers at the top are "學到的季" (seasons learned) and "有紀錄的集" (episodes with records).
- The list shows the 20 most recently learned seasons: series, season number, "片頭 X → Y" (or "還沒學到片頭", no intro learned yet), "片尾在結尾前 …", how many episodes have records, and how long ago. The values are computed for the season's first episode.
- **Clear all** (清除全部) deletes every record after a confirmation; learning starts over as you watch. The admin API `POST /web/api/intro/clear` with `{"season_id": id}` clears a single season; the page has no button for that.
- **Learn intros and credits and send them to players** is `server.intro_skip`. It is saved as soon as you toggle it.

## Turning it off

With **Learn intros and credits and send them to players** off:

- No new records are learned.
- Nothing is sent to players: no chapter markers, the Intro Skipper endpoints return 404 or `Valid: false`, and media segments are empty.
- Existing records stay in the database and are used again when you turn it back on. Use **Clear all** to delete them.

## Where the data lives

- Records are stored in the `intro_obs` table of the database `data/library.db`: one row per episode, user and kind (intro or credits), with start and end positions and a timestamp.
- They survive restarts and updates, and the daily automatic backup includes them, see [Backup and Restore](Backup-and-Restore).
- Only the current playback session is kept in memory: the last reported position and time for each user and episode, dropped when playback stops or after 6 hours of silence. A jump that happens exactly across a restart is therefore missed.
- When an episode leaves the library (the file is deleted or renamed), its records are deleted too.
