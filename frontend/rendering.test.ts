import { describe, expect, it } from "vitest";

import { renderEquation, renderRich } from "./rendering";

describe("safe rendering", () => {
  it("renders math without accepting source HTML", () => {
    const output = renderRich("Value is $x^2$. <script>alert(1)</script>");
    expect(output).toContain("katex");
    expect(output).not.toContain("<script>");
  });

  it("renders display equations", () => {
    expect(renderEquation(String.raw`E=mc^2\tag{1}`)).toContain("katex-display");
  });

  it("allows only table structure for table blocks", () => {
    const output = renderRich('<table><tr><td colspan="2">A</td></tr></table>', true);
    expect(output).toContain("<table>");
    expect(output).toContain('colspan="2"');
  });
});
