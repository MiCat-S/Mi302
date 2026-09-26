[繁體中文](播放與外網連線) | [简体中文](播放与外网访问) | **English**

This page explains what happens when a player presses play, which strm contents Mi302 understands, the playback authentication and download switches, and how to reach Mi302 from outside your network. The web admin page is in Traditional Chinese; setting names are given in English with the original label in parentheses.

## What happens when you play something

1. The player first calls `PlaybackInfo`. Mi302 returns one media source:
   - For a 115 strm (with 115 signed in): `Path` is `{address the player connected to}/videos/{id}/stream.{container}?Static=true` and `DirectStreamUrl` is `/videos/{id}/stream.{container}?Static=true&MediaSourceId=…&api_key={token}`.
   - Only direct play is offered (`SupportsTranscoding` is false). Mi302 never transcodes, so the player must handle the format itself.
   - If media info is available it includes resolution, audio tracks and subtitle tracks. If not, and **Probe when a video is opened** (打開影片時自動探測) is on (the default), the item is queued for background probing, see [Media Info](Media-Info).
2. The player requests `/Videos/{id}/stream…`. Mi302 reads the strm, applies the path rules and extracts the pickcode (the 17-character code that identifies a file on 115 Cloud (115 網盤)). It asks 115 for a download link using **the player's own User-Agent** and answers `302 Location: <115 direct link>`.
3. The player reads the video straight from 115. Video traffic does not pass through Mi302.

`Path` points back to Mi302 rather than to 115 because some players play `Path` directly when the source is http. Pointing it at Mi302 guarantees the request goes through Mi302 with the player's User-Agent.

A library can also hold regular video files instead of strm files. Mi302 serves those itself, with Range support (so seeking works), and that traffic does pass through Mi302.

## 115 direct links are tied to the User-Agent

A 115 direct link only works with the User-Agent that requested it. That is why Mi302 requests it with the player's User-Agent and redirects the player, instead of downloading on the player's behalf.

- Links are cached per (pickcode, User-Agent) until 5 minutes before the expiry time in the link, or for 10 minutes if no expiry can be read.
- Concurrent requests for the same file and User-Agent share a single request to 115.
- If the 115 open platform is authorised, it is tried first and QR-code login is the fallback, see [115 Cloud Sync](115-Cloud-Sync).
- If 115 fails: when the strm points back to Mi302 itself (same host as the address the player used, or not a URL at all), the player gets a 502 with the reason; when it points to another host, Mi302 redirects to the URL in the strm instead.

## strm contents Mi302 understands

Mi302 uses the first line of the strm that is not empty and does not start with `#`.

| strm content | Usually from | What Mi302 does |
| --- | --- | --- |
| `http://host:8096/d/{pickcode}.mkv` (optionally followed by `?/original-name`) | Mi302's own sync | Gets a 115 direct link |
| `…/d/{pickcode}` | Other tools, such as 115-station | Gets a 115 direct link |
| `…?pickcode={pickcode}` or `…?pick_code={pickcode}` | P115StrmHelper and similar | Gets a 115 direct link |
| `115://{pickcode}` | | Gets a 115 direct link |
| Any other `http://` or `https://` URL | alist and similar | Redirects to that URL |
| A local path, such as `/mnt/media/a.mkv` | | Serves the file itself |

- When a pickcode is found and 115 is signed in, the host in the URL does not matter. strm files made by other tools work without regenerating them.
- Mi302 also answers the URLs that other tools' strm files point to: `/d/{pickcode}`, `/p115/redirect?pickcode=…` and `/api/v1/plugin/p115strmhelper/redirect_url?pickcode=…`. Pointing those strm files' host at Mi302 works too. These URLs do not check login.
- The container comes from the URL's extension, then from a `file_name=` parameter, and otherwise from **Default container when the format is unknown** (看不出格式時預設的影片格式, default `mkv`).

## Playback authentication

The **Other tools' strm** card (其他工具產生的 strm) on the **Advanced settings** tab (進階設定) has **Require login for playback URLs** (播放網址要求登入, `redirect.require_auth`, on by default).

When it is on, `/Videos/{id}/…` only answers with a 302 if a valid token is sent, just like official Emby (since 4.7 Emby blocks unauthenticated stream requests on the LAN as well). The token can be sent as:

- The query parameter `api_key` (also `ApiKey` or `X-Emby-Token`). The `DirectStreamUrl` returned by `PlaybackInfo` already includes it.
- The header `X-Emby-Token` or `X-MediaBrowser-Token`, or `Token="…"` inside `X-Emby-Authorization`.

The Emby apps and Kodi put the token in the query string; Infuse sends it in the `X-Emby-Authorization` header. Both work.

When it is off, anyone who knows an item id can get the 115 direct link without logging in. Only turn it off if a player cannot play because of it: first enable **Verbose mode** (詳細模式) on the **Logs** tab (日誌) and check whether `/Videos/…` returns 401 during playback, see [Logs and FAQ](Logs-and-FAQ).

`PlaybackInfo` and downloads are not affected by this switch; they always require login.

## Downloads

The **Server** card (伺服器) on the **Advanced settings** tab has **Allow players to download videos** (允許播放器下載影片, `server.allow_download`, on by default).

- The download URLs `/Items/{id}/Download` and `/Items/{id}/File` always require login. Downloading a strm item is the same 302 to 115, so the player downloads straight from 115.
- When the switch is off, items report `CanDownload: false` and the user policy reports `EnableContentDownloading: false`, so players hide their download button. Calling a download URL directly returns 403 "下載功能已關閉" (downloads are disabled).
- The switch only covers the download URLs; the playback URLs keep working.

