import DOMPurify from "dompurify";
import katex from "katex";
import MarkdownIt from "markdown-it";

const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false }).enable(
  "table",
);
markdown.inline.ruler.before("html_inline", "plain_sup_sub", (state, silent) => {
  const match = /^<(sup|sub)>([^<>`\n]*)<\/\1>/.exec(
    state.src.slice(state.pos, state.posMax),
  );
  if (!match) return false;
  if (!silent) {
    const [, tag, content] = match;
    state.push(`${tag}_open`, tag, 1);
    state.push("text", "", 0).content = markdown.utils.unescapeAll(content);
    state.push(`${tag}_close`, tag, -1);
  }
  state.pos += match[0].length;
  return true;
});
const mathPattern = /(\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\]|\\\((.*?)\\\)|(?<!\\)\$([^$\n]+?)\$)/g;
const tableSanitizeConfig = {
  ALLOWED_ATTR: ["colspan", "rowspan"],
  ALLOWED_TAGS: ["table", "thead", "tbody", "tfoot", "tr", "th", "td"],
};

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

function renderMathText(value: string): DocumentFragment | null {
  const pattern = new RegExp(mathPattern.source, mathPattern.flags);
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  let found = false;
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0;
    const dollarDisplay = match[2];
    const bracketDisplay = match[3];
    const parenInline = match[4];
    const dollarInline = match[5];
    const display = dollarDisplay !== undefined || bracketDisplay !== undefined;
    const source = dollarDisplay ?? bracketDisplay ?? parenInline ?? dollarInline ?? "";
    const rendered = document.createElement("template");
    rendered.innerHTML = DOMPurify.sanitize(renderMath(source, display));

    found = true;
    fragment.append(document.createTextNode(value.slice(cursor, index)));
    fragment.append(rendered.content.cloneNode(true));
    cursor = index + match[0].length;
  }
  if (!found) return null;
  fragment.append(document.createTextNode(value.slice(cursor)));
  return fragment;
}

function renderSafeTable(value: string): string {
  const table = document.createElement("template");
  table.innerHTML = DOMPurify.sanitize(value, tableSanitizeConfig);
  const walker = document.createTreeWalker(table.content, NodeFilter.SHOW_TEXT);
  const textNodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    textNodes.push(node as Text);
  }
  textNodes.forEach((node) => {
    const replacement = renderMathText(node.data);
    if (replacement) node.replaceWith(replacement);
  });
  return DOMPurify.sanitize(table.innerHTML);
}

export function renderRich(value: string, allowTable = false): string {
  if (!value.trim()) return "";
  if (allowTable && /<table[\s>]/i.test(value)) {
    return renderSafeTable(value);
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
