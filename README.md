# PDF2HTML

PDF2HTML 是一个由 Codex 运行的本地工作流，用于把 Zotero 中的论文转换为面向电脑和平板横屏等宽屏设备的离线双语阅读页面。网页正式支持宽度不低于 `1024 CSS px` 的视口；更窄的窗口或手机仅尽力显示，不承诺布局效果，也不纳入验收。Zotero 始终保持只读，MinerU 在本地 NVIDIA GPU 上运行，由当前活跃的 Codex 会话提供忠实可靠的英文到简体中文翻译。

## 环境准备

```powershell
pixi install
pnpm install --frozen-lockfile
pnpm build
pixi run install-browser
pixi run pdf2html preflight
```

Python 与 MinerU 依赖由 Pixi 管理；Vue 前端只使用 pnpm。仓库已提交可直接运行的
Vue distribution，`pnpm build` 用于前端开发后刷新这些资源。

### 在 WSL 中运行

Windows 运行 Zotero，WSL 运行本仓库的 Pixi、MinerU 和阅读服务。此方式要求 WSL
使用镜像网络模式，并将 Windows 本地盘符按默认规则挂载到 `/mnt/<小写盘符>`。
在 WSL 终端执行上面的安装和预检命令；Zotero 需在 Windows 中运行并启用本地 API。
镜像网络下仍使用 `http://localhost:23119/api`，无需配置 Windows 主机 IP。
Zotero 返回的 `file:///C:/...` 附件地址会映射为 `/mnt/c/...`；附件所在盘必须已挂载，
且 WSL 用户能够读取该 PDF。MinerU 使用 WSL 中的 NVIDIA GPU，预检会检查 CUDA。
从 Windows 浏览器访问 WSL 中启动的阅读服务时，使用
`http://127.0.0.1:8765/`。

## 学习仓库位置

PDF2HTML 保存工具代码和 `.work/` 中的解析、布局及翻译工作状态；论文 bundle、笔记和
知识条目保存在外部独立 Git 学习仓库。两个仓库分别提交，不使用子模块或目录联接。

在工具仓库根目录创建不受 Git 跟踪的 `pdf2html.local.toml`：

```toml
knowledge_root = '../../Research/knowledge'
```

相对路径以工具仓库当前工作目录为基准，也可使用绝对路径，例如
`knowledge_root = 'C:/Users/hhj/Documents/Research/knowledge'`。配置优先级为
`PDF2HTML_KNOWLEDGE_ROOT` 环境变量、`pdf2html.local.toml`、旧的 `output/` 默认目录。
配置错误会报错；配置文件缺失且未设置环境变量时才使用旧默认值。新机器上应先恢复路径
配置，再执行构建或启动阅读器。

在 WSL 中，`knowledge_root` 必须写成 WSL 可访问的 Linux 路径，例如
`'/home/<user>/Research/knowledge'`；若学习仓库保存在 Windows 的 `C:` 盘，
则使用 `'/mnt/c/Users/<user>/Documents/Research/knowledge'`。

`build`、`verify` 和 `serve` 使用同一个 `<knowledge-root>/library`。学习仓库内部保留
`library/papers/`、`notes/`、`knowledge/`、`projects/`、`inbox/` 和 `templates/`，因此
论文与知识条目的相对链接不依赖 PDF2HTML 工具目录的位置。阅读器只暴露论文库，人工
笔记仍通过 Markdown 编辑器阅读。

迁移已有学习仓库时，应整体保留 `.git` 和内部目录，先记录文件哈希，再核对迁移后的
内容。已有 `.work/jobs/*/manifest.json` 中的 `bundle_path` 也须在核验映射后更新到新
位置，并重新运行 `verify`；仅改变配置不会自动迁移文件或重写任务记录。不要手工修改
生成的论文 bundle。`.work/` 中的审阅布局、翻译和来源副本仍需保留，以便复用和重建。

## Zotero 连接

预检（preflight）必须报告 Zotero API 版本 3、MinerU、Poppler、启用 CUDA 的 PyTorch 以及预期的 GPU。Zotero 必须处于运行状态，并启用 **设置（Settings）> 高级（Advanced）> 允许此计算机上的其他应用程序与 Zotero 通信（Allow other applications on this computer to communicate with Zotero）**。

## 论文工作流

每次只准备一个条目选择器（item selector）：

```powershell
pixi run pdf2html prepare --item "Exact Zotero title"
pixi run pdf2html prepare --item CI7QK99V
```

小批量任务也可以改用精确的合集（collection）或标签（tag）：

```powershell
pixi run pdf2html prepare --collection "Reading Queue"
pixi run pdf2html prepare --tag pdf2html
```