## strm files from other tools

The other settings in the **Other tools' strm** card only matter when the strm contents were not written by Mi302 (for example alist URLs or local paths). Click **Save settings** (儲存設定) at the bottom of the page after changing them.

**Path rules** (路徑替換, `redirect.path_rules`): one rule per line, written as `old prefix => new prefix`. The strm content is rewritten before anything else happens.

- Local paths: the path prefix is compared by whole path segments, so `/mnt/115` matches `/mnt/115/…` but not `/mnt/1150`.
- A rule whose old prefix is a full URL (with `http://`) is compared against the whole URL, which lets you change hosts.
- A rule whose old prefix is a path is also compared against the path part of a URL. On a match the whole URL becomes the new prefix plus the rest of the path (the query string is kept), so the new prefix must be a full URL in that case.
- For URLs, full-URL rules are checked before path rules; within each kind, the first matching rule from the top wins.

```
/mnt/115 => http://alist:5244/d/115
http://192.168.1.5:3000 => http://192.168.1.8:3000
```

The first rule turns `/mnt/115/電影/a.mkv` into `http://alist:5244/d/115/電影/a.mkv`, which is then used for the 302.

**Resolve upstream redirects on the server** (由伺服器先跟著上游的重導向走到底, `redirect.resolve_redirects`, off by default): Mi302 sends a HEAD request with the player's User-Agent, follows every redirect and gives the final URL to the player.

- Results are cached per (item, User-Agent) for `redirect.cache_ttl` seconds (default 90); each resolution waits at most `redirect.resolve_timeout` seconds (default 10). Both can only be set in the config file, and `resolve_timeout` needs a restart.
- On failure the original URL is used.
- strm files with a pickcode get their 115 direct link directly and skip this step.

**Default container when the format is unknown** (`redirect.default_container`, default `mkv`) tells the player which format to expect when the strm URL has no extension.

## Progress and watched state

Players report progress through `/Sessions/Playing`, `/Sessions/Playing/Progress` and `/Sessions/Playing/Stopped` (and the older `/Users/{id}/PlayingItems/{id}`).

- The resume position is stored per user.
- Stopping at 90% of the runtime or later marks the item watched, adds one to the play count and clears the resume position. The runtime comes from the nfo, the media info, or the runtime the player reports.
- A report without a position only updates the last-played time and leaves the resume position alone.
- The same reports are used to learn intros and credits, see [Intro and Credits](Intro-and-Credits).

## Remote access

Video goes straight from 115 to the player. Mi302 and the reverse proxy only handle API requests and small 302 responses, so your home upload bandwidth barely matters. The exceptions are regular video files in a library and strm files that contain local paths; those pass through Mi302.

Mi302 itself only speaks HTTP. For HTTPS from outside, put a reverse proxy such as Caddy or Nginx in front of it:

- Use a dedicated domain or subdomain and forward the whole site to Mi302. Under a sub-path (for example `/mi302/`), the playback URLs Mi302 returns will not contain that sub-path.
- Pass the original `Host` header and `X-Forwarded-Proto`. Mi302 honours `X-Forwarded-Proto` and `X-Forwarded-For` and builds the playback URL in `PlaybackInfo` from the address the player connected to; with a wrong `Host`, players get a wrong address.
- The WebSocket at `/embywebsocket` only answers KeepAlive messages. Forwarding it is optional; the example below does.
- If MoviePilot's cover plugin uploads through the proxy, allow request bodies of 30 MB.

Caddy (passes `Host` and `X-Forwarded-Proto` by default and obtains certificates automatically):

```
emby.example.com {
    reverse_proxy 127.0.0.1:8096
}
```

Nginx:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name emby.example.com;
    ssl_certificate     /etc/ssl/emby.example.com.crt;
    ssl_certificate_key /etc/ssl/emby.example.com.key;
    client_max_body_size 30m;

    location / {
        proxy_pass http://127.0.0.1:8096;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

In the player, use `https://emby.example.com` as the server address.

### Server URL inside strm files

The **115 and strm** card (115 與 strm) on the **Advanced settings** tab has **Server URL in strm files** (strm 裡的伺服器網址, `p115.strm.base_url`). Left empty, Mi302 uses the address the admin most recently used to open the web admin page.

When Mi302 plays a strm itself, it only extracts the pickcode and ignores the host, so remote playback works without changing this setting. You only need to set your public address when other programs read these strm files directly (for example another Emby server, or a player that opens the strm files over SMB) and must reach Mi302 from outside.

After changing it, run a **Full sync** (全量同步) once. A full sync rewrites every strm whose content changed; an incremental sync only touches files that changed on 115.

### Security

- Keep **Require login for playback URLs** on and use strong passwords.
- Consider turning off **List user names on the player login screen** (播放器登入畫面列出使用者名稱, `server.public_users`) in the **Server** card, so the login screen does not list accounts.
- The web admin page `/web` is only available to admin accounts, and API keys cannot call its API.
- The strm short links `/d/{pickcode}` do not check login, but they require knowing the 17-character pickcode.

## Players

Infuse, VidHub, SenPlayer, the official Emby apps and other players that support Emby can log in and play directly. Add an Emby server in the player with the address `http://<host>:8096` (or your reverse proxy's HTTPS address) and log in with a Mi302 account.

If a player cannot connect or play, see [Logs and FAQ](Logs-and-FAQ).
