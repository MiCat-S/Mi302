[繁體中文](備份與還原) | [简体中文](备份与还原) | **English**

Mi302 backs up its database and config file every day. This page covers what is backed up, where it goes, how to back up and download by hand, and how to restore. The web admin page is only in Traditional Chinese; labels below are translated, with the original in parentheses.

## What is backed up

Each backup is a pair of files with the same timestamp:

| File | Contents |
| --- | --- |
| `mi302-YYYYMMDD-HHMMSS.db` | a full copy of the database `library.db` |
| `mi302-YYYYMMDD-HHMMSS.yaml` | a copy of the config file `config.yaml` |

For example `mi302-20260927-031500.db` and `mi302-20260927-031500.yaml`.

The database holds:

- User accounts, password hashes, and players' login sessions.
- Watch history, playback positions and favorites.
- The 115 login: the cookie and the 115 open-platform token.
- API keys created in the web admin page.
- The 115 sync index and progress.
- Media info, cast and crew with their Chinese names, and learned intros and credits.
- Library items, and the server ID that players use to recognize this server.

Not included:

- Uploaded library covers (`data/images/`).
- Logs (`data/logs/`).
- Anything in your media folders: strm files, nfo files, posters, subtitles, `X-mediainfo.json`. A full sync can recreate the strm files; back up scraped nfo files and posters yourself if you need them.

## Where backups are stored

Backups go into `backups` inside the data folder (`server.data_dir`):

| Installation | Location |
| --- | --- |
| One-line installer (Linux) | `/opt/mi302/config/data/backups` |
| One-line installer (macOS) | `~/Mi302/config/data/backups` |
| Manual Python install | `data/backups` in the folder you start Mi302 from |

The **Backup** card (備份) under **Advanced settings** (進階設定) shows the actual location.

## Automatic backups

- Mi302 checks once at startup and then every hour. If 24 hours have passed since the last backup, it makes one. So the first backup appears right after the first start.
- Mi302 keeps running during a backup. It uses SQLite's online backup, which includes changes not yet written to the main database file.
- A backup that lacks required tables is not saved, so a broken copy never pushes out good older ones.
- If an automatic backup fails, the log records "automatic backup failed" (自動備份失敗) and the reason.

### How many to keep

**Keep this many** (保留幾份, `server.backup_keep`) on the Backup card defaults to 7 and accepts 0 to 90. Click **Save settings** (儲存設定) at the bottom of the page after changing it.

- After each backup, only the newest ones are kept; older `.db` and `.yaml` files are deleted together. If you lower the number, the extra ones go at the next backup.
- 0 turns automatic backups off. You can still back up by hand; manual backups keep the newest 7.

## Manual backup and download

On the Backup card under Advanced settings:

- **Back up now** (立即備份) makes a backup immediately and shows its file name.
- The list below shows each backup's time, file name and size, marked "includes config" (含設定檔) when the `.yaml` exists.
- The download icon downloads the database; the **yaml** button downloads the config file from the same time.

Only a logged-in administrator can make and download backups; API keys cannot. You can also copy the files straight from the folder above.

## Restoring

1. Stop Mi302.
2. Replace `library.db` in the `data` folder with the `.db` backup, and delete `library.db-wal` and `library.db-shm`.
3. If needed, replace `config.yaml` with the `.yaml` from the same time.
4. Start Mi302.

Example for the one-line installer on Linux (use the file name of the backup you want):

```bash
mi302 stop
cd /opt/mi302/config
mkdir -p data/before-restore
mv data/library.db* data/before-restore/     # move the current database aside, including -wal and -shm
cp data/backups/mi302-20260927-031500.db data/library.db
cp data/backups/mi302-20260927-031500.yaml config.yaml   # only if you also want the old settings
mi302 start
```

- With the one-line installer on macOS, use `~/Mi302/config` instead of `/opt/mi302/config`.
- With a manual Python install, stop Mi302 first (Ctrl+C in its terminal, or `sudo systemctl stop mi302`). `data` and `config.yaml` are in the folder you start Mi302 from.
- These files belong to the account that runs Mi302. Work as that account. If you copy with `sudo`, give the files back to that account afterwards, for example `sudo chown <user> data/library.db config.yaml`, or Mi302 cannot write to them.

After a restore:

- Accounts, passwords, watch history and the 115 login go back to the time of the backup. Watch history recorded after the backup is lost.
- If the 115 login in the backup is no longer valid (for example another login of the same device type kicked it out), scan the QR code again.
- 115 sync continues from where the backup left off: the next incremental sync reads the life events since then. If the gap is too long and the events are no longer available from 115, it runs a full sync automatically.
- If you restored the config file, libraries, sync tasks and other settings are back to the old state as well.

## Security

Backups contain sensitive data. Treat them like passwords and do not share or publish them:

- The `.db` file contains your 115 login cookie; anyone who has it can use your 115 account. It also holds password hashes, player login tokens and API keys.
- The `.yaml` file is a copy of the config file and may contain plaintext secrets: passwords under `users`, the MoviePilot password and API token, `p115.cookies`, and `api_keys`.

Delete downloaded backups from your computer when you no longer need them. The [Configuration Reference](Configuration-Reference) lists which config values are stored in plaintext.
