# PDF2HTML Codex workflow

This repository is operated by Codex for the user. A paper request is complete
only after the Zotero PDF has been parsed, translated, built, and visually
verified.

## Safety and source rules

- Treat Zotero as read-only. Never write items, tags, notes, or attachments.
- Use the Zotero Local API at `http://localhost:23119/api`; never read
  `zotero.sqlite` directly.
- Copy an attachment into `.work/` before passing it to MinerU. Never modify the
  Zotero-managed file.
- Keep `.work/`, `output/`, and model files untracked.
- PDF parsing is local. Translation text is sent only through the active Codex
  session; do not add a translation API or credentials.

## Required paper workflow

1. Run `pixi run pdf2html preflight`.
2. Run one selector, for example
   `pixi run pdf2html prepare --item "Exact paper title"`.
3. Compare the source PDF, MinerU blocks, `layout_report.json`, and the reported
   source-only layout preview at desktop and mobile widths. Do not edit preview
   or final HTML.
4. If deterministic normalization is insufficient, create
   `layout_overrides.json` using only validated `group`, `move`, `reclassify`,
   and `drop` operations, then run `pixi run pdf2html layout --job <job-id>` and
   review the refreshed preview. Never weaken or bypass stale hash, page, bbox,
   or block fingerprint checks.
5. Run `pixi run pdf2html finalize-layout --job <job-id>` only after visual
   review. This locks the reviewed layout and creates translation tasks.
6. Read every JSON file under the reported `translation_tasks` directory.
7. Create or complete corresponding files under `translations`, preserving every segment
   ID and `source_hash`, and replace each `target: null` with a faithful
   Simplified Chinese translation. Keep prefilled reused targets unchanged.
8. Do not translate formulas, code, URLs, citation markers, or bibliography
   entries. Preserve protected tokens exactly. Use consistent terminology and
   include the English term in parentheses on its first material occurrence.
9. Run `pixi run pdf2html build --job <job-id>` and then
   `pixi run pdf2html verify --job <job-id>`.
10. Render representative PDF pages and inspect the final page at desktop and
   mobile widths. Fix defects before reporting completion.

For a collection or tag, finish papers sequentially. A failure in one paper
must not discard completed jobs. Reuse existing parse and translation artifacts
when their recorded source hashes still match.

# 代码编写与提交
- 在任务完成后创建git提交
