# TSP 桌面版 — 续接说明（给 AI 助手看）

> 换电脑 / 换 AI 会话时，把本文档贴给 AI，或直接让它 clone 本仓库后读 `HANDOFF.md`，即可无缝续上。
> 最后更新：2026-09-16

---

## 0. 一句话现状

开源项目 `shy3130/tick-stock-panel`（TSP，A股智能量化工作台）已改造为 **Windows 双击安装包**，
并接入 **AKShare / Tushare 免费数据源**。**云端构建已成功产出 `TickFlowStockPanel-Setup-x64.exe`（128.52 MB）**。

---

## 1. 代码在哪

| 位置 | 说明 |
| :--- | :--- |
| **GitHub（权威源）** | `https://github.com/baixiaobai-AI/tick-stock-panel`（public，分支 `main`） |
| 原机器本地 | `D:\Programs\TSP\tick-stock-panel` |
| 最新 Release | `https://github.com/baixiaobai-AI/tick-stock-panel/releases/tag/v0.1.0` |

**换电脑后不要依赖原机器的本地目录**，一律以 GitHub 为准。

---

## 2. 已完成的工作

1. **AKShare 数据源插件**（零 Key，东方财富公开接口）
   `backend/app/plugins/akshare/` — 日K / 复权因子 / 全市场实时；股票列表取自 `stock_info_a_code_name`，失败回退 TickFlow。
2. **Tushare 数据源插件**（需 Token，免费积分够用）
   `backend/app/plugins/tushare/` — 日K / 复权因子；`amount` 千元→元已 ×1000。实时行情建议留 AKShare。
3. **修复打包致命 bug** — `packaging/tickflow.spec`
   原文件在模块顶层先调用 `_safe_metadata()` 后定义，PyInstaller 构建时 `NameError` 直接崩。已把定义提到最前（现为第 31 行定义、96/131 行调用）。
4. **CI 自包含补丁** — `.github/workflows/release.yml`
   加了 `push: branches: [main]` 自动触发；并在 PyInstaller 之前安装 akshare/tushare 依赖，否则 exe 缺库启动即崩。
5. **补 `tiers.yaml`**（项目根）— 无 Key 免费模式可正常启动并拉历史日K。
6. **v0.1.0 构建成功**（GitHub Actions，约 9 分钟）。旧文档列的 4 条"待验证风险"（polars 原生库、Inno Setup、uv sync 兼容性、插件依赖收集）**真机全过**。

---

## 3. 新电脑怎么开始（重点看这节）

### 3.1 取代码

- **能正常访问 github.com 的环境**（普通电脑 / WorkBuddy 客户端）：
  ```
  git clone https://github.com/baixiaobai-AI/tick-stock-panel.git
  ```
- **沙盒环境（github.com 被屏蔽时）**：`github.com:443` 直连超时、走代理 CONNECT 也 502，
  但 **`codeload.github.com` 是通的**，可下载 tarball：
  ```
  https://codeload.github.com/baixiaobai-AI/tick-stock-panel/tar.gz/refs/heads/main
  ```

### 3.2 推送改动 —— 本沙盒 `git push` 不通，必须用 API

**现象**：`github.com` 域名被网络策略挡住（直连超时 / 代理 502），
而 `api.github.com`、`codeload.github.com` 通。**所以 git 的 HTTPS/SSH 推送走不通，别在这上面浪费时间。**

**可行方案**：全程用 GitHub Git Data API（走 `api.github.com`），仓库里已备好现成脚本：

- `tools/gh_update.ps1` —— **日常改代码就用这个**（增量同步，只上传变化的文件，约 10 秒）
- `tools/gh_tree.ps1` —— 全量建树+提交（首次初始化用，日常不必）
- `tools/gh_upload.ps1` —— 全量建 blob（已过时，仅作参考）

