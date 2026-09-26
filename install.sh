#!/usr/bin/env bash
# Mi302 部署腳本：安裝、更新、啟動停止、移除。
#
#   curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash
#
# 裝好後用 mi302 指令管理（mi302 status / logs / restart / update ...），完整說明：bash install.sh --help

set -euo pipefail
umask 022

REPO_URL="${MI302_REPO:-https://github.com/MiCat-S/Mi302.git}"
TARBALL_URL="https://codeload.github.com/MiCat-S/Mi302/tar.gz/refs/heads"
PIP_MIRROR_CN="https://pypi.tuna.tsinghua.edu.cn/simple"
SERVICE="mi302"
LAUNCHD_LABEL="com.mi302.server"
WRAPPER="/usr/local/bin/mi302"

usage() {
	cat <<'EOF'
Mi302 部署腳本

用法：bash install.sh [指令] [選項]

指令：
  install          安裝；已經裝過時等於更新並套用新選項（預設）
  update           更新到最新版並重新啟動
  status           看執行狀態與網址
  logs             即時看日誌（Ctrl+C 離開）
  start | stop | restart
  reset-password 帳號 新密碼
                   忘記密碼時重設（帳號不存在會建立成管理員）
  uninstall        移除開機自動啟動與 mi302 指令，保留程式、設定和資料

選項：
  --dir 資料夾      安裝位置（Linux 預設 /opt/mi302，macOS 預設 ~/Mi302；
                   在程式資料夾裡執行本腳本時，就裝在那個資料夾）
  --port 埠號       網頁與播放器用的埠號（預設 8096）
  --user 使用者     用哪個 Linux 使用者執行，要能讀寫媒體資料夾
                   （預設是執行 sudo 的使用者）
  --mirror         pip 用清華鏡像（國內網路；連不上 PyPI 時會自動改用）
  --branch 分支     預設 main
  -y, --yes        不詢問，沒給的選項都用預設值

例子：
  sudo bash install.sh                                   # 互動式安裝
  sudo bash install.sh --user cat --port 8097 -y         # 以 cat 身分執行，埠號 8097
  curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash -s -- -y
EOF
}

# ---------- 輸出與詢問 ----------

if [ -t 1 ]; then
	C_G=$'\033[32m' C_Y=$'\033[33m' C_R=$'\033[31m' C_B=$'\033[1m' C_0=$'\033[0m'
else
	C_G="" C_Y="" C_R="" C_B="" C_0=""
fi
info() { printf '%s==>%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s注意：%s%s\n' "$C_Y" "$*" "$C_0" >&2; }
die() {
	printf '%s錯誤：%s%s\n' "$C_R" "$*" "$C_0" >&2
	exit 1
}
has() { command -v "$1" >/dev/null 2>&1; }

can_ask() { [ "$YES" != 1 ] && (exec </dev/tty) 2>/dev/null; }

# ask 變數 問題 預設值：用 /dev/tty 讀，所以 curl | bash 也能互動
ask() {
	local __var=$1 __q=$2 __def=${3-} __ans=""
	if can_ask; then
		read -r -p "$__q${__def:+ [$__def]}：" __ans </dev/tty || true
	fi
	printf -v "$__var" '%s' "${__ans:-$__def}"
}

confirm() { # confirm 問題 預設(y/n)
	local ans
	ask ans "$1 (y/n)" "$2"
	case $ans in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# ---------- 參數 ----------

ORIG_ARGS=("$@")
CMD=install
MODE="" DIR="" PORT="" RUN_USER="" MIRROR="" BRANCH="" YES=0
EXTRA=()

parse_args() {
	while [ $# -gt 0 ]; do
		case $1 in
		install | update | uninstall | status | logs | start | stop | restart | reset-password) CMD=$1 ;;
		--python) MODE=python ;; # 舊版的選項，現在只有這一種方式
		--docker | --media | --media=*) die "這個版本不再提供 Docker（媒體資料夾直接在網頁上選），拿掉 $1 重新執行" ;;
		--dir | --port | --user | --branch)
			[ $# -ge 2 ] || die "$1 後面要接值"
			case $1 in
			--dir) DIR=$2 ;;
			--port) PORT=$2 ;;
			--user) RUN_USER=$2 ;;
			--branch) BRANCH=$2 ;;
			esac
			shift
			;;
		--dir=*) DIR=${1#*=} ;;
		--port=*) PORT=${1#*=} ;;
		--user=*) RUN_USER=${1#*=} ;;
		--branch=*) BRANCH=${1#*=} ;;
		--mirror) MIRROR=$PIP_MIRROR_CN ;;
		-y | --yes) YES=1 ;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			if [ "$CMD" = reset-password ]; then
				EXTRA+=("$1")
			else
				die "不認得的參數：$1（看 bash install.sh --help）"
			fi
			;;
		esac
		shift
	done
	if [ -n "$PORT" ]; then
		case $PORT in '' | *[!0-9]*) die "埠號要是數字：$PORT" ;; esac
		[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "埠號要在 1–65535：$PORT"
	fi
}

