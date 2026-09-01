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

`prepare` 会将每份 PDF 复制到 `.work/jobs/` 下，运行 MinerU，并在 `translation_tasks/` 中生成 JSON 文件。Codex 会把这些文件复制到同级的 `translations/` 目录，并填写每个 `target`，同时保留片段 ID、源文本、源哈希、公式、URL、DOI 字符串和引文标记。

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

确定性校验（deterministic verification）会检查翻译完整性、受保护标记（protected tokens）、缺失媒体、未解析的模板标记（template markers）以及远程运行时依赖。此外，Codex 会将渲染出的 PDF 页面与生成的阅读器进行对比，并在交付前分别以桌面端和移动端宽度检查站点。
