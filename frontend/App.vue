<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import PaperBlock from "./PaperBlock.vue";
import { renderRich } from "./rendering";
import type {
  PaperBundle,
  ReaderMode,
  SearchResponse,
  SearchResult,
  ShelfResponse,
} from "./types";

function savedTheme(): string {
  try {
    return window.localStorage?.getItem("pdf2html-theme") || "light";
  } catch {
    return "light";
  }
}

const paperMatch = window.location.pathname.match(/^\/papers\/([^/]+)\/$/);
const paperId = paperMatch?.[1] ?? "";
const loading = ref(true);
const error = ref("");
const shelf = ref<ShelfResponse | null>(null);
const bundle = ref<PaperBundle | null>(null);
const query = ref("");
const matches = ref<string[] | null>(null);
const searchResults = ref<SearchResult[]>([]);
const mode = ref<ReaderMode>("bilingual");
const readerSize = ref(18);
const theme = ref(savedTheme());
const progress = ref(0);
const dialogImage = ref<{ src: string; alt: string } | null>(null);
let searchController: AbortController | null = null;

const translations = computed<Record<string, string>>(() =>
  Object.fromEntries(bundle.value?.translations.map((item) => [item.id, item.target]) ?? []),
);
const media = computed<Record<string, string>>(() =>
  Object.fromEntries(bundle.value?.media.map((item) => [item.asset, item.path]) ?? []),
);
const visiblePapers = computed(() => {
  const papers = shelf.value?.papers ?? [];
  if (matches.value === null) return papers;
  const ids = new Set(matches.value);
  return papers.filter((paper) => ids.has(paper.paper_id));
});
const visibleBlocks = computed(() => {
  const document = bundle.value?.document;
  if (!document) return [];
  const bodyBlocks = document.blocks.filter(
    (block) => !["document_title", "abstract_heading", "abstract_body"].includes(
      String(block.extra.front_matter_role ?? ""),
    ),
  );
  const introduction = bodyBlocks.findIndex(
    (block) =>
      block.kind === "heading" &&
      block.source.replace(/^(?:\d+(?:\.\d+)*\.?|[IVXLCDM]+\.)\s*/, "").trim().toLowerCase() === "introduction",
  );
  if (introduction < 0) return bodyBlocks;
  const abstract = bodyBlocks.findIndex(
    (block, index) =>
      index < introduction && block.kind === "paragraph" && /^Abstract\b/i.test(block.source),
  );
  if (!document.metadata.abstract && abstract < 0) return bodyBlocks;
  return [
    ...bodyBlocks.slice(0, introduction).filter((block, index) =>
        (!document.metadata.abstract && index >= abstract) ||
        String(block.extra.front_matter_role ?? "") === "keywords" ||
        (block.kind === "paragraph" && block.source.toLowerCase().includes("http")),
    ),
    ...bodyBlocks.slice(introduction),
  ];
});
const toc = computed(() => visibleBlocks.value.filter((block) => block.kind === "heading"));

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { message?: string };
    throw new Error(payload.message || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    if (paperId) {
      bundle.value = await fetchJson<PaperBundle>(`/papers/${paperId}/paper.json`);
      const sourceTitle = bundle.value.document.metadata.title;
      const targetTitle = bundle.value.translations.find((item) => item.id === "meta.title")?.target;
      document.title = targetTitle ? `${targetTitle} · ${sourceTitle}` : sourceTitle;
    } else {
      shelf.value = await fetchJson<ShelfResponse>("/api/shelf");
      document.title = "双语论文书架";
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    loading.value = false;
  }
}

async function search(): Promise<void> {
  const value = query.value.trim();
  searchController?.abort();
  if (!value) {
    matches.value = null;
    searchResults.value = [];
    return;
  }
  searchController = new AbortController();
  try {
    const response = await fetchJson<SearchResponse>(
      `/api/search?q=${encodeURIComponent(value)}&limit=50`,
      searchController.signal,
    );
    searchResults.value = response.results;
    matches.value = [...new Set(response.results.map((item) => item.paper_id))];
  } catch (reason) {
    if (!(reason instanceof DOMException && reason.name === "AbortError")) {
      error.value = reason instanceof Error ? reason.message : String(reason);
    }
  }
}