# ---------- 環境 ----------

OS=$(uname -s)
SCRIPT_PATH=""
if [ -f "${BASH_SOURCE[0]:-}" ]; then
	SCRIPT_PATH=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
fi

need_privilege() {
	case $OS in
	Linux)
		[ "$(id -u)" = 0 ] && return
		if [ -n "$SCRIPT_PATH" ] && has sudo; then
			info "需要 root 權限，用 sudo 重新執行"
			exec sudo bash "$SCRIPT_PATH" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
		fi
		die "請用 root 執行，或加上 sudo，例如：curl -fsSL …/install.sh | sudo bash"
		;;
	Darwin)
		[ "$(id -u)" != 0 ] || die "macOS 請不要用 sudo，直接以自己的帳號執行"
		;;
	*) die "不支援的系統：$OS。Windows 請照 README 手動用 Python 執行" ;;
	esac
}

user_home() {
	local h=""
	has getent && h=$(getent passwd "$1" | cut -d: -f6)
	echo "${h:-/}"
}

owner_of() { if [ "$OS" = Darwin ]; then stat -f %Su "$1"; else stat -c %U "$1"; fi; }

# as_user 使用者 指令…：以指定使用者身分執行（本來就是那個人時直接執行）
as_user() {
	local u=$1
	shift
	if [ "$u" = "$(id -un)" ]; then
		"$@"
	elif has runuser; then
		runuser -u "$u" -- env HOME="$(user_home "$u")" "$@"
	else
		sudo -H -u "$u" -- "$@"
	fi
}

pkg_install() { # 用系統的套件管理員安裝，失敗回傳非 0
	if has apt-get; then
		DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
	elif has dnf; then
		dnf install -y -q "$@"
	elif has yum; then
		yum install -y -q "$@"
	elif has apk; then
		apk add --no-cache "$@"
	elif has pacman; then
		pacman -Sy --noconfirm --needed "$@"
	elif has zypper; then
		zypper -q install -y "$@"
	elif has brew; then
		brew install "$@"
	else
		return 1
	fi
}

default_dir() {
	# 在程式資料夾裡執行（例如自己 git clone 下來的）就裝在那裡
	if [ -n "$SCRIPT_PATH" ] && [ -f "$(dirname "$SCRIPT_PATH")/embyserver/__main__.py" ]; then
		dirname "$SCRIPT_PATH"
	elif [ "$OS" = Linux ]; then
		echo /opt/mi302
	else
		echo "$HOME/Mi302"
	fi
}

# ---------- 部署設定（存在 <安裝位置>/.env） ----------

