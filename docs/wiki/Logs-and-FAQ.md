[繁體中文](日誌與常見問題) | [简体中文](日志与常见问题) | **English**

This page explains where to find Mi302's logs, how to use verbose mode, and how to solve common problems during installation and use.

The web admin page and Mi302's log messages are in Traditional Chinese. Where this page quotes them, the original text is given so you can search for it.

## The Logs page

The **Logs** page (日誌) in the web admin page shows records of syncing, scraping, scanning, playback and errors, newest first. Each row has a time, a level, a source (for example 115 同步 for 115 sync, MoviePilot, 掃描 for scanning, 播放 for playback) and the message.

- **Level**: the drop-down offers **All** (全部), **Normal** (一般), **Warnings and errors** (警告和錯誤) and **Errors only** (只看錯誤). The default is Normal. All also shows verbose (debug) records.
- **Search**: type a title, `115`, `MoviePilot` or another keyword; it matches the message and the source.
- **Auto refresh** (自動更新): on by default; new records appear every 2 seconds.
- **How many records**: Mi302 keeps the latest 3000 records in memory, and the page shows at most the newest 1000 of them that match the level and search. The in-memory records are cleared on restart; for older records, use the log file.
- **Download**: **Download log file** (下載日誌檔) downloads the current `mi302.log`. Rotated older files are listed at the bottom of the page after 舊檔： ("old files"); click a file name to download it.

The top of the page shows the full path of the log file. If the log file cannot be written, it shows （無法寫入日誌檔，只保留在記憶體） ("cannot write the log file, kept in memory only"), and the download button gives you the in-memory records instead.

## The log file

The full log is `mi302.log` in the `data/logs/` folder:

| Install method | Log file |
| --- | --- |
| One-line installer (Linux) | `/opt/mi302/config/data/logs/mi302.log` |
| One-line installer (macOS) | `~/Mi302/config/data/logs/mi302.log` |
| Manual Python install | `data/logs/mi302.log` in the folder you run Mi302 from |

- When the file reaches 5 MB it is rotated: older files are renamed `mi302.log.1` through `mi302.log.5`, and 5 old files are kept.
- Each line has the form `time level source: message`, for example `2026-09-27 10:00:00,123 INFO embyserver.strm_sync: …`.
- To follow it in a terminal, run `mi302 logs`. It shows the last 100 lines and then new lines as they arrive (Ctrl+C to exit).

The same lines also go to Mi302's terminal output:

- systemd (one-line installer on Linux): read it with `journalctl -u mi302 -f`.
- launchd (macOS) and background mode without systemd: written to `config/data/logs/console.log`. This file is not rotated.

Mi302 starts writing `mi302.log` only after it has read the config file. Startup errors such as a broken config file therefore only appear in the terminal output.

## Verbose mode

When a player cannot connect or cannot play, turn on **Verbose mode** (詳細模式) at the bottom of the Logs page:

- Mi302 then also logs every player request: method, URL, status code, time taken and the player's User-Agent. URLs that Mi302 does not handle are marked 未實作或找不到 ("not implemented or not found"). Requests from the web admin page itself are not logged.
- Turning it on switches the level drop-down to All.
- The switch is the config setting `server.log_level: debug`. It is saved to `config.yaml`, so it stays on after a restart. Verbose mode produces a lot of records; turn it off once you have found the cause.

You can also set `server.log_level: debug` in `config.yaml` directly; `info` turns it off.

## Hidden credentials

Before anything is written to the log file, the Logs page or the terminal output, the values of these URL parameters are replaced with `***`, case-insensitively: `api_key`, `apikey`, `token`, `X-Emby-Token`, `X-MediaBrowser-Token`, `pw`, `password`. This also applies to URLs printed by third-party libraries.

Logs still contain titles, file paths and IP addresses. Check them before sharing a log publicly.

## FAQ

### Other devices cannot connect

- Run `mi302 status` to check that Mi302 is running and to see the URL it reports.
- Make sure the firewall allows the port, for example:

  ```bash
  sudo ufw allow 8096/tcp
  sudo firewall-cmd --permanent --add-port=8096/tcp && sudo firewall-cmd --reload
  ```

  The one-line installer prints this command when it finds ufw or firewalld active.
- Connect with the machine's LAN IP, not `localhost` or `127.0.0.1`. On a machine with several network interfaces, the IP printed by the installer may not be the one you want.
- `server.host` in `config.yaml` defaults to `0.0.0.0` (accept connections on all interfaces). With `127.0.0.1` only the machine itself can connect. Changing it needs a restart.
- For access from outside your home network, see [Playback](Playback).

### The port is already in use

