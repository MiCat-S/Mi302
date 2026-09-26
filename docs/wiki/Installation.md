[繁體中文](安裝) | [简体中文](安装) | **English**

This page covers installing Mi302: the one-line installer and its options, special cases, the `mi302` management command, running it manually with Python, updating and uninstalling.

The installer's messages and the web admin page are in Traditional Chinese. Where this page quotes them, the original text is given so you can match it on screen.

## Choosing an install method

| Method | Good for | Start at boot |
| --- | --- | --- |
| One-line installer (recommended) | Linux, macOS | Set up for you |
| Manual Python install | Controlling every step yourself, or Windows | Set it up yourself |

Mi302 needs Python 3.10 or newer; the installer installs it if it cannot find one. There is no Docker version of Mi302. If you used the old Docker version, see "Moving from the old Docker version" below.

## One-line installer

On the Linux machine that will run Mi302 (on macOS, leave out `sudo`; see "macOS" below):

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash
```

The script does the following, in order:

1. Downloads Mi302 to the install folder (on Linux, `/opt/mi302` by default). It uses git if present; if git is missing it tries to install it with the system package manager, and falls back to downloading a tarball.
2. Asks two questions:
   - **Which user runs Mi302**: defaults to the account that ran `sudo`. This account must be able to read and write your media folders; the `.strm` files created by 115 sync are written as this user.
   - **Port**: defaults to 8096. Not asked if a config file already exists; the port from the config file is kept.
3. Installs Python if no 3.10+ is found, creates a virtual environment in `.venv`, installs the Python dependencies, and installs ffmpeg. ffmpeg is used for [media info probing](Media-Info); if it cannot be installed you get a warning and everything else still works.
4. Sets up start at boot (the systemd service `mi302.service`), starts Mi302, and waits up to 60 seconds for the web page to respond.
5. Prints the URLs and the management commands.

System packages are installed with whichever of apt-get, dnf, yum, apk, pacman or zypper the machine has. Once the questions are answered you can walk away; the only later prompt appears if the port is already taken by another program.

The final output looks like this (the IP is the first LAN address the script finds):

```
==> Mi302 已經在執行  版本 64a30e6（2026-09-27 10:00）

  管理網頁： http://192.168.1.20:8096/web
  播放器：   新增 Emby 伺服器，位址 http://192.168.1.20:8096
  設定檔：   /opt/mi302/config/config.yaml
  資料和日誌：/opt/mi302/config/data/

  管理指令： mi302 status | logs | restart | update | reset-password 帳號 密碼 | uninstall
```

The lines are: the web admin page, the address to add in players, the config file, the data and log folder, and the management commands. Open the web admin page URL and continue with [First Setup](First-Setup). If a ufw or firewalld firewall is active, the script also prints the command that opens the port.

### Options

| Option | Meaning |
| --- | --- |
| `--user NAME` | User that runs Mi302 (Linux only). It needs read and write access to your media folders. Default: the account that ran `sudo` |
| `--port PORT` | Port for the web page and players, 1–65535. Default 8096 |
| `--dir FOLDER` | Install folder. Default `/opt/mi302` on Linux, `~/Mi302` on macOS. When you run `install.sh` from inside a Mi302 program folder, it installs into that folder |
| `--mirror` | Use the Tsinghua PyPI mirror for pip. Switched on automatically when PyPI cannot be reached |
| `--branch BRANCH` | Branch to install from. Default `main` |
| `-y`, `--yes` | Do not ask; use defaults for anything not given |
| `-h`, `--help` | Show help |

To skip the questions, give all options at once:

```bash
# run as user cat on port 8097, defaults for everything else
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash -s -- --user cat --port 8097 -y
```

The user, branch and pip mirror are stored in `.env` in the install folder; the port is stored as `server.port` in `config.yaml`. Later runs and updates reuse them, so you only pass the options you want to change.

### Installed files

```
/opt/mi302/                 ~/Mi302 on macOS
  config/                   settings and data, kept on update
    config.yaml             config file (settings from the web page are written here too)
    config.yaml.bak         previous version of the config file
    data/
      library.db            database: accounts, watch history, 115 login state, ...
      backups/              daily automatic backups
      images/               uploaded images (for example library covers)
      logs/mi302.log        log file
  .env                      options chosen at install time
  .venv/                    Python virtual environment
  install.sh, embyserver/…  program
