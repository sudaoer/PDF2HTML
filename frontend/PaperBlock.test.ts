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

describe("heading blocks", () => {
  it.each([2, 3])("renders semantic H%s with a matching level class", (level) => {
    const block = heading(level);
    const wrapper = mount(PaperBlock, {
      props: {
        block,
        paperId: "paper",
        translations: { [block.segment_id]: "中文标题" },
        media: {},
      },
    });
    expect(wrapper.find(`h${level}`).text()).toBe("中文标题");
    expect(wrapper.find(".heading-pair").classes()).toContain(`heading-level-${level}`);
  });
});