When the installer finds the port in use, it warns you to stop the other program first or Mi302 will fail to start (先把它關掉，不然 Mi302 會啟動失敗), and asks whether it has been stopped (已經關掉了，繼續安裝？).

- Usually a Mi302 you started by hand is still running. Emby and Jellyfin also use 8096 by default.
- To see what is using the port: `sudo ss -ltnp | grep 8096` on Linux, `lsof -nP -iTCP:8096 -sTCP:LISTEN` on macOS.
- Stop that program or pick another port: run the installer again with `--port 8097`, or change `server.port` in `config.yaml` and run `mi302 restart`.

### Mi302 does not respond or does not start after installing

The installer waits up to 60 seconds. If Mi302 has not responded by then, it says so and suggests `mi302 logs` (Mi302 還沒有回應；看日誌找原因：mi302 logs).

1. Run `mi302 logs` to read the log file.
2. If the log file is empty or missing, read the terminal output: `journalctl -u mi302 -n 50` on Linux, `config/data/logs/console.log` on macOS or without systemd.
3. Common causes are a broken config file (the message contains 設定檔 … 格式錯誤, "config file … format error") and a port that is already in use. If you broke the config file, fix it or restore `config.yaml.bak`, then run `mi302 restart`.

### A network drive is mounted after boot

NFS or SMB shares from a NAS, or Parallels folders such as `/media/psf/...`, may be mounted only after Mi302 has started.

If a library folder is missing, unreadable or empty, Mi302 keeps the existing items and watch history instead of treating every video as deleted. The log shows 媒體庫路徑不存在或是空的：…（沒掛載好？）；保留原本的 N 個項目 ("library path missing or empty … not mounted? keeping the existing N items").

Once the drive is mounted, click **Rescan all** (全部重新掃描) on the **Libraries** page (媒體庫). The systemd service created by the installer waits for the network and `remote-fs.target` before starting, so network drives listed in `/etc/fstab` are usually mounted in time.

### .strm files cannot be written, or videos are not found by the scan

This usually means the account running Mi302 has no permission on the media folder.

- On Linux, it is the user chosen at install time, stored as `MI302_USER` in `.env` in the install folder. On macOS it is your own account.
- Test reading and writing as that account:

  ```bash
  sudo -u USER ls /media/folder
  sudo -u USER touch /media/folder/test && sudo -u USER rm /media/folder/test
  ```

- To fix it, run the installer again with `--user` set to an account that has access, or give this account permission on the folder.
- If you moved from the old Docker version, existing `.strm` and `.nfo` files may belong to root; see "Moving from the old Docker version" in [Installation](Installation).

### Networks in mainland China

- **pip**: when PyPI cannot be reached, the one-line installer switches to the Tsinghua mirror automatically and remembers it for later updates. You can also force it with `--mirror`. For a manual install, add `-i https://pypi.tuna.tsinghua.edu.cn/simple` to the pip command.
- **GitHub**: if `raw.githubusercontent.com` cannot be reached, open the project on GitHub, click Code → Download ZIP, unpack it and run `sudo bash install.sh` inside the unpacked folder (without `sudo` on macOS). The unpacked folder becomes the install folder, so move it where you want Mi302 to live before running the script. `mi302 update` still needs access to GitHub.

### Forgotten administrator password

Installed with the one-line installer:

```bash
mi302 reset-password admin NEW_PASSWORD
```

On macOS without the `mi302` command, use `bash ~/Mi302/install.sh reset-password admin NEW_PASSWORD`.

Installed manually: run this in the folder that holds `data/` (`Mi302/` in the install example):

```bash
.venv/bin/python -m embyserver -c config.yaml --reset-password admin NEW_PASSWORD
```

- On success it prints 已重設 admin 的密碼 ("password of admin has been reset"). You do not need to stop Mi302 first.
- If the account does not exist, a new administrator is created with this name and password.

### A player cannot play

1. On the **115 Cloud** page (115 網盤), check that 115 is still logged in and has not been paused by rate limiting or an expired login. See [115 Cloud Sync](115-Cloud-Sync).
2. Search the Logs page for the title:
   - A line like 播放 … 302 到 … ("play … 302 to …"): Mi302 handed the 115 direct link to the player. The problem is between the player and 115.
   - A line like 播放 … 失敗 ("play … failed"): getting the direct link from 115 failed; the message gives the reason.
   - Nothing: the play request never got that far. Turn on verbose mode, play again and look at the status codes of the player's requests.
3. The stream URL returns 401: the player did not send its login. **Playback URLs require login** (播放網址要求登入) under **Advanced settings** (進階設定) is on by default; turn it off only if a player cannot play because of it.
4. Downloads return 403: **Allow players to download videos** (允許播放器下載影片) under Advanced settings is turned off.

How playback works, the login requirement and remote access are covered in [Playback](Playback).
