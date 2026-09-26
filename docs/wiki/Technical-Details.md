[繁體中文](技術細節與開發) | [简体中文](技术细节与开发) | **English**

This page is for people who want to know how Mi302 works inside or want to contribute: the request flow, path matching, the Emby endpoints that are implemented, the admin API, the code layout, running the tests, and the projects Mi302 builds on.

## Request flow

Mi302 is a FastAPI application run by uvicorn. Its data lives in SQLite (`data/library.db`).

1. **Scan**: `.strm` files, regular video files, nfo files and images in the library folders are written to the database, see [Library and Scanning](Library-and-Scanning).
2. **Browse**: players call the Emby API (`/Users/{id}/Views`, `/Items`, …) and Mi302 builds Emby `BaseItemDto` objects from the database.
3. **Playback info**: `POST /Items/{id}/PlaybackInfo`. strm items come back with `Protocol=Http` and `IsRemote=true`, direct play only (transcoding off), and a `DirectStreamUrl` pointing to `/videos/{id}/stream.{container}`.
4. **Stream**: the player requests `/Videos/{id}/stream`, `/Videos/{id}/original.xxx` or `/Items/{id}/Download`:
   - strm items: Mi302 reads the strm and applies the path rules. With a pickcode it gets a 115 direct link using the player's User-Agent; otherwise it can first follow the upstream redirects. It answers `302 Location: <real URL>`.
   - Regular video files are served directly, with Range support.
5. **Progress**: `/Sessions/Playing…` stores the resume position and watched state, and feeds intro/credits learning, see [Intro and Credits](Intro-and-Credits).

See [Playback](Playback) for the details. Mi302 reports itself as Emby Server 4.8.11.0.

## Paths and parameters

Every incoming HTTP request has its path normalised before routing:

- ASCII letters in the path are lower-cased and other characters are left alone (the `É` in `/Persons/Émilie` stays), so paths are case-insensitive.
- A leading `/emby` or `/mediabrowser` is removed, so both prefixed and bare paths work.
- A trailing `/` is removed.
- Query parameter names are case-insensitive (`ParentId` and `parentid` both work).

Also:

- Error responses are plain text, as in Emby.
- CORS is open to all origins.
- FastAPI's generated API docs are at `/api-docs`.

## Authentication

The token is looked for in this order:

1. The header `X-Emby-Token` or `X-MediaBrowser-Token`.
2. `Token="…"` inside the header `X-Emby-Authorization` (or `Authorization`), in the form `MediaBrowser Client="…", Device="…", DeviceId="…", Version="…", Token="…"`.
3. The query parameter `api_key`, `X-Emby-Token`, `ApiKey` or `X-MediaBrowser-Token`.

Tokens are issued by `POST /Users/AuthenticateByName`. When the same user logs in again from the same device (`DeviceId`), the old token is revoked. API keys (from `api_keys` in the config file, or created in the web admin page) act as an admin (the first admin by name) and are meant for programs such as MoviePilot; they cannot call `/web/api/…` or `/p115/…`.

## Implemented Emby endpoints

Paths are written in Emby's usual casing but matched case-insensitively. Everything not marked "no login" needs a token.

### System and users

| Endpoint | Notes |
| --- | --- |
| `GET /System/Info/Public` | Server name, version and id; no login |
| `GET /System/Info` | Full server info |
| `GET, POST /System/Ping` | Returns `Emby Server`; no login |
| `GET /System/Endpoint` | Always reports "local network"; no login |
| `GET /Branding/Configuration` | Empty branding; no login |
| `GET /Users/Public` | Users for the login screen (empty when `server.public_users` is off); no login |
| `POST /Users/AuthenticateByName` | Log in with JSON or a form; the password field is `Pw` or `Password`; no login |
| `GET /Users` | All users; admin only |
| `GET /Users/{id}` | A user; only yourself unless you are an admin |
| `POST /Sessions/Logout` | Log out and revoke the token |
| `POST /Sessions/Capabilities`, `/Sessions/Capabilities/Full` | Accepted and ignored |
| `GET /Sessions` | Empty list |
| `GET, POST /DisplayPreferences/{id}` | Fixed defaults, nothing is stored; no login |
| `GET /Users/{id}/GroupingOptions`, `/Plugins`, `/Localization/{kind}` | Empty lists; no login |
| WebSocket `/embywebsocket` | Only answers KeepAlive |

