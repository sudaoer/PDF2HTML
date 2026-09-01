# PDF2HTML

PDF2HTML 是一个由 Codex 运行的本地工作流，用于把 Zotero 中的论文转换为响应式（responsive）的离线双语阅读页面。Zotero 始终保持只读，MinerU 在本地 NVIDIA GPU 上运行，由当前活跃的 Codex 会话提供忠实可靠的英文到简体中文翻译。

## 环境准备

```powershell
pixi install
pixi run pdf2html preflight
```

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

`prepare` 会将每份 PDF 复制到 `.work/jobs/` 下，运行 MinerU，执行保守的结构归一化，并生成源语言布局预览和 `layout_report.json`。此阶段不会生成翻译任务。Codex 必须先对照源 PDF、MinerU 块和桌面/移动预览完成版面审查；常见组合图和页注由确定性代码处理，特殊情况写入带哈希前置条件的 `layout_overrides.json`。

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

`move` 使用两个 target（移动块、锚点）以及 `before_id` 或 `after_id`；`reclassify` 使用一个 target 和受支持的 `kind`；`drop` 使用一个 target 和必填原因。override 不能修改文本、图片路径、译文或 HTML，任何前置条件失配都会使整份文件失效。

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

离线书架（offline shelf）写入 `output/library/index.html`。每篇论文位于 `output/library/papers/<paper-id>/` 下，且只使用相对路径的本地资源。

## 质量检查

```powershell
pixi run lint
pixi run typecheck
pixi run test
```

确定性校验（deterministic verification）会检查翻译完整性、受保护标记（protected tokens）、缺失媒体、未解析的模板标记（template markers）、远程运行时依赖，以及空正文、孤立子图标题、正文页注和损坏的组合图。任何失效的 override 都会阻止布局锁定。此外，Codex 会将渲染出的 PDF 页面与生成的阅读器进行对比，并在交付前分别以桌面端和移动端宽度检查站点。