```

How the config file works is described in [First Setup](First-Setup); backups in [Backup and Restore](Backup-and-Restore).

## Special cases

### macOS

Do not use `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | bash
```

- If you run it with `sudo`, the script stops.
- The install folder is `~/Mi302`. Mi302 runs as your own account; the script does not ask which user to use.
- If Python has to be installed, the script uses Homebrew to install `python@3.12`; ffmpeg is also installed with Homebrew. If Python is needed and Homebrew is missing, the script stops: install [Homebrew](https://brew.sh) first, or install Python 3.12 from python.org.
- Start at login is handled by launchd (`~/Library/LaunchAgents/com.mi302.server.plist`). Mi302 starts after you log in to macOS and is restarted if it exits abnormally.
- Terminal output goes to `~/Mi302/config/data/logs/console.log`.
- The `mi302` command is written to `/usr/local/bin`. If that folder does not exist, or your account cannot write to it (for example because it is owned by root), the script silently skips the command. The only sign is that the management commands line (管理指令) at the end shows `bash /Users/<you>/Mi302/install.sh status | logs | …` instead of `mi302 …`. In that case replace `mi302` with `bash ~/Mi302/install.sh` everywhere on this page:

  ```bash
  bash ~/Mi302/install.sh status
  bash ~/Mi302/install.sh reset-password admin NEW_PASSWORD
  ```

  Always give a subcommand. Without one, the script runs a reinstall, which updates Mi302 and restarts it.

### You already cloned and ran Mi302 yourself

Run this inside that folder (without `sudo` on macOS):

```bash
sudo bash install.sh
```

- The script installs in place. Your existing `config.yaml` and `data/` stay where they are and keep being used; they are not moved into `config/`.
- Stop the Mi302 you started by hand first, or the port will be taken. If the script finds the port in use, it warns you and asks whether it has been stopped (已經關掉了，繼續安裝？).
- The code is updated to the latest `main`. Files you changed in the program folder are first saved as `local-changes-YYYYMMDD-HHMMSS.patch`, then reset to the latest version.

### Moving from the old Docker version

Docker is no longer supported, but the settings and data in `config/` carry over unchanged:

1. Note the values of `MI302_PORT` (the host port) and `MI302_MEDIA` (the media folder) in `.env` in the old folder. `.env` is rewritten by the new installer.
2. Run `docker compose down` in the old folder to stop the container. Keep the `config/` folder.
3. Run the new installer with the one-line command and point `--dir` at the old folder. If the old host port was not 8096, pass it with `--port`:

   ```bash
   # old folder is /opt/mi302, old host port was 8097
   curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash -s -- --dir /opt/mi302 --port 8097
   ```

   Do not run the `install.sh` inside the old folder: that is the old script, which still handles the install the Docker way. The new script reports that it found an old Docker install and is switching to plain Python (偵測到舊的 Docker 安裝，改成直接用 Python 執行), then installs as usual.
4. Inside the container the media folder was `/media`; Mi302 now sees the host's real paths. If library folders or the local folders of sync tasks still point to `/media/...`, change them in the web admin page to the host paths (under `MI302_MEDIA`). The same applies to MoviePilot path mappings.
5. The container wrote files as root, so existing `.strm` and `.nfo` files in your media folders may belong to root. If the Mi302 user cannot change them, fix the owner with `sudo chown -R USER MEDIA_FOLDER`.

The old `--docker` and `--media` options are gone; passing them is an error.

### Machines without systemd (for example WSL)

Without systemd, the script runs Mi302 as a background process and reminds you to run `mi302 start` yourself after every reboot.

- The process ID is stored in `config/mi302.pid`; `mi302 stop` and `mi302 restart` use it to find Mi302.
- Terminal output goes to `config/data/logs/console.log`.
- On WSL with systemd enabled, Mi302 is set up as a systemd service like on any other Linux.

## The mi302 command

After installing, manage Mi302 with `mi302`:

| Command | What it does |
| --- | --- |
| `mi302` or `mi302 status` | Whether Mi302 is running, the web admin URL, the version |
| `mi302 logs` | Show the last 100 lines of the log file and follow new lines (Ctrl+C to exit) |
| `mi302 start`, `mi302 stop`, `mi302 restart` | Start, stop, restart |
| `mi302 update` | Update to the latest version and restart; settings and data are not touched |
| `mi302 reset-password USER NEW_PASSWORD` | Reset a forgotten password; if the account does not exist, it is created as an administrator |
| `mi302 uninstall` | Remove start at boot and the `mi302` command; program, settings and data stay |

- `mi302` is `/usr/local/bin/mi302`, which runs the `install.sh` in the install folder. So `mi302 status` is the same as `sudo bash /opt/mi302/install.sh status`.
- On Linux every command needs root; `mi302` re-runs itself with `sudo`, so you may be asked for your password.
- On Linux the service is the systemd unit `mi302.service`, so `systemctl status mi302` and `journalctl -u mi302 -f` also work. On macOS it is the launchd job `com.mi302.server`.

### Changing the port or the user

Run the installer again with the new option. Everything already installed is reused, and Mi302 is updated along the way:

```bash
sudo bash /opt/mi302/install.sh --port 8097
sudo bash /opt/mi302/install.sh --user cat
```

To change only the port, you can also edit `server.port` in `config.yaml` and run `mi302 restart`. After changing the port, update the server address in your players and your firewall rules.

## Manual install with Python

You need Python 3.10 or newer (check with `python3 --version`). On Debian and Ubuntu, if the venv module is missing, run `sudo apt install python3-venv` first.

```bash
git clone https://github.com/MiCat-S/Mi302.git
cd Mi302
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m embyserver
```

Then open `http://<host>:8096/web` and continue with [First Setup](First-Setup).

