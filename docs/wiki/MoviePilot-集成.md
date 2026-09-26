[繁體中文](MoviePilot-整合) | **简体中文** | [English](MoviePilot)

Mi302 不自己刮削，这件事交给 [MoviePilot](https://github.com/jxxghp/MoviePilot)。本页说明如何连接 MoviePilot、哪些视频会送去刮削、补全缺集如何判断缺哪几集，以及如何让 MoviePilot 把 Mi302 当作 Emby 媒体服务器。

Mi302 是按 MoviePilot V3 开发的。旧版大多能用，但有几项功能会打折扣，见[旧版 MoviePilot](#旧版-moviepilot)。

## 工作方式

Mi302 只读取文件夹里已有的 nfo 和图片。这些数据可以在 115 同步时一并下载（见[115 网盘与同步](115-网盘与同步)），或交给 MoviePilot 刮削：

1. 同步生成新的 strm 后，Mi302 先扫描有变动的地方，新片马上出现在播放器里（这时还没有海报）。
2. Mi302 把新 strm 的路径发给 MoviePilot 的刮削 API（`POST /api/v1/media/scrape/local`）。
3. MoviePilot 识别视频，到 TMDB 等来源查询数据，把 nfo、海报、背景图写进同一个文件夹。
4. 刮削完成后，Mi302 只重新扫描刮削过的地方，播放器就能看到海报和简介。

所以 MoviePilot 和 Mi302 要能看到同一批文件，例如装在同一台机器上，或挂载同一个网络硬盘。

## 连接设置

设置在“MoviePilot”页的“连线”卡片。

1. 在 MoviePilot 的“设定 → 系统”复制 **API 令牌**。
2. 在 Mi302 填写“MoviePilot 网址”（例如 `http://192.168.1.10:3000`，要以 `http://` 或 `https://` 开头）和“API 令牌”。
3. 两边看到的路径不一样时，填写“路径对应”，见下一节。
4. 点“测试连线”。它会先保存，再测试。

“测试连线”只检查网址和 API 令牌。它发一个空路径给刮削 API，MoviePilot 会返回“刮削路径无效”，所以不会真的刮削。路径对应是否正确，要看第一次刮削的结果。卡片右上角平时显示“已设定”或“未设定”，测试后显示“连线成功”或“连线失败”。

| 页面上的名称 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| MoviePilot 网址 | `moviepilot.url` | 空 | 末尾的 `/` 会去掉 |
| API 令牌 | `moviepilot.api_token` | 空 | 放在 `X-API-KEY` 请求头和 `token` 查询参数里发送 |
| 同时刮削几项 | `moviepilot.concurrency` | 3 | 1–8 |
| 路径对应 | `moviepilot.path_mappings` | 无 | 见下一节 |
| 同步产生新的 strm 后自动送去刮削 | `moviepilot.scrape_after_sync` | 开 | 关闭时同步完只扫描 |
| MoviePilot 帐号、MoviePilot 密码 | `moviepilot.username`、`moviepilot.password` | 空 | 补全缺集需要；旧版刮削 API 也需要 |
| 全量同步后自动补全 | `moviepilot.fill_after_full_sync` | 关 | 在“补全缺集”卡片，切换后立即保存 |
| （只在配置文件） | `moviepilot.timeout` | 300 | 每一项最多等几秒；超时当作连接失败，这一批停下 |

### 什么时候要填账号密码

账号密码在“连线”卡片里折叠起来的“MoviePilot 帐号密码（补全缺集需要；旧版刮削 API 也需要）”，点一下展开。

- **补全缺集**：MoviePilot 建订阅的 API 只接受账号登录，不接受 API 令牌，一定要填。
- **旧版 MoviePilot**：刮削 API 只接受登录。“测试连线”出现“拒绝存取”时，填写账号密码。

填了账号密码时，MoviePilot 返回 401 或 403，Mi302 会用账号密码登录（`POST /api/v1/login/access-token`）再试一次，之后都用登录拿到的 token；token 过期会自动重新登录。只填网址和账号密码、不填 API 令牌也可以。

## 路径对应

Mi302 发给 MoviePilot 的是 Mi302 看到的文件路径。两边看到的路径一样（例如装在同一台机器上）就不用填。不一样时，在“路径对应”里一行填一条：

```
Mi302 的路径 => MoviePilot 的路径
```

例子：

- Mi302 在 Parallels 虚拟机里看到 `/media/psf/Vo`，MoviePilot 装在 Mac 上看到 `/Volumes/Vo`：填 `/media/psf/Vo => /Volumes/Vo`。
- MoviePilot 用 Docker：看它容器里的挂载路径。主机的 `/volume1/media` 挂载成 `/mnt/media`，就填 `/volume1/media => /mnt/media`。

规则：

- 按路径开头的完整文件夹匹配，多条都符合时用最长的那条。
- 反方向也用同一组规则：MoviePilot 通知 Mi302 重新扫描时发来的是 MoviePilot 的路径，Mi302 会换回自己的路径。
- MoviePilot 返回文件不存在时，“刮削”卡片的错误里会写“MoviePilot 找不到 …，请检查路径对应”，后面是它收到的路径。

配置文件里的写法：

```yaml
moviepilot:
  path_mappings:
    - from: /media/psf/Vo
      to: /Volumes/Vo
```

## 发送规则

同步后自动发送的只有这次新生成的 strm。点“刮削”卡片的“刮削缺少资料的项目”时，检查整个媒体库。两种都按这些规则决定发送什么：

- **电影**：发送 strm 文件本身。已经有同名 nfo（`X.nfo`），或电影放在自己的文件夹、里面有 `movie.nfo` 的不发送。
- **剧集**：整部剧还没有 `tvshow.nfo` 时，发送整个剧集文件夹，一次处理剧、季、集。已经有 `tvshow.nfo` 的剧，只发送没有 nfo 的那几集。
- 已经有 nfo 的不发送，避免覆盖从 115 带下来或之前刮削好的数据。
- 点“刮削缺少资料的项目”时，有 nfo 却没有剧照的集也会再发送一次（30 天内确定没有剧照的除外，见下文）。
- 连接失败、认证失败、MoviePilot 返回 HTTP 错误或超时，整批停下，还没发送的算失败，原因显示在“刮削”卡片上。

剧集文件夹的判断和扫描时一样，媒体库下面可以有分类文件夹（见[媒体库与扫描](媒体库与扫描)）。例如媒体库路径选 `电视剧`、下面是 `国产剧/庆余年 (2019)/Season 1/…`，发送的是 `庆余年 (2019)` 这部剧，tmdbid 也从它的 `tvshow.nfo` 取。

## 刮削速度

MoviePilot 的刮削 API 是同步的：每一项都要等它到 TMDB 查询数据、下载图片才响应，一部片可能要几十秒。Mi302 这样加速：

- **同时发送多项**：“同时刮削几项”默认 3，最多 8。太多时 TMDB 可能限速；MoviePilot 日志出现 429 就调低。
- **已经刮削过的剧带上 tmdbid**：发送单集时，Mi302 从剧的 `tvshow.nfo` 取出 tmdbid，直接告诉 MoviePilot 是哪一部（`media_source=themoviedb&media_id=…`）。MoviePilot 不必再用文件名搜索 TMDB，也不会认错。这需要 MoviePilot V3；旧版会忽略这些参数，照常用文件名识别。

MoviePilot 本身慢的话，多半是连接 TMDB 慢：在 MoviePilot 设置 TMDB 的 API 地址和图片地址代理。

## 刮削结果

“刮削”卡片显示上次刮削（同步后自动或手动）的数字：送出、成功、失败、没有剧照。

MoviePilot 报告完成后，Mi302 会检查它有没有真的写出 nfo：

- 发送单个文件时看 `X.nfo`，发送剧集文件夹时看 `tvshow.nfo`。没写出来就算失败。
- MoviePilot 认不出集数时什么都不写，却报告完成。这种集会记为失败，并说明原因：文件名要有 `S01E01` 这类集号。
- 剧集的一集有 nfo、没有剧照时算成功，另外计入“没有剧照”。

### 单集没有剧照

MoviePilot 的单集图片只来自 TMDB 那一集的剧照，保存为和视频同名的 `X.jpg`（Mi302 也认 `X-thumb.jpg`）。没有图片通常是因为：

1. **TMDB 没有这集的剧照**：国产剧、综艺、刚播出的集很常见，MoviePilot 也写不出来。这种集 Mi302 改用剧的横幅图（`thumb`、`landscape`），没有就用背景图，播放器不会一片空白。
2. **MoviePilot 下载图片失败**：MoviePilot 日志有“图片下载失败”，多半是连不上 `image.tmdb.org`，要在 MoviePilot 设置 TMDB 图片代理。修好之后点“刮削缺少资料的项目”，有 nfo 却没有剧照的集会再发送一次。

发送过、确定没有剧照的集，30 天内点“刮削缺少资料的项目”不再重发。

## 补全缺集

媒体库里的剧少了几集时，可以让 MoviePilot 去下载补齐。设置在“MoviePilot”页的“补全缺集”卡片。

### 准备工作

1. 把 Mi302 加为 MoviePilot 的媒体服务器（见下文“让 MoviePilot 把 Mi302 当作 Emby”），MoviePilot 才知道哪些集已经有了。
2. 在“连线”卡片填写 MoviePilot 的账号密码并保存。
3. 剧要先刮削过，`tvshow.nfo` 里有 tmdbid。没有 tmdbid 的剧不会发送。

### 卡片上的剧集列表

“补全缺集”卡片列出媒体库里所有的剧和每一季有几集：

- 集号有空洞的季（例如有第 2、4 集，没有第 3 集）各占一行，标出缺几集、缺哪几集。有空洞的剧排在前面。
- 在“搜寻剧名”里输入即可搜索，片名、原名、年份、拼音、首字母都能识别。勾选“只看集号有空洞的”只列出有空洞的剧。
- 一次列 20 部，点“再显示 N 部”继续看。
- 每部剧右边的“补全”只发送那一部。没有 tmdbid 的剧标着“没有 tmdbid，要先刮削”，按钮不可用。
- 卡片右上角的“全部补全”发送所有有 tmdbid 的剧，会先确认一次。
- 特别篇（第 0 季）不列出、不发送。

“集号有空洞”只是提示。最后几集没下载到的情况，下面的检查同样算得出来。

### 每一季怎么判断

补全按季逐一处理，只处理媒体库里已经有集的季。TMDB 上有、媒体库整季都没有的季不会自动订阅。

1. 向 MoviePilot 查询 TMDB 上这一季的集和播出日期（`GET /api/v1/tmdb/{tmdbid}/{季}`）。
2. 判断哪些集已经播出：
   - 有播出日期的，日期不晚于今天（Mi302 主机的日期）就算播过。
   - 没有日期的常是还没播的占位集。集号不超过媒体库里这一季最后一集的算播过（都有第 10 集了，第 3 集一定播过）。
   - 在最后一集之后、又没有日期的不确定，不算缺。结果里会注明有几集 TMDB 没有播出日期、没有计入。
3. 对照 Mi302 里这一季已有的集号。已播出的都有，就不建订阅，记为“已经齐全”。
4. 缺集才建订阅（`POST /api/v1/subscribe/`），用 tmdbid 指定是哪一部（V3 的 `media_source`／`media_id`），不靠剧名，不会认错。
5. 立即请 MoviePilot 搜索这条订阅（`POST /api/v1/subscribe/search/{订阅 id}`）。MoviePilot V3 建订阅后只是安排搜索，有时要等到定时搜索才开始。之前就订阅过的，也会请它再搜一次。这一步失败时，结果里会注明，MoviePilot 会在定时搜索时处理。

查不到 TMDB 的集数时，照样建订阅，交给 MoviePilot 判断。

MoviePilot 下载、整理完会通知 Mi302 重新扫描。缺集的季如果还在更新，订阅会继续追新集。

### 结果

卡片上的数字：

| 名称 | 含义 |
|---|---|
| 建订阅并搜寻 | 有缺集、建了订阅并请 MoviePilot 搜索的季数 |
| 缺的集数 | 所有季缺的集数合计 |
| 已经齐全 | 已播出的集都有、没建订阅的季数；旧版 MoviePilot 以“媒体库中已存在”拒绝时也算在这里 |
| 之前订阅过 | MoviePilot 说订阅已经存在的季数（也请它再搜了一次） |
| 没 tmdbid | 没有 tmdbid 而跳过的剧数 |
| 失败 | MoviePilot 不接受订阅，或中途停下后没做的季数 |

展开“每一季的结果（N）”可以看每一季缺哪几集、MoviePilot 怎么回复。连接或认证失败时整批停下，剩下的季算失败。同一时间只运行一批补全；已经在运行时再点，会提示“已经在补全中，等它做完”。

### 全量同步后自动补全

勾选“全量同步后自动补全”（默认关闭，切换后立即保存）时，每次全量同步、刮削完之后，自动把所有有 tmdbid 的剧发送一次。没填账号密码时不做。等刮削完才做，是因为新刮削的剧到这时才有 tmdbid。

## 让 MoviePilot 把 Mi302 当作 Emby

MoviePilot 可以把 Mi302 加为媒体服务器，用来判断片子是否已经有了，整理完自动通知 Mi302 重新扫描。

1. 在 Mi302“MoviePilot”页的“让 MoviePilot 把 Mi302 当成 Emby”卡片，输入用途（例如 `MoviePilot`，不填就叫 MoviePilot），点“建立 API 金钥”。在列表里点复制图标复制密钥。
2. 在 MoviePilot 的“设定 → 媒体服务器”新增 Emby。地址填卡片上显示的“地址”（就是你打开管理页面用的网址，例如 `http://192.168.1.20:8096`），API 密钥粘贴刚才那把。MoviePilot 要能访问这个地址。

API 密钥以管理员身份调用 Emby API，但不能用来操作管理页面。不用了可以在列表里删除；用这把密钥的程序会连不上。

MoviePilot 通知 Mi302 某些文件有变动时（`POST /Library/Media/Updated`），Mi302 按“路径对应”把路径换回自己的路径，只扫描这些文件所在的电影或剧，不重新扫描整个媒体库。通知里没有路径时，才扫描整个媒体库。

## 媒体库封面

播放器首页每个媒体库的封面，可以用 MoviePilot 的媒体库封面插件生成，例如 [wio-ki/MoviePilot-Plugins](https://github.com/wio-ki/MoviePilot-Plugins) 的“Emby媒体库封面生成”：

1. 先按上一节把 Mi302 加为 MoviePilot 的 Emby 媒体服务器。
2. 安装插件，在插件设置的媒体服务器里选 Mi302，选择要生成封面的媒体库。
3. 在插件里手动运行一次，或设置定时任务（例如每天一次）。

插件从媒体库里随机挑选海报组成封面，再上传到 Mi302（`POST /Items/{id}/Images/Primary`）。上传的封面：

- 保存在数据目录（`server.data_dir`）的 `images/` 里，重新扫描不会被覆盖，也比媒体库文件夹里的 `poster`／`folder`／`cover` 图优先。
- 在“媒体库”页能看到。也可以点“上传封面”自己上传，或点“改回预设”删除上传的封面。见[媒体库与扫描](媒体库与扫描)。

插件的“入库监控”要 MoviePilot 整理完成或 Emby 的新增通知才会触发。Mi302 从 115 同步进来的文件不经过这两者，所以新片进来后封面不会自动更新，请用定时任务或手动更新。

## 旧版 MoviePilot

Mi302 是按 MoviePilot V3 开发的。在旧版（例如 V2）上：

- 刮削 API 可能不接受 API 令牌：要填账号密码。
- 刮削时带的 tmdbid 参数会被忽略，照常用文件名识别，比较慢，也可能认错。
- 建订阅：旧版认 `tmdbid` 字段，V3 认 `media_source`／`media_id`，Mi302 两种都发。旧版遇到媒体库已有的会拒绝（“媒体库中已存在”），Mi302 记为“已经齐全”。
- 请 MoviePilot 搜索订阅：V3 用 POST，旧版用 GET。收到 HTTP 405 时，Mi302 改用 GET 再试。
- 查询 TMDB 集数：V3 和旧版返回的格式不同，两种都能识别。
- 遇到 MoviePilot 没有的 API（HTTP 404），Mi302 会显示“MoviePilot 没有这个 API（…），请确认网址或升级 MoviePilot”。

## 其他用到 MoviePilot 的地方

“演职人员显示中文名”开启时，Mi302 通过 MoviePilot 查询 TMDB 人物的别名（`GET /api/v1/tmdb/person/{id}`），挑出中文名。见[媒体库与扫描](媒体库与扫描)。