`prepare` 会将每份 PDF 复制到 `.work/jobs/` 下，运行 MinerU，执行保守的结构归一化，并生成源语言布局预览和 `layout_report.json`。此阶段不会生成翻译任务。Codex 必须先对照源 PDF、MinerU 块，以及桌面和 `1024 CSS px` 平板横屏宽度的预览完成版面审查；常见组合图和页注由确定性代码处理，特殊情况写入带哈希前置条件的 `layout_overrides.json`。

标题归一化会同时哈希 MinerU 的 content list 与 `middle.json` 行级结构。论文元数据标题是唯一的 `h1`，正文主节从 `h2` 开始；`layout_report.json` 的 `heading_outline` 会列出每个标题的原始层级、最终层级、对齐方式和判定来源。自动判定优先使用明确的十进制编号与可信 MinerU 层级，只有在栏内居中/左对齐证据形成稳定分组时才推断 `h2`/`h3`。

修改或新增 override 后重新生成预览：

```powershell
pixi run pdf2html layout --job <job-id>
```

`layout_report.json` 的 `override_targets` 为每个可操作块给出 `id`、`page`、`bbox` 和 `block_hash`。override 必须逐字复制这些前置条件，并同时绑定 PDF 与 MinerU 内容哈希：

```json
{
  "schema_version": 1,
  "job_id": "<job-id>",
  "source_sha256": "<manifest source.sha256>",
  "content_sha256": "<layout_report normalization.content_sha256>",
  "operations": [
    {
      "op": "group",
      "targets": [
        {"id": "<block-a>", "page": 0, "bbox": [0, 0, 0, 0], "block_hash": "<hash-a>"},
        {"id": "<block-b>", "page": 0, "bbox": [0, 0, 0, 0], "block_hash": "<hash-b>"}
      ],
      "caption_from": "<block-id>",
      "layout": "auto",
      "reason": "Confirmed against the source PDF"
    }
  ]
}
```

若子图标签已嵌入图片、未出现在 MinerU 图注中，人工核对 PDF 后可为 `group` 添加 `"labels": {"<block-a>": "(a)", "<block-b>": "(b)"}`。映射必须覆盖全部且仅包含分组目标，标签按几何阅读顺序从 `(a)` 连续排列，并与已有图注标签一致；`caption_from` 仍指定总图注来源，不修改源图注。

`move` 使用两个 target（移动块、锚点）以及 `before_id` 或 `after_id`；`reclassify` 使用一个 target 和受支持的 `kind`，改为 `heading` 时还必须提供 1–6 的 `level`；`drop` 使用一个 target 和必填原因。override 不能修改文本、图片路径、译文或 HTML，任何前置条件失配都会使整份文件失效。标题层级属于块指纹的一部分，因此刷新归一化规则后必须从最新报告复制 target。

当同一张图被拆成多个资产，而真实子图标签并不对应这些资产时，可在 `group` 中增加
`"source_crop": [90, 85, 910, 545]`。如果单个图块的资产裁掉了图内列标题或标签，也可使用
带 `source_crop` 的 `group` 修复：`targets` 只包含该图块，`caption_from` 指向同一图块，
裁剪范围包含该块的完整 bbox 及源 PDF 中缺失的图内内容。普通 `group` 仍须至少两个目标。
四个数为源 PDF 页面的归一化坐标
`[x0, y0, x1, y1]`，左上角为原点，各轴范围为 `0..1000`。必须对照源 PDF 确认裁剪范围
保留完整图内标签且不纳入总图注；范围必须有效、有限，并包含所有 target 的 bbox。
所有 target 仍须为同页图块，逐字保留当前报告中的页码、bbox 和块指纹，`caption_from`
必须指向其中具有完整图注的块。`source_crop` 不能与 `labels` 混用，也不能用于其他操作。

此分组会生成一个普通 `figure`。工具只读取作业中的 `source.pdf`，核对 SHA-256 后使用
`pdftoppm` 以 150 DPI 渲染该页并裁剪，保留源图真实标签。派生 PNG 使用内容哈希命名，写入
content list 同目录下独立的 `pdf2html-crops/`，不覆盖 MinerU 文件。图块记录源哈希、裁剪范围、
原始块和图注来源。`text_corrections.json` 仍使用原 `caption_from` 块的前置条件校正总图注。
CLI 的 `build` 和 `verify` 会对照已审阅文档与源 PDF 重现裁图；缺失媒体可以重建，已有媒体
内容失配则失败。离线 bundle 核验继续只依赖 bundle 本身。

源 PDF 证实 MinerU 文本识别有误时，在作业目录创建 `text_corrections.json`，逐项校正块的 `source` 或 `caption_source`。它与结构性 override 分开管理；每项使用最新 `layout_report.json` 中的 `override_targets` 作为目标，且必须填写核对原因：

