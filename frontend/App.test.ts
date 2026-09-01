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
    expect(document.title).toContain("中文标题 · English title");
    const chinese = wrapper.findAll("button").find((button) => button.text() === "中文");
    await chinese?.trigger("click");
    expect(wrapper.find(".paper-page").classes()).toContain("mode-target");
  });

  it("keeps front matter out of the ordered hierarchical table of contents", async () => {
    history.pushState({}, "", "/papers/outline-paper/");
    const block = (id: string, source: string, level: number, role = "") => ({
      id,
      kind: "heading",
      page: 0,
      level,
      source,
      segment_id: role ? "" : `${id}.text`,
      asset: "",
      caption_source: "",
      caption_segment_id: "",
      footnote_source: "",
      footnote_segment_id: "",
      members: [],
      layout: "auto",
      extra: role ? { front_matter_role: role } : {},
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          schema_version: 1,
          bundle_revision: "sha256:outline",
          paper_id: "outline-paper",
          job_id: "job",
          media: [],
          translations: [
            { id: "meta.title", source_hash: "a", target: "论文标题" },
            { id: "section.text", source_hash: "b", target: "主节" },
            { id: "subsection.text", source_hash: "c", target: "子节" },
          ],
          document: {
            job_id: "job",
            metadata: {
              title: "Paper title",
              creators: [],
              date: "",
              publication: "",
              doi: "",
              url: "",
              abstract: "",
              tags: [],
            },
            segments: [],
            front_matter_notes: [],
            endnotes: [],
            blocks: [
              block("source-title", "Paper title", 1, "document_title"),
              block("section", "Section", 2),
              block("subsection", "Subsection", 3),
            ],
          },
        }),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();
    const entries = wrapper.findAll(".toc-panel a");
    expect(entries).toHaveLength(2);
    expect(entries.map((entry) => entry.classes())).toEqual([
      ["toc-level-2"],
      ["toc-level-3"],
    ]);
    expect(entries.map((entry) => entry.text())).toEqual(["主节Section", "子节Subsection"]);
    expect(wrapper.find(".paper-body h2").text()).toBe("主节");
    expect(wrapper.find(".paper-body h3").text()).toBe("子节");
  });

  it("renders HTTP search excerpts without interpreting markup", async () => {
    history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: string) => {
        if (input.startsWith("/api/search")) {
          return Promise.resolve(
            jsonResponse({
              query: "route",
              library_revision: "sha256:test",
              results: [
                {
                  paper_id: "safe-paper",
                  bundle_revision: "sha256:test",
                  kind: "paragraph",
                  block_id: "b1",
                  segment_id: "b1.text",
                  page: 2,
                  title: "English title",
                  title_zh: "中文标题",
                  source_excerpt: "<img src=x onerror=alert(1)>",
                  target_excerpt: "安全摘要",
                  score: 1,
                },
              ],
            }),
          );
        }
        return Promise.resolve(
          jsonResponse({
            schema_version: 1,
            library_revision: "sha256:test",
            papers: [],
            errors: [],
          }),
        );
      }),
    );
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.find("input[type='search']").setValue("route");
    await flushPromises();
    expect(wrapper.find(".search-hit").text()).toContain("<img src=x");
    expect(wrapper.find(".search-hit img").exists()).toBe(false);
  });
});
