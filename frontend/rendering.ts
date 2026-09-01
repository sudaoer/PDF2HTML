import DOMPurify from "dompurify";
import katex from "katex";
import MarkdownIt from "markdown-it";

const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false }).enable(
  "table",
);
const mathPattern = /(\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\]|\\\((.*?)\\\)|(?<!\\)\$([^$\n]+?)\$)/g;

function latexGroup(value: string, start: number): [string, number] | null {
  if (start >= value.length || value[start] !== "{") return null;
  let depth = 0;
  let index = start;
  while (index < value.length) {
    const character = value[index];
    if (
      character === "\\" &&
      index + 1 < value.length &&
      (value[index + 1] === "{" || value[index + 1] === "}")
    ) {
      index += 2;
      continue;
    }
    if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) return [value.slice(start + 1, index), index];
    }
    index += 1;
  }
  return null;
}

function legacyEmPayload(value: string): string | null {
  const stripped = value.trim();
  if (!stripped.startsWith("\\em")) return null;
  const suffix = stripped.slice(3);
  if (suffix && /^\p{L}/u.test(suffix)) return null;
  return suffix.trim();
}

function normalizeLegacyLatex(value: string): string {
  const normalized: string[] = [];
  const command = "\\textbf";
  let index = 0;
  while (index < value.length) {
    if (value.startsWith(command, index)) {
      let groupStart = index + command.length;
      while (groupStart < value.length && /\s/u.test(value[groupStart])) groupStart += 1;
      const group = latexGroup(value, groupStart);
      if (group) {
        const [inner, groupEnd] = group;
        const payload = legacyEmPayload(inner);
        normalized.push(
          payload === null
            ? `\\textbf{${normalizeLegacyLatex(inner)}}`
            : `\\boldsymbol{${normalizeLegacyLatex(payload)}}`,
        );
        index = groupEnd + 1;
        continue;
      }
    }
    if (value[index] === "{") {
      const group = latexGroup(value, index);
      if (group) {
        const [inner, groupEnd] = group;
        const payload = legacyEmPayload(inner);
        normalized.push(
          payload === null
            ? `{${normalizeLegacyLatex(inner)}}`
            : `{\\mathit{${normalizeLegacyLatex(payload)}}}`,
        );
        index = groupEnd + 1;
        continue;
      }
    }
    normalized.push(value[index]);
    index += 1;
  }
  return normalized.join("");
}

function renderMath(source: string, displayMode: boolean): string {
  return katex.renderToString(normalizeLegacyLatex(source.trim()), {
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