用法：
```powershell
powershell -ExecutionPolicy Bypass -File tools/gh_update.ps1 `
  -Owner baixiaobai-AI -Repo tick-stock-panel `
  -Token <PAT> `
  -Root "<本地仓库路径>" `
  -Message "改动说明"        # 加 -DryRun 可只看差异不上传
```
内部流程：读远端 HEAD + 递归树 → 本地算 SHA（`sha1("blob <len>\0"+内容)`）→ 只上传差异 blob →
`base_tree` 分批重建整树 → 建提交（parent = 远端 HEAD）→ `PATCH /git/refs/heads/main`。
删除的文件无需特殊处理（树由本地文件全集重建）。
API 建的提交**同样会触发 push 事件和 Actions**。

### 3.3 需要用户提供的东西

- **GitHub Personal Access Token**，需勾选 `repo` + `workflow`（没有 `workflow` 推 workflow 文件会被拒）。
- 用户名：`baixiaobai-AI`
- ⚠️ token 不要写进任何文件，用完可在 <https://github.com/settings/tokens> 吊销。

---

## 4. 改功能的流程

1. 用户用大白话描述需求（如"选股结果能导出 Excel""默认数据源改成 AKShare"）。
2. AI 改 `本地仓库` 的源码。
3. AI 跑 `tools/gh_update.ps1` 增量推送（约 10 秒）。
4. 云端 `桌面客户端发布` workflow 自动跑（约 9 分钟）。
5. 从 Releases 拿新 `TickFlowStockPanel-Setup-x64.exe` 给用户，覆盖安装即可，**数据不丢**（数据在安装目录 `data/`）。

数据源插件机制：`backend/app/plugins/<name>/` 下放 `plugin.yaml` + `provider.py`，
实现 `get_instruments / get_daily / get_adj_factors / get_realtime` 即可；缺哪个数据集会自动回退 TickFlow。

---

## 5. 关键文件速查

| 文件 | 作用 |
| :--- | :--- |
| `packaging/tickflow.spec` | PyInstaller 打包配置（已修 NameError） |
| `packaging/tickflow.iss` | Inno Setup 安装包脚本 |
| `.github/workflows/release.yml` | 云端 Windows 构建（已加 push 自动触发 + 插件依赖） |
| `tiers.yaml` | 免费档位能力声明 |
| `backend/app/plugins/akshare/` | 零 Key 免费数据源 |
| `backend/app/plugins/tushare/` | Tushare 免费积分数据源 |
| `frontend/` | 前端（Vite + pnpm，CI 用 `--frozen-lockfile`） |
| `tools/*.ps1` | 走 API 推送的脚本（见 3.2） |

---

## 6. 踩过的坑（别重蹈覆辙）

1. **空仓库调 `POST /git/blobs` 返回 409** —— 先用 contents API 建一个初始提交把仓库"激活"，之后正常。
2. **一次性提交 781 条 tree 会 422**（"input was too large"）—— 必须用 `base_tree` 分批追加，每批 250 条。
3. **PowerShell 日志拼接坑**：`Log "x" + $y` 会先求值函数调用，只传入第一段，导致报错信息被吞、排查白跑很多轮。
   必须写 `Log "x=$($y)"` 或 `Log ("x" + $y)`。
4. **PortableGit 的 `cmd\git.exe` 缺 remote-helper**（报 `'remote-https' is not a git command`）——
   网络操作要用 `mingw64\bin\git.exe` 并设 `GIT_EXEC_PATH=mingw64\bin`（本地 init/add/commit 用哪个都行）。
5. **PowerShell 工具的 stdout 不回显** —— 把结果写进文本文件再用 Read 工具读。
6. 旧文档里"AI 无法代推、必须用户自己用 GitHub Desktop"的结论**已不成立**，见 3.2。

---

## 7. 装好 exe 后怎么用免费数据源

软件内：设置 → 数据源

- **AKShare**：零 Key，直接可用（日K / 复权 / 实时来自东方财富）
- **Tushare**：填 tushare.pro 注册的 Token（免费积分够拉日K / 复权）
- 实时监控的 realtime 数据源建议设为 **AKShare**（免费无需 token）
