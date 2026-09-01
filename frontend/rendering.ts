import DOMPurify from "dompurify";
import katex from "katex";
import MarkdownIt from "markdown-it";

const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false }).enable(
  "table",
);
const mathPattern = /(\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\]|\\\((.*?)\\\)|(?<!\\)\$([^$\n]+?)\$)/g;

function renderMath(source: string, displayMode: boolean): string {
  return katex.renderToString(source.trim(), {
    displayMode,
    output: "htmlAndMathml",
    strict: "ignore",
    throwOnError: false,
    trust: false,
  });
}

export function renderRich(value: string, allowTable = false): string {
  if (!value.trim()) return "";
  if (allowTable && /<table[\s>]/i.test(value)) {
    return DOMPurify.sanitize(value, {
      ALLOWED_ATTR: ["colspan", "rowspan"],
      ALLOWED_TAGS: ["table", "thead", "tbody", "tfoot", "tr", "th", "td"],
    });
  }
  const replacements: string[] = [];
  const protectedText = value.replace(
    mathPattern,
    (_match, _whole, dollarDisplay, bracketDisplay, parenInline, dollarInline) => {
      const display = dollarDisplay !== undefined || bracketDisplay !== undefined;
      const source = dollarDisplay ?? bracketDisplay ?? parenInline ?? dollarInline ?? "";
      const index = replacements.push(renderMath(source, display)) - 1;
      return `PDFTWOHTMLMATHTOKEN${index}END`;
    },
  );
  let rendered = markdown.render(protectedText);
  replacements.forEach((replacement, index) => {
    rendered = rendered.replace(`PDFTWOHTMLMATHTOKEN${index}END`, replacement);
  });
  return DOMPurify.sanitize(rendered);
}

export function renderEquation(value: string): string {
  return DOMPurify.sanitize(renderMath(value, true));
}