### Libraries and items

| Endpoint | Notes |
| --- | --- |
| `GET /Users/{id}/Views`, `/Library/MediaFolders` | Library list |
| `GET /Library/VirtualFolders`, `/Library/VirtualFolders/Query`, `/Library/SelectableMediaFolders` | Libraries and their folders (used by MoviePilot) |
| `POST /Library/Refresh` | Rescan everything; admin only |
| `POST /Library/Media/Updated` | Scan only the paths in the notification; admin only |
| `POST /Items/{id}/Refresh` | Rescan only the series or movie containing this item; admin only |
| `GET /Users/{id}/Items`, `/Items` | Item queries, parameters below |
| `GET /Users/{id}/Items/{itemId}`, `/Items/{id}` | Full details of one item, including `MediaSources`, `People` and `Chapters`; returns a person for a person id |
| `GET /Users/{id}/Items/Latest` | Recently added (`Limit`, `ParentId`, `IncludeItemTypes`; episodes are grouped by series) |
| `GET /Users/{id}/Items/Resume` | Continue watching |
| `GET /Items/{id}/Ancestors` | Parent items |
| `GET /Items/Counts` | Numbers of movies, series and episodes |
| `GET /Shows/{id}/Seasons` | Seasons |
| `GET /Shows/{id}/Episodes` | Episodes (`SeasonId`, `Season`, `StartIndex`, `Limit`, `Fields`) |
| `GET /Shows/NextUp` | Next up (`SeriesId`, `Limit`) |
| `GET /Genres` | All genres |
| `GET /Persons`, `/Persons/{name}` | Search people, look up a person by name (original or Chinese) |
| `GET /Persons/{name}/Images/{type}` | Person photo, a 302 to the URL from the nfo; no login |
| `POST /Users/{id}/PlayedItems/{itemId}`; `DELETE` (or `POST …/Delete`) | Mark watched or unwatched; for a series or season, all its episodes |
| `POST /Users/{id}/FavoriteItems/{itemId}`; `DELETE` (or `POST …/Delete`) | Add or remove a favourite |

Query parameters for `/Items`: `ParentId`, `Recursive`, `IncludeItemTypes` (including `Person` also searches people), `ExcludeItemTypes`, `Ids`, `PersonIds`, `Person`, `IsFolder`, `SearchTerm`, `NameStartsWith`, `Years`, `Genres`, `Filters` (`IsPlayed`, `IsUnplayed`, `IsFavorite`, `IsResumable`), `IsPlayed`, `IsFavorite`, `SortBy`, `SortOrder`, `StartIndex`, `Limit`, `Fields` (`MediaSources`, `People`, `Chapters`), `UserId`.

`SortBy` accepts `SortName`, `Name`, `DateCreated`, `DateLastContentAdded`, `PremiereDate`, `ProductionYear`, `CommunityRating`, `CriticRating`, `Runtime`, `Random`, `DatePlayed`, `PlayCount`, `IndexNumber`, `ParentIndexNumber` and `AiredEpisodeOrder`.

Endpoints that return empty results so players do not fail: `/Users/{id}/Items/{id}/Intros`, `/Items/{id}/Similar`, `/Movies/{id}/Similar`, `/Shows/{id}/Similar`, `/Videos/{id}/AdditionalParts`, `/Items/{id}/CriticReviews`, `/Users/{id}/Items/{id}/LocalTrailers`, `/Users/{id}/Items/{id}/SpecialFeatures`, `/Items/{id}/SpecialFeatures`, `/Items/{id}/ThemeMedia`.

### Images