load_env() {
	local file="$DIR/.env" k v
	[ -f "$file" ] || return 0
	while IFS='=' read -r k v || [ -n "$k" ]; do
		case $k in
		MI302_MODE | MI302_PORT | MI302_USER | MI302_CONF | MI302_BRANCH | TZ | PIP_MIRROR)
			v=${v%$'\r'}
			v=${v#\"}
			v=${v%\"}
			printf -v "SAVED_$k" '%s' "$v"
			;;
		esac
	done <"$file"
}

save_env() {
	local file="$DIR/.env"
	{
		echo "# Mi302 部署設定，install.sh 產生；埠號在 config.yaml 的 server.port，改完執行 mi302 restart"
		echo "MI302_MODE=\"$MODE\""
		echo "MI302_USER=\"$RUN_USER\""
		echo "MI302_CONF=\"$CONF\""
		[ "$BRANCH" = main ] || echo "MI302_BRANCH=\"$BRANCH\""
		echo "TZ=\"$TZ_NAME\""
		echo "PIP_MIRROR=\"$MIRROR\""
	} >"$file.tmp"
	mv "$file.tmp" "$file"
}

host_tz() {
	local tz=""
	if has timedatectl; then tz=$(timedatectl show -p Timezone --value 2>/dev/null || true); fi
	if [ -z "$tz" ] && [ -L /etc/localtime ]; then
		tz=$(readlink /etc/localtime | sed -n 's|.*zoneinfo/||p')
	fi
	[ -z "$tz" ] && [ -f /etc/timezone ] && tz=$(head -n1 /etc/timezone)
	echo "${tz:-Asia/Shanghai}"
}

# ---------- 取得程式 ----------

git_in_dir() { # 以資料夾擁有者的身分跑 git，免得 root 在別人的資料夾留下 root 的檔案
	as_user "$(owner_of "$DIR")" git -c safe.directory="$DIR" -C "$DIR" "$@"
}

fetch_code() {
	if [ -f "$DIR/embyserver/__main__.py" ]; then
		if [ ! -d "$DIR/.git" ]; then
			# 自己下載 ZIP 解壓的：安裝時直接用，update 才下載新版
			if [ "$CMD" = install ]; then
				info "使用 $DIR 裡的程式"
			else
				info "更新程式（$DIR，下載壓縮檔）"
				download_tarball
			fi
			return
		fi
		has git || pkg_install git >/dev/null 2>&1 || true
		has git || die "需要 git 才能更新 $DIR"
		info "更新程式（$DIR）"
		git_in_dir fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" ||
			die "無法連到 GitHub 更新程式，檢查網路後再試"
		if ! git_in_dir diff --quiet HEAD --; then
			local patch
			patch="$DIR/local-changes-$(date +%Y%m%d-%H%M%S).patch"
			git_in_dir diff HEAD -- >"$patch"
			chown "$(owner_of "$DIR")" "$patch" 2>/dev/null || true
			warn "程式資料夾裡有自己改過的檔案，已備份成 $patch，然後還原成最新版"
			git_in_dir reset -q --hard
		fi
		git_in_dir checkout -q -B "$BRANCH" "origin/$BRANCH"
	elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
		die "$DIR 已經有其他檔案，換個位置：--dir 別的資料夾"
	else
		mkdir -p "$(dirname "$DIR")"
		if ! has git; then
			info "安裝 git"
			pkg_install git >/dev/null 2>&1 || true
		fi
		if has git; then
			info "下載程式到 $DIR"
			git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$DIR" || die "無法從 $REPO_URL 下載程式，檢查網路後再試"
		else
			info "下載程式到 $DIR（沒有 git，改下載壓縮檔）"
			mkdir -p "$DIR"
			download_tarball
		fi
	fi
}

download_tarball() {
	has curl || die "需要 curl 或 git 才能下載程式"
	has tar || die "需要 tar"
	curl -fsSL "$TARBALL_URL/$BRANCH" | tar -xz --strip-components=1 -C "$DIR" ||
		die "無法下載程式壓縮檔，檢查網路後再試"
}

version() {
	if [ -d "$DIR/.git" ] && has git; then
		git_in_dir log -1 --format='%h（%cd）' --date=format:'%Y-%m-%d %H:%M' 2>/dev/null || true
	fi
}

# ---------- Python ----------

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

find_python() {
	local c
	for c in python3.13 python3.12 python3.11 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
		if has "$c" && py_ok "$c"; then
			command -v "$c"
			return 0
		fi
	done
	return 1
}

ensure_python() {
	PY=$(find_python) && return
	info "找不到 Python 3.10 以上，嘗試安裝"
	if [ "$OS" = Darwin ]; then
		has brew || die "請先安裝 Homebrew（https://brew.sh），或從 python.org 安裝 Python 3.12"
		brew install python@3.12 || true
	elif has apt-get; then
		pkg_install python3 python3-venv python3-pip || true
	elif has dnf || has yum; then
		pkg_install python3.12 || pkg_install python3.11 || pkg_install python3 || true
	else
		pkg_install python3 || true
	fi
	PY=$(find_python) ||
		die "系統的 Python 太舊（需要 3.10 以上），請先安裝新版 Python（例如 python3.12）再執行"
}

ensure_venv() {
	local venv="$DIR/.venv" owner
	owner=$(owner_of "$DIR")
	if [ -x "$venv/bin/python" ] && py_ok "$venv/bin/python" && "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
		return
	fi
	rm -rf "$venv"
	info "建立 Python 虛擬環境（$($PY -V 2>&1)）"
	if ! as_user "$owner" "$PY" -m venv "$venv" >/dev/null 2>&1; then
		rm -rf "$venv"
		# Debian／Ubuntu 的 venv 模組是另外一個套件
		if has apt-get; then
			local ver
			ver=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
			pkg_install "python$ver-venv" >/dev/null 2>&1 || pkg_install python3-venv >/dev/null 2>&1 || true
		fi
		as_user "$owner" "$PY" -m venv "$venv" || die "無法建立 Python 虛擬環境（Debian／Ubuntu 請先 apt install python3-venv）"
	fi
}

ensure_ffprobe() { # 媒體資訊探測要用 ffprobe；裝不起來不影響其他功能
	has ffprobe && return
	info "安裝 ffmpeg（媒體資訊探測用）"
	pkg_install ffmpeg >/dev/null 2>&1 || warn "沒裝成 ffmpeg，媒體資訊只能讀現成的 X-mediainfo.json；需要時自己安裝 ffmpeg"
}

pick_mirror() {
	[ -n "$MIRROR" ] && return
	has curl || return 0
	if ! curl -fsS -m 6 -o /dev/null https://pypi.org/simple/pip/ 2>/dev/null; then
		MIRROR=$PIP_MIRROR_CN
		info "連不上 PyPI，改用清華鏡像"
	fi
}

pip_install() {
	info "安裝相依套件"
	local args=(-m pip install -q --disable-pip-version-check -r "$DIR/requirements.txt")
	[ -n "$MIRROR" ] && args+=(-i "$MIRROR")
	as_user "$(owner_of "$DIR")" "$DIR/.venv/bin/python" "${args[@]}" ||
		die "安裝相依套件失敗；國內網路可以加 --mirror 再試"
}

# 在設定資料夾裡以執行身分跑 python（設定檔、資料庫的擁有者才會對）
conf_python() {
	(cd "$CONF" && as_user "$RUN_USER" env PYTHONPATH="$DIR" "$DIR/.venv/bin/python" "$@")
}

config_port() { # 讀設定檔裡的埠號；沒有設定檔時是預設 8096
	conf_python -c 'from embyserver.config import load_config; print(load_config("config.yaml").server.port)' 2>/dev/null ||
		echo 8096
}

set_config_port() {
	conf_python - "$1" <<'EOF'
import sys
from embyserver import config_file
from embyserver.config import load_config
config = load_config("config.yaml")
config.server.port = int(sys.argv[1])
config_file.write(config, "config.yaml")
EOF
}

prepare_conf() {
	mkdir -p "$CONF"
	if [ "$CONF" = "$DIR" ]; then
		# 舊的放法：設定檔和資料直接放在程式資料夾
		mkdir -p "$DIR/data"
		chown -R "$RUN_USER" "$DIR/data"
		for f in config.yaml config.yaml.bak; do [ -e "$DIR/$f" ] && chown "$RUN_USER" "$DIR/$f"; done
		chown "$RUN_USER" "$DIR"
	else
		chown -R "$RUN_USER" "$CONF"
	fi
	return 0
}

# ---------- 服務：systemd（Linux）、launchd（macOS），都沒有時用背景程序 ----------

has_systemd() { [ -d /run/systemd/system ] && has systemctl; }
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"

write_systemd() {
	cat >"$UNIT_FILE" <<EOF
# Mi302，install.sh 產生；要改就重新執行 install.sh，不要直接改這個檔
[Unit]
Description=Mi302 (Emby compatible media server)
Wants=network-online.target
After=network-online.target remote-fs.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$CONF
Environment="PYTHONPATH=$DIR" PYTHONUNBUFFERED=1
ExecStart="$DIR/.venv/bin/python" -m embyserver -c "$CONF/config.yaml"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
	systemctl daemon-reload
	systemctl enable -q "$SERVICE"
}

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

write_launchd() {
	mkdir -p "$(dirname "$PLIST")" "$CONF/data/logs"
	local d c
	d=$(xml_escape "$DIR")
	c=$(xml_escape "$CONF")
	cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$LAUNCHD_LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$d/.venv/bin/python</string>
		<string>-m</string>
		<string>embyserver</string>
		<string>-c</string>
		<string>$c/config.yaml</string>
	</array>
	<key>WorkingDirectory</key><string>$c</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PYTHONPATH</key><string>$d</string>
		<key>PYTHONUNBUFFERED</key><string>1</string>
	</dict>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
	<key>StandardOutPath</key><string>$c/data/logs/console.log</string>
	<key>StandardErrorPath</key><string>$c/data/logs/console.log</string>
</dict>
</plist>
EOF
}

bg_running() { [ -f "$CONF/mi302.pid" ] && kill -0 "$(cat "$CONF/mi302.pid")" 2>/dev/null; }

svc() { # svc start|stop|restart|status
	local action=$1
	if [ "$OS" = Darwin ]; then
		local target
		target="gui/$(id -u)"
		case $action in
		start) launchctl bootstrap "$target" "$PLIST" 2>/dev/null || launchctl kickstart "$target/$LAUNCHD_LABEL" ;;
		stop) launchctl bootout "$target/$LAUNCHD_LABEL" 2>/dev/null || true ;;
		restart)
			launchctl bootout "$target/$LAUNCHD_LABEL" 2>/dev/null || true
			sleep 1
			launchctl bootstrap "$target" "$PLIST"
			;;
		status) launchctl print "$target/$LAUNCHD_LABEL" 2>/dev/null | grep -E '^[[:space:]]*(state|pid|last exit code) =' || echo "沒有在執行" ;;
		esac
	elif has_systemd && [ -f "$UNIT_FILE" ]; then
		case $action in
		status) systemctl --no-pager --lines=0 status "$SERVICE" || true ;;
		*) systemctl "$action" "$SERVICE" ;;
		esac
	else
		case $action in
		start)
			bg_running && return 0
			mkdir -p "$CONF/data/logs"
			chown -R "$RUN_USER" "$CONF/data"
			local run=()
			has setsid && run+=(setsid)
			[ "$RUN_USER" != "$(id -un)" ] && run+=(runuser -u "$RUN_USER" --)
			# 背景的只能是單一指令（不能是 cd && …），$! 才會是 Mi302 本身，也不會佔住這個腳本的輸出
			(
				cd "$CONF" || exit 1
				nohup ${run[@]+"${run[@]}"} env HOME="$(user_home "$RUN_USER")" PYTHONPATH="$DIR" PYTHONUNBUFFERED=1 \
					"$DIR/.venv/bin/python" -m embyserver -c "$CONF/config.yaml" </dev/null >>"$CONF/data/logs/console.log" 2>&1 &
				echo $! >"$CONF/mi302.pid"
			)
			;;
		stop)
			if bg_running; then
				# setsid 開了新的程序群組，連同子程序一起停
				local pid
				pid=$(cat "$CONF/mi302.pid")
				kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
				local _
				for _ in $(seq 1 15); do
					kill -0 "$pid" 2>/dev/null || break
					sleep 1
				done
			fi
			rm -f "$CONF/mi302.pid"
			;;
		restart)
			svc stop
			sleep 1
			svc start
			;;
		status) if bg_running; then echo "在背景執行（pid $(cat "$CONF/mi302.pid")）"; else echo "沒有在執行"; fi ;;
		esac
	fi
}