- `config.yaml` and the `data/` folder are created in **the folder you run the command from**, which is `Mi302/` in the example. The default `data_dir` of `./data` is relative to that folder, not to the config file.
- Media info probing needs ffprobe. Install ffmpeg yourself, for example `sudo apt install ffmpeg` or `brew install ffmpeg`.
- To change the port, edit `server.port` in `config.yaml` and restart.
- If pip is slow from mainland China, add `-i https://pypi.tuna.tsinghua.edu.cn/simple`.
- Windows: replace `.venv/bin/` with `.venv\Scripts\` in the commands. The one-line installer does not support Windows.

### Command-line options

| Option | What it does |
| --- | --- |
| `-c PATH`, `--config PATH` | Config file to use. Without it, the environment variable `EMBYSERVER_CONFIG` is used, and failing that `config.yaml` in the current folder |
| `--reset-password USER NEW_PASSWORD` | Reset the password and exit. If the account does not exist, it is created as an administrator |
| `--scan` | Scan all libraries and exit |
| `--sync-115` | Run a full 115 sync of the `.strm` files, then scan and send to scraping according to your settings, and exit. `--sync-115 incremental` runs an incremental sync instead |

Without `--reset-password`, `--scan` or `--sync-115`, the server starts.

### Start at boot with systemd

Replace `cat` and the paths with yours and save this as `/etc/systemd/system/mi302.service`. `WorkingDirectory` must be the folder that holds `data/`:

```ini
[Unit]
Description=Mi302
Wants=network-online.target
After=network-online.target remote-fs.target

[Service]
User=cat
WorkingDirectory=/home/cat/Mi302
ExecStart=/home/cat/Mi302/.venv/bin/python -m embyserver -c /home/cat/Mi302/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mi302
```

Alternatively, run `sudo bash install.sh` inside the program folder to hand it over to the installer (see "You already cloned and ran Mi302 yourself" above).

## Updating

Installed with the one-line installer:

```bash
mi302 update
```

This downloads the latest version (with git; installs unpacked from a ZIP download a tarball instead), updates the dependencies, rewrites the start-at-boot configuration, restarts Mi302 and prints the URLs. Settings and data in `config/` are not touched. Files you changed in the program folder are saved as `local-changes-*.patch` and then reset to the latest version. Updating needs access to GitHub. Running the one-line install command again also updates.

Installed manually:

```bash
cd Mi302
git pull
.venv/bin/pip install -r requirements.txt
```

Then restart Mi302. With the systemd unit above, run `sudo systemctl restart mi302`.

## Uninstalling

```bash
mi302 uninstall
```

After you confirm, it stops Mi302 and removes start at boot (the systemd service, the launchd job or the background process) and the `mi302` command. The program, settings and data stay, so running the installer again brings everything back.

To remove everything, delete the install folder as well. This deletes settings, database, backups and logs; [back up](Backup-and-Restore) first if you may want them later:

```bash
sudo rm -rf /opt/mi302    # macOS: rm -rf ~/Mi302
```

The `.strm` files and scraped metadata in your media folders are not inside the install folder and are not deleted.

Installed manually: stop Mi302. If you set up systemd, run `sudo systemctl disable --now mi302` and delete `/etc/systemd/system/mi302.service`. Then delete the `Mi302` folder.

## Remote access

To watch away from home, put a reverse proxy in front of Mi302 and set the public address; see [Playback](Playback).

## Problems

For devices that cannot connect, port conflicts, permissions, network problems in mainland China, forgotten passwords and more, see [Logs and FAQ](Logs-and-FAQ).