| Endpoint | Notes |
| --- | --- |
| `GET /Items/{id}/Images/{type}[/{index}]` | Read an image; `type` is `Primary`, `Backdrop`, `Thumb`, `Logo` or `Art`; no login |
| `POST /Items/{id}/Images/{type}[/{index}]` | Upload an image (base64 text or the raw image, up to 30 MB), stored in `data/images/`; admin only |
| `DELETE /Items/{id}/Images/{type}[/{index}]` | Delete the uploaded image and go back to the folder image; admin only |
| `GET /Users/{id}/Images/{type}` | User avatar; always 404 |

### Playback

| Endpoint | Notes |
| --- | --- |
| `GET, POST /Items/{id}/PlaybackInfo` | Media source |
| `GET, HEAD /Videos/{id}/{name}` | Stream (`stream`, `stream.mkv`, `original.mkv`, …); needs login when `redirect.require_auth` is on |
| `GET, HEAD /Items/{id}/Download`, `/Items/{id}/File` | Download; always needs login, returns 403 when `server.allow_download` is off |
| `POST /Sessions/Playing`, `/Sessions/Playing/Progress`, `/Sessions/Playing/Stopped` | Playback progress |
| `POST /Sessions/Playing/Ping` | Accepted and ignored |
| `POST /Users/{id}/PlayingItems/{itemId}`, `…/Progress`; `DELETE` (or `POST …/Delete`) | Older progress reporting API |

## Other endpoints

| Endpoint | Notes |
| --- | --- |
| `GET /Episode/{id}/IntroTimestamps` (also `/v1`), `GET /Episode/{id}/Timestamps` | Intro and credits in the Jellyfin Intro Skipper plugin format |
| `GET /MediaSegments/{id}` | Jellyfin 10.10 media segments |
| `GET, HEAD /d/{pickcode}[.ext][/name]` | Short links used in Mi302's own strm files, a 302 to the 115 direct link; no login |
| `GET, HEAD /p115/redirect?pickcode=…`, `/api/v1/plugin/p115strmhelper/redirect_url?pickcode=…` | Compatibility with other tools' strm files; no login |

## Admin API

The API behind the web admin page `/web`. Apart from `/web/api/setup`, it needs the token of a logged-in admin account (sent in the `X-Emby-Token` header); API keys are rejected. Requests and responses are JSON.

| Path | Purpose |
| --- | --- |
| `GET, POST /web/api/setup` | Whether the first admin still has to be created; create it (only while no account exists) |
| `GET, PUT /web/api/settings` | Read and save the web settings (written to the config file) |
| `GET, POST /web/api/scan` | Scan progress and item counts per library; start a scan with `{"library": name}`, `{"path": path}`, or neither for everything |
| `GET, POST /web/api/users`; `PUT, DELETE /web/api/users/{id}` | Users |
| `GET /web/api/browse`, `/web/api/115/browse` | Pick a folder on the server, pick a 115 directory |
| `POST /web/api/moviepilot/test`, `GET /web/api/moviepilot/status`, `POST /web/api/moviepilot/scrape` | MoviePilot connection test, status, scrape items missing metadata |
| `GET /web/api/series`, `POST /web/api/moviepilot/fill` | Series and episode-gap list; fill missing episodes |
| `GET /web/api/intro/status`, `POST /web/api/intro/clear` | Intro and credits (`{"season_id": id}` clears one season) |
| `GET /web/api/people/status`, `POST /web/api/people/resolve` | Chinese names for cast and crew |
| `GET, POST /web/api/backups`; `GET /web/api/backups/{name}` | List backups, back up now, download a backup |
| `GET /web/api/mediainfo/status`, `POST /web/api/mediainfo/probe` | Media info |
| `GET /web/api/logs`, `/web/api/logs/download` | Logs, download the log file |
| `GET, POST /web/api/apikeys`; `DELETE /web/api/apikeys/{key}` | API keys |
| `GET /p115/status` | 115 account status |
| `POST /p115/qrcode`, `GET /p115/qrcode/status`, `POST /p115/cookies`, `POST /p115/logout` | QR-code login, paste a cookie, log out |
| `POST /p115/open/qrcode`, `GET /p115/open/qrcode/status`, `POST /p115/open/logout` | 115 open platform authorisation |
| `POST /p115/strm/sync?mode=incremental` (full sync without `mode`), `GET /p115/strm/status`, `PUT /p115/strm/tasks` | Sync, sync status, sync tasks |

