import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App.vue";

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("application routes", () => {
  it("renders shelf values as text", async () => {
    history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          schema_version: 1,
          library_revision: "sha256:test",
          papers: [
            {
              paper_id: "safe-paper",
              job_id: "job",
              title: '<img src=x onerror="alert(1)">',
              title_zh: "安全标题",
              creators: ["Alice"],
              date: "2026",
              publication: "Journal",
              doi: "",
              url: "",
              abstract: "",
              tags: [],
              href: "/papers/safe-paper/",
              bundle_revision: "sha256:test",
            },
          ],
          errors: [],
        }),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("安全标题");
    expect(wrapper.text()).toContain("<img src=x");
    expect(wrapper.find(".paper-card img").exists()).toBe(false);
  });

  it("loads a paper and switches language mode", async () => {
    history.pushState({}, "", "/papers/safe-paper/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          schema_version: 1,
          bundle_revision: "sha256:test",
          paper_id: "safe-paper",
          job_id: "job",
          media: [],
          translations: [
            { id: "meta.title", source_hash: "a", target: "中文标题" },
            { id: "b1.text", source_hash: "b", target: "中文正文" },
          ],
          document: {
            job_id: "job",
            metadata: {
              title: "English title",
              creators: ["Alice"],
              date: "2026",
              publication: "Journal",
              doi: "",
              url: "",
              abstract: "",
              tags: [],
            },
            segments: [],
            front_matter_notes: [],
            endnotes: [],
            blocks: [
              {
                id: "b1",
                kind: "paragraph",
                page: 0,
                level: 0,
                source: "English body",
                segment_id: "b1.text",
                asset: "",
                caption_source: "",
                caption_segment_id: "",
                footnote_source: "",
                footnote_segment_id: "",
                members: [],
                layout: "auto",
                extra: {},
              },
            ],
          },
        }),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("中文正文");
    const chinese = wrapper.findAll("button").find((button) => button.text() === "中文");
    await chinese?.trigger("click");
    expect(wrapper.find(".paper-page").classes()).toContain("mode-target");
  });
});
