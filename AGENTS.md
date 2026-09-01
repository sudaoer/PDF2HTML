# PDF2HTML Codex 工作流

本仓库由 Codex 代用户维护。只有在 Zotero PDF 已完成解析、翻译、构建和视觉核验后，
一项论文处理任务才算完成。

## 仓库职责与语言

- 根仓库是 PDF2HTML 工具仓库，保存源代码、测试、前端资源和工作流文档。
- `output/` 在根仓库中保持忽略，但其内部是独立的 Git 学习仓库，保存已验证的论文
  bundle、人工笔记和知识条目。不要把它改成根仓库的普通受跟踪目录或子模块。
- 说明文档、人工笔记、知识条目和提交信息使用简体中文。
- 命令、路径、代码标识符、公式、DOI、URL、英文原文、引用标记和受保护术语保持原样。

## 安全与来源规则

- 将 Zotero 视为只读数据源，不得写入条目、标签、笔记或附件。
- 只使用位于 `http://localhost:23119/api` 的 Zotero Local API，禁止直接读取
  `zotero.sqlite`。
- 将附件复制到 `.work/` 后才能交给 MinerU，绝不修改 Zotero 管理的文件。
- 在根仓库中保持 `.work/`、`output/` 和模型文件不受跟踪。
- PDF 解析必须在本地进行。译文只能通过当前活跃的 Codex 会话处理，不得增加翻译
  API、密钥或凭据。

## 支持的显示范围

- 阅读器面向电脑和平板横屏，正式支持的最小视口宽度为 `1024 CSS px`，包含该宽度。
- 更窄的窗口和手机仅尽力显示，不得把它们作为质量验收目标，也不得加入手机专用布局
  约束。
- 图、表和公式仍须保留与设备无关的换行和局部溢出保护。

## 必须执行的论文工作流

1. 运行 `pixi run pdf2html preflight`。
2. 运行且只运行一个选择器，例如
   `pixi run pdf2html prepare --item "Exact paper title"`。
3. 对照源 PDF、MinerU 块、`layout_report.json`（包括 `heading_outline` 中的每一项）
   以及命令报告的纯源语言布局预览，在桌面宽度和 `1024 CSS px` 平板横屏宽度下进行
   检查。确认元数据标题是唯一的 H1，正文主节从 H2 开始，子节不跳级。不得直接编辑
   预览 HTML 或最终 HTML。
4. 如果确定性归一化仍不足，创建 `layout_overrides.json`，且只能使用经过核验的
   `group`、`move`、`reclassify` 和 `drop` 操作。随后运行
   `pixi run pdf2html layout --job <job-id>` 并检查刷新后的预览。不得削弱或绕过过期
   哈希、页码、bbox 或块指纹检查。将 `reclassify` 的目标设为 `heading` 时，必须
   包含经人工复核、范围为 1 至 6 的 `level`。
5. 只有完成视觉检查后，才能运行
   `pixi run pdf2html finalize-layout --job <job-id>`。该操作会锁定已审阅布局并创建
   翻译任务。
6. 阅读命令报告的 `translation_tasks` 目录下每一个 JSON 文件。
7. 在 `translations` 下创建或补全对应文件，保留每个片段的 ID 和 `source_hash`，
   将每个 `target: null` 替换为忠实的简体中文译文。已经预填并复用的译文不得改动。
8. 不翻译公式、代码、URL、引用标记或参考文献条目。受保护 token 必须逐字保留。
   术语须保持一致，并在第一次实质性出现时在中文术语后附英文原词。
9. 先运行 `pixi run pdf2html build --job <job-id>`，再运行
   `pixi run pdf2html verify --job <job-id>`。
10. 渲染有代表性的 PDF 页面，并在桌面宽度和 `1024 CSS px` 平板横屏宽度下检查最终
    页面。发现缺陷后必须修复并重新核验，才能报告完成。

处理合集或标签时必须逐篇完成。一篇论文失败不得丢弃已经完成的任务；当记录的源哈希
仍然一致时，应复用现有解析和翻译产物。

## 学习仓库管理

- `output/library/papers/<paper-id>/` 是 PDF2HTML 自动生成并验证的证据层，不得手工
  编辑、重命名或删除其中的 `paper.en.md`、`paper.zh-CN.md`、`paper.json` 或
  `media/`。
- 人工知识内容写入 `output/notes/`、`output/knowledge/`、`output/projects/`、
  `output/inbox/` 和 `output/templates/` 等非生成目录；仓库说明、维护规则和论文
  索引可在对应位置更新。所有修改均须遵循 `output/AGENTS.md`。
- 关键知识结论必须使用标准 Markdown 相对链接，指向论文 bundle 中的具体块锚点；
  无法使用块锚点时须注明 PDF 页码。
- 每项关键证据必须明确标为“论文报告”“个人推断”或“已复现”。未经人工审阅的模型
  输出不得标记为已审阅知识。
- 新论文通过构建、确定性校验和视觉检查后，应更新论文索引；不应为尚未阅读的论文
  自动生成结论性笔记。

## 代码、验证与提交

- 修改代码后运行与改动风险相称的检查，不得用跳过、降级或删除测试的方式换取通过。
- 提交前分别检查根仓库和 `output` 仓库的状态，保留用户已有且与当前任务无关的改动。
- 论文产物通过验证后，应及时在 `output` 仓库单独提交；工具代码和根文档在任务完成
  后于根仓库提交。
- 两个仓库的提交必须相互独立，提交信息使用简体中文。
- 除非用户明确要求，不得推送远程、创建标签或发布版本。