`GET /web` serves the admin page itself, and `/web/115` redirects to `/web#115`.

## Code layout

Each module under `embyserver/`:

| Module | Contents |
| --- | --- |
| `__main__.py` | Command-line entry: `python -m embyserver [-c config.yaml] [--scan] [--sync-115 [incremental]] [--reset-password USER NEW_PASSWORD]` |
| `app.py` | Builds the FastAPI app, normalises paths, starts background jobs |
| `auth.py` | Users, tokens, Emby authorization headers |
| `backup.py` | Daily backup of the database and config file |
| `config.py` | Config data classes and loading |
| `config_file.py` | Writes the settings back to `config.yaml` |
| `settings.py` | Settings editable in the web page: validation, applying, keeping the config file in step |
| `db.py` | SQLite tables and access |
| `dto.py` | Turns database rows into Emby `BaseItemDto`, `UserDto` and `MediaSourceInfo` |
| `filetypes.py` | Shared file extension lists |
| `http_util.py` | httpx client for external services that turns connection errors into readable reasons |
| `intro.py` | Learns intros and credits from playback |
| `logs.py` | Logging to the console, the log file and memory, with credentials masked |
| `mediainfo.py` | Media info: ffprobe output and `X-mediainfo.json` to Emby format |
| `prober.py` | Probes the videos behind strm files with ffprobe |
| `moviepilot.py` | Scraping and filling missing episodes through MoviePilot |
| `p115.py` | 115: QR-code login, direct links, directory listing, life events (115's activity log), circuit breaker |
| `p115_open.py` | 115 open platform channel |
| `people.py` | Cast and crew, Chinese names, Chinese genres |
| `redirect.py` | Resolves the real URL behind a strm, path rules, redirect cache |
| `scanner.py` | Library scanning |
| `strm_sync.py` | Creates strm files from 115 and downloads metadata |
| `textutil.py` | Pinyin sorting, pinyin search, Traditional/Simplified conversion |
| `routes/system.py` | System, Users, Sessions and similar endpoints |
| `routes/items.py` | Libraries, item queries, user data, images |
| `routes/playback.py` | PlaybackInfo, streaming, progress reports, intro endpoints |
| `routes/p115.py` | Admin API for 115 login and sync, pickcode short links |
| `routes/web.py` | The web admin page and its API |
| `routes/common.py` | Small helpers shared by the routes |
| `web/admin.html` | The web admin page (one file with HTML, CSS and JavaScript) |

Tables in `library.db`: `meta` (server id, 115 login state, sync progress and so on), `users`, `tokens`, `items`, `user_data` (watch history, favourites), `p115_index` (115 file id to local path), `media_info`, `people`, `person_names`, `intro_obs`, `mp_no_image`.

## Development and tests

Python 3.10 or later is required. In a virtual environment:

```bash
pip install -r requirements.txt pytest
python -m pytest
```

After changing `install.sh`:

```bash
shellcheck install.sh
```

After changing `embyserver/web/admin.html`, extract the `<script>` and check the JavaScript syntax:

```bash
python3 -c "import re; print(re.search(r'<script>(.*)</script>', open('embyserver/web/admin.html', encoding='utf-8').read(), re.S).group(1))" > /tmp/admin.js
node --check /tmp/admin.js
```

## Credits

- [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins): the 302 playback flow follows the `embyreverseproxy` plugin, except that Mi302 needs no real Emby server behind it and implements the Emby API itself; the 115 QR-code login and direct-link flow follows `p115strmhelper`.
- [StrmAssistant](https://github.com/sjtuross/StrmAssistant) (Emby 神醫助手): `X-mediainfo.json` uses its "media info persistence" format, and intro/credits learning follows its playback-behaviour intro detection.
- [xiao-vvv/emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo) (MIT licence): the ffprobe-to-Emby field mapping, the probing rate limits and the 115 circuit-breaker rules are adapted from it; its copyright notice is kept in [`embyserver/mediainfo.py`](https://github.com/MiCat-S/Mi302/blob/main/embyserver/mediainfo.py).
