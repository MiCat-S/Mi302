# Mi302 的 MoviePilot V3 外掛

[Mi302](https://github.com/MiCat-S/Mi302) 附帶的兩個 MoviePilot V3 外掛。原始碼在 Mi302 倉庫的 `moviepilot-plugin/`，這個倉庫只是發布用的鏡像（由 `docs/publish-plugins.sh` 同步），請到 Mi302 倉庫回報問題。

| 外掛 | 做什麼 |
| --- | --- |
| Mi302 整理助手 | 讓 Mi302 照 MoviePilot 自己的規則算新名字，資料夾結構已經對、只是名字不照格式時，用 MoviePilot 的 115 授權直接批次改名。給 Mi302 的「整理 115 網盤」用。 |
| Mi302 清種助手 | qBittorrent 裡太久沒速度的種子自動刪掉，排在後面的接著開始；不夠幾個在下載就強制開始，開了沒速度的也刪。用 MoviePilot 已設定的下載器，單獨也能用。 |

## 安裝

MoviePilot「設定 → 系統 → 插件市場」加一行：

```
https://github.com/MiCat-S/Mi302-MoviePilot-Plugins
```

儲存後到「插件」頁，市場裡就會出現這兩個外掛，之後有新版也在市場裡更新。

說明見 Mi302 Wiki 的 [MoviePilot 整合](https://github.com/MiCat-S/Mi302/wiki/MoviePilot-整合)（[简体中文](https://github.com/MiCat-S/Mi302/wiki/MoviePilot-集成)、[English](https://github.com/MiCat-S/Mi302/wiki/MoviePilot)）。

---

[Mi302](https://github.com/MiCat-S/Mi302) 附带的两个 MoviePilot V3 插件，源码在 Mi302 仓库的 `moviepilot-plugin/`。安装：MoviePilot“设置 → 系统 → 插件市场”加一行 `https://github.com/MiCat-S/Mi302-MoviePilot-Plugins`，插件页的市场里就会出现“Mi302 整理助手”和“Mi302 清种助手”。

Two MoviePilot V3 plugins shipped with [Mi302](https://github.com/MiCat-S/Mi302); the source lives in `moviepilot-plugin/` of the Mi302 repository. To install, add `https://github.com/MiCat-S/Mi302-MoviePilot-Plugins` under MoviePilot's Settings → System → Plugin market; both plugins then appear in the market.
