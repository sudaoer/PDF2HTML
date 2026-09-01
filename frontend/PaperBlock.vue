<script setup lang="ts">
import { computed } from "vue";

import { renderEquation, renderRich } from "./rendering";
import type { DocumentBlock, ReaderMode } from "./types";

const props = defineProps<{
  block: DocumentBlock;
  mode: ReaderMode;
  paperId: string;
  translations: Record<string, string>;
  media: Record<string, string>;
}>();
const emit = defineEmits<{ openImage: [src: string, alt: string] }>();

const target = computed(() => props.translations[props.block.segment_id] ?? "");
const captionTarget = computed(
  () => props.translations[props.block.caption_segment_id] ?? "",
);
const footnoteTarget = computed(
  () => props.translations[props.block.footnote_segment_id] ?? "",
);
const tableContent = computed(() =>
  props.mode === "target" ? target.value : props.block.source,
);
const tableLanguage = computed(() => (props.mode === "target" ? "zh-CN" : "en"));
const tableModeClass = computed(() => (props.mode === "target" ? "target" : "source"));
const headingLevel = computed(() => Math.min(6, Math.max(1, props.block.level || 1)));
const heading = computed(() => `h${headingLevel.value}`);
const pages = computed(() => {
  const raw = props.block.extra.origin_pages;
  const values = Array.isArray(raw) && raw.length ? raw.map(Number) : [props.block.page];
  const start = Math.min(...values) + 1;
  const end = Math.max(...values) + 1;
  return start === end ? `PDF ${start}` : `PDF ${start}-${end}`;
});
const media = (asset: string) => {
  const path = props.media[asset];
  return path ? `/papers/${props.paperId}/${path}` : "";
};
</script>

<template>
  <section
    v-if="block.kind === 'heading'"
    :id="block.id"
    class="heading-pair pair"
    :class="`heading-level-${headingLevel}`"
    :data-page="block.page + 1"
  >
    <div class="target" lang="zh-CN">
      <component :is="heading" v-html="renderRich(target)" />
    </div>
    <div class="source" lang="en">
      <component :is="heading" v-html="renderRich(block.source)" />
    </div>
  </section>

  <figure
    v-else-if="block.kind === 'figure_group'"
    :id="block.id"
    class="figure-group"
    :class="`layout-${block.layout}`"
    :data-page="block.page + 1"
    :data-members="block.members.length"
    :style="{ '--figure-columns': block.members.length }"
  >
    <div class="figure-members">
      <div v-for="member in block.members" :key="member.id" class="figure-member">
        <button
          class="figure-open"
          type="button"
          @click="emit('openImage', media(member.asset), `论文子图 ${member.label}`)"
        >
          <img :src="media(member.asset)" :alt="`论文子图 ${member.label}`" loading="lazy">
        </button>
        <span class="subfigure-label" lang="en">{{ member.label }}</span>
      </div>
    </div>
    <figcaption class="pair">
      <div class="target" lang="zh-CN" v-html="renderRich(captionTarget)" />
      <div class="source" lang="en" v-html="renderRich(block.caption_source)" />
    </figcaption>
    <div v-if="block.footnote_source" class="figure-note pair">
      <div class="target" lang="zh-CN" v-html="renderRich(footnoteTarget)" />
      <div class="source" lang="en" v-html="renderRich(block.footnote_source)" />
    </div>
    <span class="page-marker">{{ pages }}</span>
  </figure>

  <figure v-else-if="block.kind === 'figure'" :id="block.id" :data-page="block.page + 1">
    <button
      v-if="block.asset"
      class="figure-open"
      type="button"
      @click="emit('openImage', media(block.asset), `论文图，第 ${block.page + 1} 页`)"
    >
      <img :src="media(block.asset)" :alt="`论文图，第 ${block.page + 1} 页`" loading="lazy">
    </button>
    <figcaption v-if="block.caption_source" class="pair">
      <div class="target" lang="zh-CN" v-html="renderRich(captionTarget)" />
      <div class="source" lang="en" v-html="renderRich(block.caption_source)" />
    </figcaption>
    <div v-if="block.footnote_source" class="figure-note pair">
      <div class="target" lang="zh-CN" v-html="renderRich(footnoteTarget)" />
      <div class="source" lang="en" v-html="renderRich(block.footnote_source)" />
    </div>
    <span class="page-marker">{{ pages }}</span>
  </figure>

  <section
    v-else-if="block.kind === 'table'"
    :id="block.id"
    class="table-block"
    :data-page="block.page + 1"
  >
    <div v-if="block.caption_source" class="table-caption pair">
      <div class="target" lang="zh-CN" v-html="renderRich(captionTarget)" />
      <div class="source" lang="en" v-html="renderRich(block.caption_source)" />
    </div>
    <div class="pair table-pair">
      <div
        class="table-scroll"
        :class="tableModeClass"
        :lang="tableLanguage"
        v-html="renderRich(tableContent, true)"
      />
    </div>
    <span class="page-marker">{{ pages }}</span>
  </section>

  <section
    v-else-if="block.kind === 'equation'"
    :id="block.id"
    class="source-only equation"
    :data-page="block.page + 1"
  >
    <div class="equation-scroll" v-html="renderEquation(block.source)" />
    <span class="page-marker">{{ pages }}</span>
  </section>

  <section
    v-else-if="block.kind === 'code' || block.kind === 'reference'"
    :id="block.id"
    class="source-only"
    :class="block.kind"
    :data-page="block.page + 1"
    lang="en"
  >
    <pre v-if="block.kind === 'code'"><code>{{ block.source }}</code></pre>
    <div v-else v-html="renderRich(block.source)" />
    <span class="page-marker">{{ pages }}</span>
  </section>

  <section
    v-else
    :id="block.id"
    class="text-pair pair"
    :class="block.kind"
    :data-page="block.page + 1"
  >
    <div class="target" lang="zh-CN" v-html="renderRich(target)" />
    <div class="source" lang="en" v-html="renderRich(block.source)" />
    <span class="page-marker">{{ pages }}</span>
  </section>
</template>
