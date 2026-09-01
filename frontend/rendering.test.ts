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

  it("normalizes legacy bold-italic commands in the RouterNet inline formula", () => {
    const output = renderRich(
      String.raw`Each segment is a sub-path $S^{o} \xrightarrow[U]{\textbf { \em F }} S \xrightarrow[R]{\textbf { \em F }_{\mathrm{next}}} S^{o}_{\mathrm{next}}$.`,
    );
    expect(output).toContain("boldsymbol");
    expect(output).not.toContain(String.raw`\em`);
    expect(output).not.toContain("katex-error");
    expect(output).not.toContain("color:#cc0000");
  });

  it("normalizes legacy emphasis in display and nested formulas", () => {
    const display = renderEquation(String.raw`{\em F}_{\mathrm{next}}`);
    const nested = renderRich(String.raw`Nested: $\textbf{\em A_{\textbf{\em B}}}$.`);
    expect(display).toContain("mathit");
    expect(display).not.toContain(String.raw`\em`);
    expect(display).not.toContain("katex-error");
    expect(nested.match(/boldsymbol/g)?.length).toBeGreaterThanOrEqual(2);
    expect(nested).not.toContain(String.raw`\em`);
    expect(nested).not.toContain("katex-error");
  });

  it("does not rewrite longer commands that begin with em", () => {
    const output = renderRich(String.raw`Commands: $ {\emph{word}}$ and $ {\emptyset}$.`);
    expect(output).toContain("textit");
    expect(output).toContain("∅");
    expect(output).not.toContain("katex-error");
  });

  it("leaves malformed legacy groups to the existing KaTeX error policy", () => {
    const output = renderRich(String.raw`Broken: $\textbf{\em F$.`);
    expect(output).toContain("katex-error");
  });

  it("allows only table structure for table blocks", () => {
    const output = renderRich('<table><tr><td colspan="2">A</td></tr></table>', true);
    expect(output).toContain("<table>");
    expect(output).toContain('colspan="2"');
  });
});
