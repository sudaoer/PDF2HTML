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
            { id: "note.text", source_hash: "c", target: "贡献相同。机构 $^{2}$" },
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
            front_matter_notes: [
              {
                id: "note",
                kind: "author_note",
                marker: "1",
                page: 0,
                source: "Equal contribution. Institute $^{2}$",
                segment_id: "note.text",
                url: "",
              },
            ],
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
    expect(wrapper.find(".paper-front-notes .target sup").text()).toBe("1");
    expect(wrapper.find(".paper-front-notes .target").text()).toContain("1贡献相同。机构");
    expect(wrapper.find(".paper-front-notes .source").text()).toContain("1Equal contribution. Institute");
    expect(wrapper.findAll(".paper-front-notes .katex")).toHaveLength(2);
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

  it("keeps grouped keywords visible without adding them to the table of contents", async () => {
    history.pushState({}, "", "/papers/keyword-paper/");
    const block = (
      id: string,
      kind: string,
      source: string,
      level: number,
      role = "",
    ) => ({
      id,
      kind,
      page: 0,
      level,
      source,
      segment_id: role === "document_title" || role.startsWith("abstract_") ? "" : `${id}.text`,
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
          bundle_revision: "sha256:keywords",
          paper_id: "keyword-paper",
          job_id: "job",
          media: [],
          translations: [
            { id: "meta.title", source_hash: "a", target: "论文标题" },
            { id: "meta.abstract", source_hash: "b", target: "中文摘要" },
            {
              id: "keywords.text",
              source_hash: "c",
              target: "关键词：医学影像；标志点检测",
            },
            { id: "introduction.text", source_hash: "d", target: "1. 引言" },
          ],
          document: {
            job_id: "job",
            metadata: {
              title: "Keyword paper",
              creators: [],
              date: "",
              publication: "",
              doi: "",
              url: "",
              abstract: "English abstract",
              tags: [],
            },
            segments: [],
            front_matter_notes: [],
            endnotes: [],
            blocks: [
              block("source-title", "heading", "Keyword paper", 1, "document_title"),
              block("article-info", "heading", "ARTICLE INFO", 2),
              block(
                "keywords",
                "paragraph",
                "Keywords: Medical imaging; Landmark detection",
                0,
                "keywords",
              ),
              block("abstract-heading", "heading", "ABSTRACT", 2, "abstract_heading"),
              block("abstract-body", "paragraph", "English abstract", 0, "abstract_body"),
              block("introduction", "heading", "1. Introduction", 2),
            ],
          },
        }),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();
    const keyword = wrapper.find(".paper-body #keywords");
    expect(keyword.exists()).toBe(true);
    expect(keyword.find(".target").text()).toBe("关键词：医学影像；标志点检测");
    expect(keyword.find(".source").text()).toBe("Keywords: Medical imaging; Landmark detection");
    expect(wrapper.findAll(".paper-body #keywords")).toHaveLength(1);
    expect(wrapper.findAll(".toc-panel a").map((entry) => entry.text())).toEqual([
      "1. 引言1. Introduction",
    ]);
    expect(wrapper.text()).not.toContain("ARTICLE INFO");
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