# ---------- 共用 ----------

wait_ready() {
	has curl || return 0
	local _
	for _ in $(seq 1 60); do
		if curl --noproxy "*" -fsS -m 2 -o /dev/null "http://127.0.0.1:$PORT/System/Info/Public" 2>/dev/null; then
			return 0
		fi
		sleep 1
	done
	return 1
}

lan_ip() {
	local ip=""
	if [ "$OS" = Darwin ]; then
		ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
	else
		ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
		[ -n "$ip" ] || ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
	fi
	echo "${ip:-<這台主機的 IP>}"
}

firewall_hint() {
	if has ufw && ufw status 2>/dev/null | grep -q "Status: active"; then
		warn "ufw 防火牆開著，其他裝置連不上時執行：sudo ufw allow $PORT/tcp"
	elif has firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
		warn "firewalld 開著，其他裝置連不上時執行：sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
	fi
}

log_file() { echo "$CONF/data/logs/mi302.log"; }

install_wrapper() {
	local dir
	dir=$(dirname "$WRAPPER")
	if [ -d "$dir" ] && [ -w "$dir" ]; then
		cat >"$WRAPPER" <<EOF
#!/bin/sh
# Mi302 管理指令（install.sh 產生）：mi302 status | logs | restart | update | reset-password 帳號 密碼 | uninstall
[ \$# -eq 0 ] && set -- status
exec bash "$DIR/install.sh" --dir "$DIR" "\$@"
EOF
		chmod +x "$WRAPPER"
		HAS_WRAPPER=1
	else
		HAS_WRAPPER=0
	fi
}

manage_cmd() { if [ "${HAS_WRAPPER:-0}" = 1 ] || [ -x "$WRAPPER" ]; then echo "mi302"; else echo "bash $DIR/install.sh"; fi; }

# 決定模式、位置、設定資料夾；install 以外的指令要已經裝過
resolve() {
	[ -n "$DIR" ] || DIR=$(default_dir)
	DIR=${DIR%/}
	case $DIR in /*) ;; *) DIR="$(pwd)/$DIR" ;; esac
	load_env
	[ -n "$MODE" ] || MODE=${SAVED_MI302_MODE-}
	if [ "$MODE" = docker ]; then
		# 舊的 Docker 安裝：這個版本不再提供 Docker，改成直接用 Python，config/ 裡的設定和資料照用
		[ "$CMD" = install ] || die "這個版本不再提供 Docker。先 docker compose down，再執行 bash install.sh 改成直接用 Python（設定和資料會沿用）"
		warn "偵測到舊的 Docker 安裝，改成直接用 Python 執行；請先確認容器已經停掉（docker compose down）"
		MODE=python
	fi
	[ -n "$RUN_USER" ] || RUN_USER=${SAVED_MI302_USER-}
	[ -n "$MIRROR" ] || MIRROR=${SAVED_PIP_MIRROR-}
	[ -n "$BRANCH" ] || BRANCH=${SAVED_MI302_BRANCH:-main}
	TZ_NAME=${SAVED_TZ:-$(host_tz)}
	set_conf
	if [ "$CMD" != install ] && [ -z "$MODE" ]; then
		die "$DIR 還沒用這個腳本安裝過；先執行 bash install.sh（或用 --dir 指定安裝位置）"
	fi
}

# 設定檔和資料的資料夾：<安裝位置>/config；以前手動執行、設定檔和資料放在程式資料夾的就沿用
set_conf() {
	if [ -n "${SAVED_MI302_CONF-}" ]; then
		CONF=$SAVED_MI302_CONF
	elif [ ! -d "$DIR/config" ] && { [ -f "$DIR/config.yaml" ] || [ -d "$DIR/data" ]; }; then
		CONF=$DIR
	else
		CONF=$DIR/config
	fi
}

port_busy() { has curl && curl --noproxy "*" -s -m 2 -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null; }

# 這個埠號已經被別的程式（例如之前手動執行的 Mi302）佔用時先提醒，不然服務會啟動失敗
check_port_free() {
	local running=0
	if [ "$OS" = Darwin ]; then
		launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1 && running=1
	else
		bg_running && running=1
		has_systemd && systemctl is-active -q "$SERVICE" 2>/dev/null && running=1
	fi
	[ "$running" = 1 ] && return 0
	port_busy "$PORT" || return 0
	warn "埠號 $PORT 已經有程式在用（例如之前手動執行的 Mi302），先把它關掉，不然 Mi302 會啟動失敗"
	confirm "已經關掉了，繼續安裝？" n || exit 1
}

# ---------- 指令 ----------

cmd_install() {
	MODE=python
	echo
	echo "${C_B}Mi302 安裝${C_0}（安裝位置 $DIR）"
	set_conf
	fetch_code
	install_wrapper
	install_python
	save_env
	finish
}

install_python() {
	if [ -z "$RUN_USER" ]; then
		if [ "$OS" = Darwin ]; then
			RUN_USER=$(id -un)
		else
			local def=${SUDO_USER:-root}
			[ "$def" = root ] && [ "$(owner_of "$DIR")" != root ] && def=$(owner_of "$DIR")
			ask RUN_USER "用哪個使用者執行 Mi302（要能讀寫媒體資料夾）" "$def"
		fi
	fi
	id "$RUN_USER" >/dev/null 2>&1 || die "沒有這個使用者：$RUN_USER"
	# 問題都先問完，後面安裝時不用守在螢幕前
	if [ -z "$PORT" ] && [ ! -f "$CONF/config.yaml" ]; then
		ask PORT "埠號" 8096
	fi

	ensure_python
	ensure_venv
	ensure_ffprobe
	pick_mirror
	pip_install
	prepare_conf

	local current
	current=$(config_port)
	PORT=${PORT:-$current}
	if [ "$PORT" != "$current" ] || [ ! -f "$CONF/config.yaml" ]; then
		set_config_port "$PORT" || die "無法寫入設定檔 $CONF/config.yaml"
	fi
	check_port_free

	if [ "$OS" = Darwin ]; then
		write_launchd
		info "設定登入後自動啟動（launchd）"
		svc restart
	elif has_systemd; then
		# 以前用背景方式跑過的先停掉
		if bg_running; then svc stop; fi
		write_systemd
		info "設定開機自動啟動（systemd）"
		systemctl restart "$SERVICE"
	else
		warn "這台機器沒有 systemd（例如 WSL），改成在背景執行，重新開機後要自己執行：$(manage_cmd) start"
		svc restart
	fi
}

finish() {
	local ip
	ip=$(lan_ip)
	if wait_ready; then
		echo
		info "${C_B}Mi302 已經在執行${C_0}  版本 $(version)"
	else
		echo
		warn "Mi302 還沒有回應；看日誌找原因：$(manage_cmd) logs"
	fi
	firewall_hint
	cat <<EOF

  管理網頁： http://$ip:$PORT/web
  播放器：   新增 Emby 伺服器，位址 http://$ip:$PORT
  設定檔：   $CONF/config.yaml
  資料和日誌：$CONF/data/

  管理指令： $(manage_cmd) status | logs | restart | update | reset-password 帳號 密碼 | uninstall
EOF
	echo
}

cmd_update() {
	fetch_code
	ensure_python
	ensure_venv
	ensure_ffprobe
	pick_mirror
	pip_install
	prepare_conf
	[ -n "$PORT" ] && [ "$PORT" != "$(config_port)" ] && set_config_port "$PORT"
	PORT=$(config_port)
	[ "$OS" = Darwin ] && write_launchd
	has_systemd && [ -f "$UNIT_FILE" ] && write_systemd
	info "重新啟動"
	svc restart
	save_env
	finish
}

cmd_uninstall() {
	confirm "移除 Mi302 的開機自動啟動和 mi302 指令？程式、設定和資料會留著" y || exit 0
	if [ "$OS" = Darwin ]; then
		svc stop
		rm -f "$PLIST"
	else
		if has_systemd && [ -f "$UNIT_FILE" ]; then
			systemctl disable --now -q "$SERVICE" || true
			rm -f "$UNIT_FILE"
			systemctl daemon-reload
		fi
		svc stop
	fi
	[ -f "$WRAPPER" ] && grep -q "install.sh" "$WRAPPER" && rm -f "$WRAPPER"
	info "已移除。要連資料一起刪掉：rm -rf $DIR"
}

current_port() { config_port; }

main() {
	parse_args "$@"
	need_privilege
	resolve
	cd /
	[ "$CMD" = install ] || RUN_USER=${RUN_USER:-$(id -un)}
	case $CMD in
	install) cmd_install ;;
	update) cmd_update ;;
	uninstall) cmd_uninstall ;;
	status)
		svc status
		PORT=$(current_port)
		if has curl && curl --noproxy "*" -fsS -m 3 -o /dev/null "http://127.0.0.1:$PORT/System/Info/Public" 2>/dev/null; then
			info "網頁：http://$(lan_ip):$PORT/web  版本 $(version)"
		else
			warn "http://127.0.0.1:$PORT 沒有回應"
		fi
		;;
	logs)
		[ -f "$(log_file)" ] || die "還沒有日誌檔：$(log_file)"
		tail -n 100 -F "$(log_file)"
		;;
	start | stop | restart)
		svc "$CMD"
		;;
	reset-password)
		[ ${#EXTRA[@]} -eq 2 ] || die "用法：reset-password 帳號 新密碼"
		conf_python -m embyserver -c config.yaml --reset-password "${EXTRA[@]}"
		;;
	esac
}

# 整個腳本讀完才開始執行：更新時這個檔案會被換掉
main "$@"; exit $?
