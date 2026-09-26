[繁體中文](首次設定) | [简体中文](首次设置) | **English**

After installing Mi302, follow this page to finish the first setup in the web admin page: administrator account, libraries, 115 sync, scraping and players. The second half covers user management and how the config file `config.yaml` works.

The web admin page is only in Traditional Chinese. This page describes each button in English and gives the original label once in parentheses. You do not need to edit any config file; everything is done in the web page.

## 1. Open the web admin page

Open `http://<host>:8096/web` in a browser, using the port you chose at install time. The installer prints this URL at the end as the web admin page (管理網頁).

## 2. Create the administrator

The first time you open the page, while no account exists yet, you see "Welcome! Create the administrator account first" (歡迎！先建立管理員帳號):

1. The **Account** field (帳號) is prefilled with `admin`; you can change it.
2. Fill in **Password** (密碼) and **Confirm password** (再輸入一次密碼).
3. Click **Create and sign in** (建立並登入).

This account signs in to the web admin page and can also sign in from players. From then on, the web admin login only accepts administrator accounts.

If the `users` list in the config file already contains accounts, Mi302 creates them at startup, this screen does not appear, and you sign in with one of those accounts.

## 3. Follow the Overview checklist

After signing in you land on the **Overview** page (概覽). Until setup is done, a **Getting started** card (開始設定) at the top lists these steps:

| Step | Checked when |
| --- | --- |
| Create a library (建立媒體庫) | At least one library exists |
| Log in to 115 (登入 115) | 115 is logged in |
| Add a sync task and sync (新增同步任務並同步) | At least one sync task has finished its first sync |
| Set up MoviePilot scraping, optional (設定 MoviePilot 刮削（選用）) | The MoviePilot connection details are filled in |

Each unfinished step has a **Go** button (前往) that takes you to the right page. Once the first three steps are done, the card no longer appears. The sections below follow the same order.

## 4. Create a library

A library is a category on the players' home screen, such as Movies or Series. On the **Libraries** page (媒體庫):

1. Click **Add library** (新增媒體庫). The first library is named 電影 (Movies) by default.
2. Enter a name and pick the type: **Movies** (電影) or **Series** (劇集).
3. Click **Add folder** (加入資料夾). In the **Choose folder** dialog (選擇資料夾), click through to a folder on the server, or type a path at the top and click **Go** (前往), then click **Choose this folder** (選這個資料夾). A library can have several folders.
4. Click **Save and scan** (儲存並掃描) at the bottom of the page.

Folders are paths on the machine that runs Mi302, and the account running Mi302 must be able to read them. If a folder contains category folders such as 国产剧 or 日番, pick the parent folder; Mi302 looks inside. Folder layout and naming are covered in [Library and Scanning](Library-and-Scanning).

## 5. Log in to 115 and sync

115 Cloud (115 網盤) is a Chinese cloud drive. On the **115 Cloud** page (115 網盤):

1. In the **Account** card (帳號), click **Scan QR code** (掃碼登入), scan the code with the 115 mobile app and confirm. QR-code login occupies one 115 device type (by default the Alipay mini program); other logins of the same type are signed out. You can change the type under **Advanced settings** (進階設定).
2. In the **Sync tasks** card (同步任務):
   - Next to **115 folder** (115 目錄), click **Browse** (瀏覽) and choose a folder on 115.
   - Next to **Local folder** (放到本機資料夾), click **Browse** and choose the local folder for the `.strm` files.
   - Click **Add task** (新增任務).
3. In the **Sync** card (同步), click **Incremental sync** (增量同步). The first sync of a task automatically runs as a full sync. After a sync, only the folders that changed are rescanned.

The local folder must be inside one of a library's folders; a subfolder is best. For example, if the Movies library uses `/media/movies`, put the sync task into `/media/movies/115`. Use full paths. Two tasks cannot share a local folder or have one inside the other. The folder structure on 115 is kept as it is.

Finally, turn on automatic sync. In the **Sync options** card (同步選項):

- **Auto incremental sync interval (minutes)** (自動增量同步間隔（分鐘）) defaults to 0, which means no automatic sync. 5 is a good value.
- **Auto full sync interval (hours)** (自動全量同步間隔（小時）) defaults to 168, once a week.

Click **Save sync options** (儲存同步選項). Login methods, the difference between incremental and full sync, and the other sync options are covered in [115 Cloud Sync](115-Cloud-Sync).

## 6. Scraping (optional)

