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

  it("renders math inside sanitized table text nodes", () => {
    const output = renderRich(
      String.raw`<table><tr><th>Plain</th><th>$OR_{20}$ ↓</th><th>\(OR_{40}\)</th><th>$\textbf{\em F}$</th></tr></table>`,
      true,
    );
    const container = document.createElement("div");
    container.innerHTML = output;

    expect(container.querySelectorAll("table .katex")).toHaveLength(3);
    expect(container.textContent).toContain("Plain");
    expect(container.textContent).toContain("↓");
    expect(container.textContent).not.toContain("$OR_{20}$");
    expect(output).not.toContain(String.raw`\em`);
    expect(output).not.toContain("katex-error");
  });

  it("keeps the strict table allowlist while adding KaTeX", () => {
    const output = renderRich(
      '<table onclick="alert(1)"><tr><td rowspan="2" colspan="3" onmouseover="alert(2)">$x^2$<script>alert(3)</script><img src="x" onerror="alert(4)"></td><td><a href="https://example.com">link</a></td></tr></table>',
      true,
    );
    const container = document.createElement("div");
    container.innerHTML = output;
    const cell = container.querySelector("td");

    expect(container.querySelectorAll("table .katex")).toHaveLength(1);
    expect(cell?.getAttribute("rowspan")).toBe("2");
    expect(cell?.getAttribute("colspan")).toBe("3");
    expect(container.querySelector("script, img, a")).toBeNull();
    expect(output).not.toContain("onclick");
    expect(output).not.toContain("onmouseover");
    expect(output).not.toContain("onerror");
  });
});