```json
{
  "schema_version": 1,
  "job_id": "<job-id>",
  "source_sha256": "<manifest source.sha256>",
  "content_sha256": "<layout_report normalization.content_sha256>",
  "corrections": [
    {
      "target": {"id": "<block-id>", "page": 0, "bbox": [0, 0, 0, 0], "block_hash": "<hash>"},
      "field": "source",
      "value": "<checked text>",
      "reason": "Checked against source PDF page 1"
    }
  ]
}
```

对人工分组的总图注进行校正时，仍以分组前 `caption_from` 块的前置条件绑定 `caption_source`，校正会应用到对应组合图的总图注。修改后重新运行 `layout`、视觉审查和 `finalize-layout`。源文字变更会使对应译文失效，须补全新的翻译任务，再运行 `build` 与 `verify`；不得直接编辑 MinerU 输出或生成的论文 bundle。

预览通过后锁定结构并生成翻译任务：

```powershell
pixi run pdf2html finalize-layout --job <job-id>
```

`finalize-layout` 会按角色、源文本和源哈希复用唯一匹配的旧译文，归档旧翻译文件，并在 `translation_tasks/` 与 `translations/` 中写入和当前结构完全对齐的 JSON。Codex 只需填写剩余的 `target: null`，同时保留片段 ID、源文本、源哈希、公式、URL、DOI 字符串和引文标记。

使用以下命令完成一个任务（job）：

```powershell
pixi run pdf2html build --job <job-id>
pixi run pdf2html verify --job <job-id>
pixi run pdf2html status --job <job-id>
```

`build` 在 `<knowledge-root>/library/papers/<paper-id>/` 写入可重复生成的 Markdown
bundle：`paper.en.md`、`paper.zh-CN.md`、自包含的 `paper.json` 和内容寻址的
`media/`。它不会在论文库目录生成 HTML。首次成功构建还会在
`<knowledge-root>/library/shelf/<paper-id>.json` 独立记录 UTC 入库时间 `added_at`，
重建保留原值，不改变论文内容及其 revision。`source` 为 `built` 的记录来自首次构建；
`git_archive` 来自首次 Git 归档时间，是历史论文入库时间的近似值。缺失记录的论文
不推定入库时间。

启动只读本地书架：

```powershell
pixi run pdf2html serve --port 8765
```

在当前环境生成视觉检查用 PNG（PDF 页码从 1 开始）：

```bash
pixi run pdf2html render-pdf --pdf .work/jobs/<job-id>/source.pdf --page 6 --dpi 150 --output /tmp/source-p6.png
pixi run pdf2html render-web --url file:///absolute/path/to/layout_preview/index.html --output /tmp/layout.png --width 1440 --selector '#<block-id>'
pixi run pdf2html render-web --url http://127.0.0.1:8765/papers/<paper-id>/ --output /tmp/reader.png --width 1024 --wait-for .paper-body --selector '#<block-id>'
```

`render-web` 使用当前系统的 Playwright 无头 Chromium。首次使用时运行 `pixi run install-browser` 下载对应系统的浏览器；WSL 内会使用 Linux 浏览器。`--selector` 只截取指定页面元素；省略它时截取视口，加入 `--full-page` 时截取整页。`--height` 设置视口高度。

服务默认监听 `0.0.0.0`，本机打开 `http://127.0.0.1:8765/`，内网设备使用
`http://<本机内网 IP>:8765/`。需要限定地址时使用 `--host`，例如
`pixi run pdf2html serve --host 127.0.0.1 --port 8765`。书架支持按入库时间、发表日期
及标题排序，默认最近入库在前；时间缺失项排在末尾。搜索过滤后的论文卡片使用同一
排序方式，搜索命中条目按相关性排列。服务只读取论文和书架元数据，提供 Vue
书架、双语阅读器和中英文全文搜索。Markdown、sidecar 和媒体也可通过每篇论文的
`/papers/<paper-id>/` 路由读取；服务只允许 `paper.json` 与 Vite manifest 中列出的
文件，不提供任意文件系统访问。

## 质量检查

```powershell
pixi run lint
pixi run typecheck
pixi run test
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

确定性校验会直接检查 bundle schema、canonical revision、Document/译文重建、
Markdown AST、受保护 token、公式、块/段落标记、图形组、媒体哈希和路径安全，
并通过应用内 HTTP 请求验证 shelf、Markdown、JSON、media、assets、全文搜索与
ETag/304。任何失效的 override 都会阻止布局锁定。此外，Codex 会将源 PDF 页面与
Vue 阅读器进行对比，并在交付前分别以桌面宽度和 `1024 CSS px` 平板横屏宽度检查站点。
