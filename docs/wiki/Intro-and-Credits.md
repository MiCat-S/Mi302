[繁體中文](片頭片尾跳過) | [简体中文](片头片尾跳过) | **English**

Mi302 learns where each season's intro and credits are from the way people watch, so players can offer "Skip intro". Seasons it gets wrong, or that you want set up in advance, can be set by hand. This page explains how it learns, how to set values by hand, what players receive, how to test it with SenPlayer, and where the data is kept. The web admin page is in Traditional Chinese; labels are given in English with the original in parentheses.

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
- If the season has manual settings, the manual values win for whatever was set; see "Manual settings" below.

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

1. Make sure **Learn intros and credits and send them to players** (學片頭片尾並給播放器) in the collapsed **Settings** section (設定) of the **Intro and credits** card (片頭片尾) on the **Tools** tab (整理) is on (it is by default).
2. In SenPlayer, open an episode of a series that has an intro and let it play normally for a few seconds.
3. Inside the intro zone (the first 10 minutes, or the first 25% for short episodes), skip forward past the intro in a single jump of 15 seconds to 3 minutes, for example by dragging the progress bar.
4. Keep playing for 10 to 20 seconds after the jump so SenPlayer reports the new position.
5. In the web admin page, the **Intro and credits** card should now list the season with "片頭 X → Y" (intro X → Y).
6. In SenPlayer, open another episode of the same season. When it reaches the intro, the skip-intro button should appear.

If nothing was learned, search the **Logs** tab (日誌) for `學到片頭` (intro learned; the log is in Traditional Chinese, so type those characters). Common causes: a jump longer than 3 minutes, a jump that started after the intro zone, or the player not reporting a position just before or after the jump.

To test credits, stop playback or move to the next episode within the last 5 minutes while at least 20 seconds remain. The card then shows "片尾在結尾前 …" (credits start … before the end). The episode needs a known runtime.

## The Intro and credits card

On the **Intro and credits** sub-tab of the **Tools** tab:

- The numbers at the top are seasons learned (學到的季), episodes with records (有紀錄的集) and seasons set by hand (手動設定的季).
- The search box finds every season of a series by title, including seasons nothing has been learned for yet. It matches the title, original title, Simplified or Traditional characters, full pinyin and pinyin initials, for example `qyn`.
- Without a search, the list shows seasons with learned records or manual settings, most recent activity first, 20 per page. Each season shows the series, the season number, "片頭 X → Y" (intro) and "片尾在結尾前 …" (credits start before the end), each marked learned (學到) or manual (手動); seasons set by hand carry a **Manual** tag (手動). The values are computed for the season's first episode.
- **Edit** (編輯) on each season opens the manual settings, see the next section.
- **Clear all** (清除全部) deletes every learned record after a confirmation; learning starts over as you watch. Manual settings are kept.
- **Learn intros and credits and send them to players**, in the **Settings** section, is `server.intro_skip`. It is saved as soon as you toggle it.

## Manual settings

If a learned value is off, or you want a season set up before anyone watches it, search for the series and click **Edit** (編輯) on the season:

| Field | Choices |
| --- | --- |
| Intro (片頭) | Automatic, learned from playback (自動（照播放行為學）); Manual (手動設定), with a start and an end; No intro in this season (這一季沒有片頭) |
| Credits (片尾) | Automatic (自動（照播放行為學）); Manual (手動設定), with how long before the end the credits start; No credits in this season (這一季沒有片尾) |

- Write times as minutes:seconds (for example `1:35`), hours:minutes:seconds, or plain seconds. Until a season has manual settings, the fields start with the learned values, so small corrections are quick.
- Credits are stored as "time before the end", so episodes of different lengths in one season still line up. A runtime is needed (the nfo `<runtime>` or media info) before they can be sent to players.
- Tick **Use this for every season of the series** (這部劇的每一季都用這個設定) to save the same settings for all seasons of that series.
- Seasons with learned records also have **Clear this season's learned records** (清除這一季學到的紀錄).
- Whatever is set by hand goes to players directly instead of what playback taught; intro and credits can be set separately, and the other one keeps learning. Playback is still recorded, so switching both back to Automatic returns to the learned values.
- The intro end and the credits length must be within 60 minutes, and the intro start must come before its end.

## Turning it off

With **Learn intros and credits and send them to players** off:

- No new records are learned.
- Nothing is sent to players: no chapter markers, the Intro Skipper endpoints return 404 or `Valid: false`, and media segments are empty.
- Learned records and manual settings stay in the database and are used again when you turn it back on. Use **Clear all** to delete the learned records.
- Manual settings are also only sent to players while this switch is on.

## Where the data lives

- Records are stored in the `intro_obs` table of the database `data/library.db`: one row per episode, user and kind (intro or credits), with start and end positions and a timestamp.
- They survive restarts and updates, and the daily automatic backup includes them, see [Backup and Restore](Backup-and-Restore).
- Only the current playback session is kept in memory: the last reported position and time for each user and episode, dropped when playback stops or after 6 hours of silence. A jump that happens exactly across a restart is therefore missed.
- Manual settings live in the `intro_manual` table of the same database, one row per season.
- When an episode leaves the library (the file is deleted or renamed), its records are deleted too; when a season disappears, its manual settings are deleted as well.
