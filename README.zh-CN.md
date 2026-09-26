# Mi302

[繁體中文](README.md) | **简体中文** | [English](README.en.md)

Mi302 是一个 API 与 Emby 兼容的视频服务器，给放在 115 网盘里的视频用。它把 115 的文件夹同步成本地的 `.strm` 文件，播放器按 Emby 的方式登录观看；播放时用 HTTP 302 把播放器跳转到 115 直链，视频流量不经过服务器。

不需要后面有真正的 Emby，也不需要其他 115 工具。完整说明在 [Wiki](https://github.com/MiCat-S/Mi302/wiki)。管理页面目前是繁体中文界面。

## 功能

- **115 同步**：扫码登录 115，把文件夹同步成 strm。之后读 115 的生活事件做增量同步，每周再全量同步一次查漏补缺。
- **Emby 兼容**：Infuse、VidHub、SenPlayer、Emby 官方 App 等支持 Emby 的播放器，添加服务器就能登录观看。
- **302 直连播放**：用播放器自己的 User-Agent 向 115 获取直链再跳转过去，服务器不转发视频、不转码。
- **刮削交给 MoviePilot**：新同步的视频自动送去刮削。媒体库缺集时，可以让 MoviePilot 订阅补齐。
- **媒体信息**：读取 `X-mediainfo.json`（神医助手的格式），也能用 ffprobe 探测，播放器能看到 4K、HDR、音轨和字幕轨。
- **片头片尾跳过**：从播放行为中学出片头片尾，SenPlayer 等播放器会出现“跳过片头”。
- **中文友好**：中文片名按拼音排序，拼音、首字母、简繁体都能搜到；演职人员显示中文名，类型中文化。
- **网页管理**：所有设置都在网页 `/web` 完成，并和 `config.yaml` 保持一致；每天自动备份数据库。
- **保护 115 账号**：被 115 限流或登录失效时自动熔断，同步和探测先停下，播放不受影响。

## 工作方式

```mermaid
flowchart LR
    Cloud[115 网盘] -- 同步 --> Strm[本地 .strm]
    Strm -- 扫描 --> Mi302[Mi302]
    Player[播放器] -- Emby API --> Mi302
    Mi302 -- 302 到 115 直链 --> Player
    Player -- 直接读取视频 --> Cloud
```

## 快速开始

Linux 上用一键安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | sudo bash
```

macOS 不要加 sudo：

```bash
curl -fsSL https://raw.githubusercontent.com/MiCat-S/Mi302/main/install.sh | bash
```

脚本会安装 Python、ffmpeg 和依赖，设置开机自启，最后打印管理页面的网址。Windows 或想自己控制每一步的，见 [安装](https://github.com/MiCat-S/Mi302/wiki/安装) 里的手动安装。

装好后用浏览器打开 `http://<主机>:8096/web`：

1. 创建管理员账号。
2. 添加媒体库，选择服务器上的文件夹。
3. 在“115 網盤”页扫码登录，添加同步任务，点“增量同步”。第一次会先自动全量同步。
4. 有 MoviePilot 的话，在“MoviePilot”页填网址和 API 令牌。
5. 在播放器里添加 Emby 服务器，地址填 `http://<主机>:8096`，用刚才的账号登录。

每一步的细节见 [首次设置](https://github.com/MiCat-S/Mi302/wiki/首次设置)。

## 文档

| 页面 | 内容 |
| --- | --- |
| [安装](https://github.com/MiCat-S/Mi302/wiki/安装) | 一键安装、手动安装、`mi302` 管理命令、更新与卸载 |
| [首次设置](https://github.com/MiCat-S/Mi302/wiki/首次设置) | 网页上的设置步骤、用户、配置文件如何工作 |
| [115 网盘与同步](https://github.com/MiCat-S/Mi302/wiki/115-网盘与同步) | 登录、同步任务、增量与全量同步、熔断 |
| [媒体库与扫描](https://github.com/MiCat-S/Mi302/wiki/媒体库与扫描) | 文件夹结构、部分扫描、拼音排序搜索、演职人员中文化 |
| [播放与外网访问](https://github.com/MiCat-S/Mi302/wiki/播放与外网访问) | 302 播放流程、播放地址的登录、下载、反向代理 |
| [片头片尾跳过](https://github.com/MiCat-S/Mi302/wiki/片头片尾跳过) | 怎样学出片头片尾、播放器拿到什么、怎样测试 |
| [媒体信息与探测](https://github.com/MiCat-S/Mi302/wiki/媒体信息与探测) | `X-mediainfo.json`、ffprobe 探测、115 的限速 |
| [MoviePilot 集成](https://github.com/MiCat-S/Mi302/wiki/MoviePilot-集成) | 刮削、补全缺集、把 Mi302 当作 Emby、媒体库封面 |
| [备份与还原](https://github.com/MiCat-S/Mi302/wiki/备份与还原) | 自动备份、下载备份、还原步骤 |
| [配置文件参考](https://github.com/MiCat-S/Mi302/wiki/配置文件参考) | `config.yaml` 每一项的说明和默认值 |
| [日志与常见问题](https://github.com/MiCat-S/Mi302/wiki/日志与常见问题) | 日志、连不上、权限、国内网络、忘记密码 |
| [技术细节与开发](https://github.com/MiCat-S/Mi302/wiki/技术细节与开发) | 请求流程、已实现的 Emby 接口、代码结构、测试 |

## 常用命令

一键安装后可以用 `mi302` 管理：

| 命令 | 作用 |
| --- | --- |
| `mi302 status` | 是否在运行、网址、版本 |
| `mi302 logs` | 实时查看日志 |
| `mi302 restart` | 重启 |
| `mi302 update` | 更新到最新版，配置和数据不动 |
| `mi302 reset-password admin 新密码` | 忘记密码时重置 |

## 致谢

- 302 播放流程参考 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 `embyreverseproxy` 插件。
- 媒体信息文件与 [StrmAssistant（神医助手）](https://github.com/sjtuross/StrmAssistant) 兼容；ffprobe 到 Emby 字段的对照改写自 [xiao-vvv/emby-mediainfo](https://github.com/xiao-vvv/emby-mediainfo)（MIT 许可，版权声明保留在 `embyserver/mediainfo.py`）。
- 刮削和订阅交给 [MoviePilot](https://github.com/jxxghp/MoviePilot)。
- 拼音用 [pypinyin](https://github.com/mozillazg/python-pinyin)，简繁转换用 [zhconv](https://github.com/gumblex/zhconv)。

## 开发

```bash
pip install -r requirements.txt pytest
python -m pytest
```

代码结构和测试方法见 [技术细节与开发](https://github.com/MiCat-S/Mi302/wiki/技术细节与开发)。Wiki 的源文件在 [`docs/wiki`](docs/wiki)，修改文档请改那里，再用 `docs/publish-wiki.sh` 发布。
