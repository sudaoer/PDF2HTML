import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import PaperBlock from "./PaperBlock.vue";
import type { DocumentBlock } from "./types";

function heading(level: number): DocumentBlock {
  return {
    id: `heading-${level}`,
    kind: "heading",
    page: 0,
    level,
    source: "Source heading",
    segment_id: `heading-${level}.text`,
    asset: "",
    caption_source: "",
    caption_segment_id: "",
    footnote_source: "",
    footnote_segment_id: "",
    members: [],
    layout: "auto",
    extra: {},
  };
}

function tableBlock(): DocumentBlock {
  return {
    id: "table-1",
    kind: "table",
    page: 4,
    level: 0,
    source: "<table><tr><td>Method</td></tr><tr><td>Source row</td></tr></table>",
    segment_id: "table-1.table",
    asset: "images/table-1.png",
    caption_source: "Table 1: Results",
    caption_segment_id: "table-1.caption",
    footnote_source: "",
    footnote_segment_id: "",
    members: [],
    layout: "auto",
    extra: {},
  };
}

describe("heading blocks", () => {
  it.each([2, 3])("renders semantic H%s with a matching level class", (level) => {
    const block = heading(level);
    const wrapper = mount(PaperBlock, {
      props: {
        block,
        mode: "bilingual",
        paperId: "paper",
        translations: { [block.segment_id]: "中文标题" },
        media: {},
      },
    });
    expect(wrapper.find(`h${level}`).text()).toBe("中文标题");
    expect(wrapper.find(".heading-pair").classes()).toContain(`heading-level-${level}`);
  });
});

describe("table blocks", () => {
  it.each([
    ["bilingual", "Method", "en"],
    ["source", "Method", "en"],
    ["target", "方法", "zh-CN"],
  ] as const)("renders one structured table in %s mode", (mode, expected, language) => {
    const block = tableBlock();
    const wrapper = mount(PaperBlock, {
      props: {
        block,
        mode,
        paperId: "paper",
        translations: {
          [block.segment_id]:
            "<table><tr><td>方法</td></tr><tr><td>中文行</td></tr></table>",
          [block.caption_segment_id]: "表 1：结果",
        },
        media: { [block.asset]: "media/table-1.png" },
      },
    });

    expect(wrapper.findAll("table")).toHaveLength(1);
    expect(wrapper.find("table").text()).toContain(expected);
    expect(wrapper.find(".table-scroll").attributes("lang")).toBe(language);
    expect(wrapper.find(".table-fallback").exists()).toBe(false);
    expect(wrapper.findAll("img")).toHaveLength(0);
  });

  it("keeps both table captions in bilingual mode", () => {
    const block = tableBlock();
    const wrapper = mount(PaperBlock, {
      props: {
        block,
        mode: "bilingual",
        paperId: "paper",
        translations: {
          [block.segment_id]:
            "<table><tr><td>方法</td></tr><tr><td>中文行</td></tr></table>",
          [block.caption_segment_id]: "表 1：结果",
        },
        media: { [block.asset]: "media/table-1.png" },
      },
    });

    expect(wrapper.find(".table-caption .target").text()).toBe("表 1：结果");
    expect(wrapper.find(".table-caption .source").text()).toBe("Table 1: Results");
  });
});