function updateProgress(): void {
  const available = document.documentElement.scrollHeight - window.innerHeight;
  progress.value = available > 0 ? Math.min(100, (window.scrollY / available) * 100) : 0;
}

function applyTheme(): void {
  document.documentElement.dataset.theme = theme.value;
  try {
    window.localStorage?.setItem("pdf2html-theme", theme.value);
  } catch {
    // Storage can be unavailable in hardened or file-like browser contexts.
  }
}

watch(query, () => void search());
watch(theme, applyTheme);
onMounted(() => {
  applyTheme();
  window.addEventListener("scroll", updateProgress, { passive: true });
  window.addEventListener("resize", updateProgress);
  void load();
});
onBeforeUnmount(() => {
  searchController?.abort();
  window.removeEventListener("scroll", updateProgress);
  window.removeEventListener("resize", updateProgress);
});
</script>

<template>
  <div v-if="loading" class="app-state" role="status">正在加载论文库…</div>
  <main v-else-if="error" class="app-state error-state">
    <p class="eyebrow">Unable to load</p>
    <h1>无法加载内容</h1>
    <p>{{ error }}</p>
    <a href="/">返回书架</a>
  </main>

  <main v-else-if="!paperId && shelf" class="shelf shelf-page">
    <header class="shelf-hero">
      <p class="eyebrow">Local bilingual library</p>
      <h1>双语论文书架</h1>
      <p>由 Zotero、MinerU 与 Codex 生成的本地流式阅读版本。</p>
      <label class="search-box">
        <span>搜索</span>
        <input v-model="query" type="search" placeholder="标题、作者、年份、标签或正文">
      </label>
    </header>
    <section v-if="query" class="search-results" aria-live="polite">
      <article v-for="result in searchResults" :key="`${result.paper_id}:${result.block_id}`" class="search-hit">
        <p class="paper-year">
          {{ result.page ? `PDF ${result.page}` : "论文信息" }} · {{ result.kind }}
        </p>
        <h2>
          <a :href="`/papers/${result.paper_id}/${result.block_id ? `#${result.block_id}` : ''}`">
            {{ result.title_zh }}
          </a>
        </h2>
        <p v-if="result.target_excerpt" lang="zh-CN">{{ result.target_excerpt }}</p>
        <p v-if="result.source_excerpt" class="card-original" lang="en">{{ result.source_excerpt }}</p>
      </article>
      <p v-if="!searchResults.length" class="empty-state">没有匹配的内容。</p>
    </section>
    <section class="paper-grid" aria-live="polite">
      <article v-for="paper in visiblePapers" :key="paper.paper_id" class="paper-card">
        <p class="paper-year">{{ paper.date }}</p>
        <h2><a :href="paper.href">{{ paper.title_zh }}</a></h2>
        <p class="card-original" lang="en">{{ paper.title }}</p>
        <p class="card-authors">{{ paper.creators.join(", ") }}</p>
        <p v-if="paper.publication" class="card-publication">{{ paper.publication }}</p>
      </article>
      <article v-for="item in shelf.errors" :key="item.paper_id" class="paper-card broken-card">
        <p class="paper-year">损坏的 bundle</p>
        <h2>{{ item.paper_id }}</h2>
        <p>{{ item.message }}</p>
      </article>
      <p v-if="!visiblePapers.length && !shelf.errors.length" class="empty-state">
        {{ query ? "没有匹配的内容。" : "还没有完成的论文。" }}
      </p>
    </section>
  </main>

  <div
    v-else-if="bundle"
    class="paper-page"
    :class="`mode-${mode}`"
    :style="{ '--reader-size': `${readerSize}px` }"
  >
    <header class="reader-bar">
      <a class="shelf-link" href="/" aria-label="返回论文书架">← 书架</a>
      <div class="reader-controls" role="group" aria-label="阅读设置">
        <button
          v-for="item in ([['bilingual', '双语'], ['target', '中文'], ['source', '原文']] as const)"
          :key="item[0]"
          type="button"
          :aria-pressed="mode === item[0]"
          @click="mode = item[0]"
        >
          {{ item[1] }}
        </button>
        <button type="button" aria-label="缩小字号" @click="readerSize = Math.max(15, readerSize - 1)">A−</button>
        <button type="button" aria-label="放大字号" @click="readerSize = Math.min(24, readerSize + 1)">A+</button>
        <button type="button" aria-label="切换明暗主题" @click="theme = theme === 'dark' ? 'light' : 'dark'">◐</button>
      </div>
      <div class="reading-progress" aria-hidden="true"><span :style="{ width: `${progress}%` }" /></div>
    </header>
    <div class="reader-layout">
      <aside class="toc-panel" aria-label="论文目录">
        <p class="eyebrow">Contents</p>
        <nav>
          <a v-for="block in toc" :key="block.id" :href="`#${block.id}`" :class="`toc-level-${block.level}`">
            <span lang="zh-CN">{{ translations[block.segment_id] }}</span>
            <small lang="en">{{ block.source }}</small>
          </a>
        </nav>
      </aside>
      <main class="paper" id="main-content">
        <article>
          <header class="paper-hero">
            <p class="eyebrow">Bilingual paper · 双语论文</p>
            <h1 lang="zh-CN">{{ translations['meta.title'] }}</h1>
            <p class="original-title" lang="en">{{ bundle.document.metadata.title }}</p>
            <p class="byline">{{ [...new Set(bundle.document.metadata.creators)].join(", ") }}</p>
            <dl class="paper-meta">
              <div v-if="bundle.document.metadata.publication"><dt>来源</dt><dd>{{ bundle.document.metadata.publication }}</dd></div>
              <div v-if="bundle.document.metadata.date"><dt>日期</dt><dd>{{ bundle.document.metadata.date }}</dd></div>
              <div v-if="bundle.document.metadata.doi"><dt>DOI</dt><dd>{{ bundle.document.metadata.doi }}</dd></div>
            </dl>
            <section v-if="bundle.document.front_matter_notes.length" class="paper-front-notes">
              <div v-for="note in bundle.document.front_matter_notes" :key="note.id" class="front-note pair">
                <div class="target" lang="zh-CN"><sup v-if="note.marker">{{ note.marker }}</sup><span v-html="renderRich(translations[note.segment_id] || note.source)" /></div>
                <div class="source" lang="en"><sup v-if="note.marker">{{ note.marker }}</sup><span v-html="renderRich(note.source)" /></div>
              </div>
            </section>
          </header>
          <section v-if="bundle.document.metadata.abstract" id="abstract" class="abstract pair">
            <div class="target" lang="zh-CN"><h2>摘要</h2><div v-html="renderRich(translations['meta.abstract'])" /></div>
            <div class="source" lang="en"><h2>Abstract</h2><div v-html="renderRich(bundle.document.metadata.abstract)" /></div>
          </section>
          <section class="paper-body">
            <PaperBlock
              v-for="block in visibleBlocks"
              :key="block.id"
              :block="block"
              :mode="mode"
              :paper-id="bundle.paper_id"
              :translations="translations"
              :media="media"
              @open-image="(src, alt) => (dialogImage = { src, alt })"
            />
          </section>
          <section v-if="bundle.document.endnotes.length" class="paper-endnotes">
            <h2>注释 <span lang="en">Notes</span></h2>
            <div v-for="note in bundle.document.endnotes" :id="note.id" :key="note.id" class="endnote pair">
              <div class="target" lang="zh-CN"><sup>{{ note.marker }}</sup>{{ translations[note.segment_id] || note.url }}</div>
              <div class="source" lang="en"><sup>{{ note.marker }}</sup>{{ note.url || note.source }}</div>
              <span class="endnote-page">PDF {{ note.page + 1 }}</span>
            </div>
          </section>
        </article>
      </main>
    </div>
    <div v-if="dialogImage" class="image-backdrop" role="dialog" aria-modal="true" @click.self="dialogImage = null">
      <button type="button" aria-label="关闭图片" @click="dialogImage = null">×</button>
      <img :src="dialogImage.src" :alt="dialogImage.alt">
    </div>
  </div>
</template>