Scraping means fetching metadata and artwork. Mi302 does not scrape by itself. Without MoviePilot, the nfo files, posters and subtitles that already exist on 115 are downloaded together with the `.strm` files (the sync option 一併下載 115 上的 nfo、海報、字幕, on by default).

If you use MoviePilot, on the **MoviePilot** page:

1. Enter the **MoviePilot URL** (MoviePilot 網址), for example `http://192.168.1.10:3000`.
2. Enter the **API token** (API 令牌). In MoviePilot you find it under Settings → System → API token.
3. Click **Save** (儲存), then **Test connection** (測試連線).

From then on, new `.strm` files created by sync are sent to MoviePilot for scraping automatically. Path mappings, filling missing episodes and more are covered in [MoviePilot](MoviePilot).

## 7. Add the server in your players

In Infuse, VidHub, SenPlayer, the official Emby apps or another player, add an **Emby** server:

- Address: `http://<host>:8096`, without `/web`. The **Connect in your player** card (在播放器裡連線) on the Overview page shows this address with a copy button. It shows the address you used to open the web page; if you opened it via `localhost`, use the machine's LAN IP instead.
- Account: the administrator account or an account created on the **Users** page (使用者).

The server name shown in players is set under **Advanced settings** → **Server name** (伺服器名稱); the default is `Emby Server`. To watch away from home, see [Playback](Playback).

## Users

The **Users** page (使用者) lists all accounts, whether each one is an **administrator** (管理員) or a **regular user** (一般使用者), and when it last signed in.

- Every account can sign in from players and has its own watch history and favorites. Only administrators can sign in to the web admin page.
- To add an account, fill in the account name and password under **Add user** (新增使用者), turn on **Administrator** (管理員) if the person should manage Mi302 too, and click **Add** (新增). The password cannot be empty.
- Each account has **Change password** (改密碼), **Make administrator** (設為管理員) or **Remove administrator** (取消管理員), and a delete button. Deleting an account also deletes its watch history.
- At least one administrator must remain: the last administrator cannot be deleted or demoted.
- Whether players list the account names on their login screen is set under **Advanced settings** with **List user names on the player login screen** (播放器登入畫面列出使用者名稱). It is on by default.

To reset a forgotten password, see [Logs and FAQ](Logs-and-FAQ).

## The config file config.yaml

All settings from the web page are stored in the config file `config.yaml`, and the two stay in sync. With the one-line installer it is `config/config.yaml` in the install folder (`/opt/mi302/config/config.yaml` on Linux); with a manual install it is in the folder you run Mi302 from. The **Advanced settings** page shows the full path at the top.

- **Created automatically**: on first start Mi302 creates the file, with a comment explaining each setting. You do not need to create it.
- **Written back when you save in the web page**: Mi302 rewrites the whole file and first copies the old one to `config.yaml.bak`. The file is regenerated from a template each time, so comments you add yourself are not kept.
- **You can edit it directly**: after editing, reload the web admin page in the browser and Mi302 rereads and applies the file. Before saving from the web page, Mi302 also reads in any manual edits first, so they are not overwritten.
- **Syntax errors**: the web page shows a banner saying the config file has an error and was not applied (設定檔有錯，沒有套用), and Mi302 keeps using the last settings it read. Fix the file and reload the page. However, if the file is broken when Mi302 restarts, Mi302 does not start; restore `config.yaml.bak` in that case.
- **Unknown keys**: typos and keys left over from older versions are skipped with a warning; they do not stop Mi302 from starting.
- **Keys that need a restart**: `host`, `port` and `data_dir` under `server`. `users`, `p115.cookies` and `p115.timeout` are also only read at startup.

Every key with its meaning, default and range is listed in [Configuration Reference](Configuration-Reference).

### What is not in the config file

These are stored in the database `data/library.db`, not in the config file:

- accounts and passwords (only password hashes are stored)
- the 115 login state
- API keys created in the web page
- watch history, the sync index, media info, learned intros and credits, and so on

The `users` list in the config file only pre-creates accounts: at startup, accounts in the list that do not exist yet are created. Existing accounts are never changed, and passwords changed in the web page are not written back to the file. These passwords are stored in plain text in the file, so keep the config file and its backups safe. `api_keys` in the config file are fixed API keys; they work alongside the keys created in the web page.

Older versions kept web settings and sync tasks in the database. On the first start after updating, Mi302 moves them into the config file automatically.

Backups of the database and config file are covered in [Backup and Restore](Backup-and-Restore).
