# Mi302

[繁體中文](README.md) | [简体中文](README.zh-CN.md) | **English**

Mi302 is a video server with an Emby-compatible API, built for videos stored on 115 Cloud (115 網盤, a Chinese cloud drive). It syncs 115 folders into local `.strm` files, and players sign in to it as if it were an Emby server. When you press play, Mi302 answers with an HTTP 302 redirect to a 115 direct link, so the video never passes through the server.

You do not need a real Emby server behind it, or any other 115 tool. The full documentation is in the [Wiki](https://github.com/MiCat-S/Mi302/wiki). The web admin page is in Traditional Chinese; the wiki pages explain every screen in English.

## Features

- **115 sync**: sign in to 115 by scanning a QR code and sync folders into strm files. After that, incremental syncs read 115's activity log (its "life events"), and a weekly full sync catches anything missed.
- **Emby compatible**: Infuse, VidHub, SenPlayer, the official Emby apps and other Emby clients just add the server and sign in.
- **302 direct play**: Mi302 fetches the 115 direct link with the player's own User-Agent and redirects the player to it. No proxying, no transcoding.
- **Scraping by MoviePilot**: newly synced videos are sent to [MoviePilot](https://github.com/jxxghp/MoviePilot) for metadata and artwork. When a series is missing episodes, MoviePilot can subscribe and download them. Series with wrong episode numbers, or any folder that looks wrong while browsing 115 in the web page, can be reorganised by MoviePilot on 115, with a preview first.
- **Media info**: reads `X-mediainfo.json` files (the StrmAssistant format) and can probe videos with ffprobe, so players show 4K, HDR, audio tracks and subtitle tracks.
- **Intro and credits skipping**: learns intros and credits from how people watch, so players such as SenPlayer offer "Skip intro".
- **Made for Chinese libraries**: Chinese titles sort by pinyin; search matches full pinyin, initials, Simplified and Traditional characters; cast names and genres can be shown in Chinese.
- **Web admin**: everything is set up at `/web` and kept in sync with `config.yaml`. The database is backed up daily.
- **Removes duplicate files on 115**: finds identical videos and different versions of the same title (comparing resolution, HDR and audio) and, after you confirm, moves the extra copies to the 115 recycle bin; local strm files and watch history are handled too.
- **115 offline download**: hand magnet, ed2k and http links to 115's cloud download; anything saved into a sync folder reaches the library with the next sync.
- **WebDAV**: Infuse, VidHub and Kodi can also browse 115 directly over read-only WebDAV, with the same 302 direct play.
- **Protects your 115 account**: when 115 rate-limits or the login expires, a circuit breaker pauses syncing and probing. Playback keeps working.

## How it works

```mermaid
flowchart LR
    Cloud[115 Cloud] -- sync --> Strm[local .strm files]
    Strm -- scan --> Mi302[Mi302]
    Player[Player] -- Emby API --> Mi302
    Mi302 -- 302 to 115 direct link --> Player
    Player -- streams the video --> Cloud
```

## Quick start

On Linux, run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash
```

On macOS, run it without sudo:

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | bash
```

The installer sets up Python, ffmpeg and the dependencies, starts Mi302 at boot, and prints the address of the web admin page. For Windows, or to do every step yourself, see the manual install in [Installation](https://github.com/MiCat-S/Mi302/wiki/Installation).

Then open `http://<host>:8096/web` in a browser:

1. Create the admin account.
2. Add a library and pick a folder on the server. To turn each subfolder into its own library, use **Bulk add from a folder** (從資料夾批量新增).
3. On the 115 tab (115 網盤), scan the QR code with the 115 app, add a sync task, and press **Incremental sync** (增量同步). The first run does a full sync automatically.
4. If you use MoviePilot, enter its URL and API token on the MoviePilot tab.
5. In your player, add an Emby server at `http://<host>:8096` and sign in with the account you created.

[First Setup](https://github.com/MiCat-S/Mi302/wiki/First-Setup) walks through each step.

## Documentation

| Page | What it covers |
| --- | --- |
| [Installation](https://github.com/MiCat-S/Mi302/wiki/Installation) | Installer, manual install, the `mi302` command, updating and uninstalling |
| [First Setup](https://github.com/MiCat-S/Mi302/wiki/First-Setup) | Setting up in the web page, users, how the config file works |
| [115 Cloud Sync](https://github.com/MiCat-S/Mi302/wiki/115-Cloud-Sync) | Signing in, sync tasks, incremental and full sync, duplicate files, the circuit breaker |
| [Library and Scanning](https://github.com/MiCat-S/Mi302/wiki/Library-and-Scanning) | Folder layout, partial scans, pinyin sort and search, Chinese names |
| [Playback](https://github.com/MiCat-S/Mi302/wiki/Playback) | The 302 flow, playback authentication, downloads, reverse proxies |
| [Intro and Credits](https://github.com/MiCat-S/Mi302/wiki/Intro-and-Credits) | How intros and credits are learned, what players receive, how to test |
| [Media Info](https://github.com/MiCat-S/Mi302/wiki/Media-Info) | `X-mediainfo.json`, ffprobe probing, 115 rate limits |
| [MoviePilot](https://github.com/MiCat-S/Mi302/wiki/MoviePilot) | Scraping, filling missing episodes, Mi302 as an Emby server, library covers |
| [Backup and Restore](https://github.com/MiCat-S/Mi302/wiki/Backup-and-Restore) | Daily backups, downloading a backup, restoring |
| [Configuration Reference](https://github.com/MiCat-S/Mi302/wiki/Configuration-Reference) | Every `config.yaml` key with its default |
| [Logs and FAQ](https://github.com/MiCat-S/Mi302/wiki/Logs-and-FAQ) | Logs, connection problems, permissions, networks in mainland China, lost passwords |
| [Technical Details](https://github.com/MiCat-S/Mi302/wiki/Technical-Details) | Request flow, implemented Emby endpoints, code layout, tests |

## Everyday commands

After a one-line install, manage Mi302 with `mi302`:

| Command | What it does |
| --- | --- |
| `mi302 status` | Whether it is running, its address and version |
| `mi302 logs` | Follow the log |
| `mi302 restart` | Restart |
| `mi302 update` | Update to the latest version; settings and data are kept |
| `mi302 reset-password admin <new password>` | Reset a forgotten password |

Checking for updates, updating and restarting also work from the version card and **Restart** (重新啟動) at the bottom of the left sidebar; when a new version is out, every page shows a notice.

## License

Mi302 is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md). **Commercial use is not allowed.**

- Allowed: personal and household use; study, research and testing; use by noncommercial organisations. You may modify the code and share it, as long as you pass on these terms and the `Required Notice` line at the top of `LICENSE.md`.
- Not allowed: any commercial use, such as selling Mi302 or a modified version, offering it as a paid product or service, bundling it with paid software or devices, or using it for profit in a company. For a commercial license, contact the author on [GitHub](https://github.com/MiCat-S/Mi302/issues) first.
- This is a source-available license, not an OSI-approved open-source license.
- Third-party packages keep their own licenses, and code adapted from other projects keeps its original copyright notice (see Credits below).

If Mi302 is useful to you, please give it a ⭐ Star on [GitHub](https://github.com/MiCat-S/Mi302) so more people can find it.

## Credits

- The 302 playback flow follows the `embyreverseproxy` plugin in [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins).
- Media info files are compatible with [StrmAssistant](https://github.com/sjtuross/StrmAssistant). The ffprobe-to-Emby field mapping is adapted from [xiao-vvv/emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo) under the MIT license; its copyright notice is kept in `embyserver/mediainfo.py`.
- Scraping and subscriptions are handled by [MoviePilot](https://github.com/jxxghp/MoviePilot).
- Pinyin comes from [pypinyin](https://github.com/mozillazg/python-pinyin); Simplified and Traditional conversion from [zhconv](https://github.com/gumblex/zhconv).

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest
```

See [Technical Details](https://github.com/MiCat-S/Mi302/wiki/Technical-Details) for the code layout and tests. The wiki sources live in [`docs/wiki`](docs/wiki); edit them there and publish with `docs/publish-wiki.sh`.
