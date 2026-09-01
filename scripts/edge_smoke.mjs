import { mkdir, writeFile } from "node:fs/promises";

const baseUrl = process.argv[2] ?? "http://127.0.0.1:8765/";
const port = Number(process.argv[3] ?? 9223);
const outputDir = new URL("../tmp/browser-qa/", import.meta.url);
const shelfMarkup = await (await fetch(baseUrl)).text();
const firstPaperHref = shelfMarkup.match(/href="(papers\/[^"#]+\/index\.html)"/)?.[1];
const paperUrl = new URL(process.argv[4] ?? firstPaperHref ?? "", baseUrl).href;
if (paperUrl === baseUrl) throw new Error("The shelf does not contain a paper link.");

class CdpClient {
  constructor(socketUrl) {
    this.nextId = 1;
    this.pending = new Map();
    this.socket = new WebSocket(socketUrl);
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
        return;
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

const targetResponse = await fetch(
  `http://127.0.0.1:${port}/json/new?${encodeURIComponent("about:blank")}`,
  { method: "PUT" },
);
if (!targetResponse.ok) throw new Error(`CDP target creation failed: ${targetResponse.status}`);
const target = await targetResponse.json();
const cdp = new CdpClient(target.webSocketDebuggerUrl);
await cdp.open();
await cdp.send("Page.enable");
await cdp.send("Runtime.enable");

async function navigate(url) {
  await cdp.send("Page.navigate", { url });
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const state = await evaluate("document.readyState");
    if (state === "complete") break;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  // Same-document hash navigation does not emit Page.loadEventFired.
  await new Promise((resolve) => setTimeout(resolve, 250));
  const hash = new URL(url).hash;
  if (hash) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const top = await evaluate(
        `Math.round(document.querySelector(${JSON.stringify(hash)})?.getBoundingClientRect().top ?? 99999)`,
      );
      if (top >= 50 && top <= 130) break;
      await new Promise((resolve) => setTimeout(resolve, 75));
    }
  }
}

async function evaluate(expression) {
  const response = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text);
  return response.result.value;
}

async function screenshot(name) {
  const result = await cdp.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
    fromSurface: true,
  });
  await mkdir(outputDir, { recursive: true });
  await writeFile(new URL(name, outputDir), Buffer.from(result.data, "base64"));
}

const report = {};
await cdp.send("Emulation.setDeviceMetricsOverride", {
  width: 390,
  height: 844,
  deviceScaleFactor: 1,
  mobile: true,
});
await navigate(paperUrl);
report.mobile = await evaluate(`({
  innerWidth,
  clientWidth: document.documentElement.clientWidth,
  scrollWidth: document.documentElement.scrollWidth,
  heroRight: Math.round(document.querySelector('.paper-hero').getBoundingClientRect().right),
  heroHeight: Math.round(document.querySelector('.paper-hero').getBoundingClientRect().height),
  titleFontPx: Number.parseFloat(getComputedStyle(document.querySelector('.paper-hero h1')).fontSize),
  controlsRight: Math.round(document.querySelector('.reader-controls').getBoundingClientRect().right),
  overflows: [...document.querySelectorAll('body *')]
    .map((element) => {
      const box = element.getBoundingClientRect();
      return {
        tag: element.tagName.toLowerCase(),
        id: element.id,
        className: typeof element.className === 'string' ? element.className : '',
        left: Math.round(box.left),
        right: Math.round(box.right),
        width: Math.round(box.width),
        text: (element.textContent || '').trim().slice(0, 60)
      };
    })
    .filter((element) => element.left < -1 || element.right > 391)
    .slice(0, 12)
})`);
report.languageToggle = await evaluate(`(() => {
  document.querySelector('[data-mode="target"]').click();
  const target = document.querySelector('.abstract .target');
  const source = document.querySelector('.abstract .source');
  return {
    bodyClass: document.body.className,
    pressed: document.querySelector('[data-mode="target"]').getAttribute('aria-pressed'),
    targetDisplay: getComputedStyle(target).display,
    sourceDisplay: getComputedStyle(source).display
  };
})()`);
report.themeToggle = await evaluate(`(() => {
  document.querySelector('[data-theme-toggle]').click();
  return document.documentElement.dataset.theme;
})()`);
await screenshot("cdp-mobile-paper.png");

await cdp.send("Emulation.setDeviceMetricsOverride", {
  width: 1440,
  height: 1000,
  deviceScaleFactor: 1,
  mobile: false,
});
await navigate(paperUrl);
report.desktopLanding = await evaluate(`(() => {
  const hero = document.querySelector('.paper-hero').getBoundingClientRect();
  const title = document.querySelector('.paper-hero h1').getBoundingClientRect();
  const abstract = document.querySelector('.abstract').getBoundingClientRect();
  return {
    innerWidth,
    innerHeight,
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    titleFontPx: Number.parseFloat(getComputedStyle(document.querySelector('.paper-hero h1')).fontSize),
    titleHeight: Math.round(title.height),
    heroHeight: Math.round(hero.height),
    abstractTop: Math.round(abstract.top)
  };
})()`);
if (Math.abs(report.desktopLanding.titleFontPx - 51.2) > 0.1) {
  throw new Error(`Desktop title font is ${report.desktopLanding.titleFontPx}px; expected 51.2px.`);
}
if (report.desktopLanding.titleHeight > 300) {
  throw new Error(`Desktop title is ${report.desktopLanding.titleHeight}px tall; expected at most 300px.`);
}
if (report.desktopLanding.heroHeight > 850) {
  throw new Error(`Desktop hero is ${report.desktopLanding.heroHeight}px tall; expected at most 850px.`);
}
if (report.desktopLanding.abstractTop >= report.desktopLanding.innerHeight) {
  throw new Error(
    `Desktop abstract starts at ${report.desktopLanding.abstractTop}px, below the first viewport.`,
  );
}
if (report.desktopLanding.scrollWidth > report.desktopLanding.clientWidth) {
  throw new Error(
    `Desktop page overflows horizontally: ${report.desktopLanding.scrollWidth}px > `
      + `${report.desktopLanding.clientWidth}px.`,
  );
}
if (Math.abs(report.mobile.titleFontPx - 32) > 0.1) {
  throw new Error(`Mobile title font is ${report.mobile.titleFontPx}px; expected 32px.`);
}
await screenshot("cdp-desktop-paper-top.png");

const anchorId = await evaluate(`(() => {
  const headings = [...document.querySelectorAll('.heading-pair[id]')];
  const application = headings.find((heading) =>
    /application|应用/i.test(heading.textContent || '')
  );
  return (application ?? headings[Math.floor(headings.length * 0.8)])?.id ?? '';
})()`);
if (!anchorId) throw new Error("The paper does not contain a heading anchor.");
await navigate(`${paperUrl}#${anchorId}`);
report.anchor = await evaluate(`({
  scrollY: Math.round(scrollY),
  headingTop: Math.round(document.querySelector(${JSON.stringify(`#${anchorId}`)}).getBoundingClientRect().top),
  headingText: document.querySelector(${JSON.stringify(`#${anchorId} .target`)}).textContent.trim()
})`);
await screenshot("cdp-desktop-application.png");

await navigate(baseUrl);
report.search = await evaluate(`(() => {
  const input = document.querySelector('[data-shelf-search]');
  const count = () => [...document.querySelectorAll('.paper-card')]
    .filter((card) => !card.hidden).length;
  input.value = 'definitely-no-match';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  const noMatch = count();
  input.value = 'RouterNet';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return { noMatch, matching: count() };
})()`);
await cdp.send("Emulation.setDeviceMetricsOverride", {
  width: 390,
  height: 844,
  deviceScaleFactor: 1,
  mobile: true,
});
await navigate(baseUrl);
report.mobileShelf = await evaluate(`({
  clientWidth: document.documentElement.clientWidth,
  scrollWidth: document.documentElement.scrollWidth,
  cardRight: Math.round(document.querySelector('.paper-card').getBoundingClientRect().right)
})`);
await screenshot("cdp-mobile-library.png");

await mkdir(outputDir, { recursive: true });
await writeFile(
  new URL("browser-smoke.json", outputDir),
  `${JSON.stringify(report, null, 2)}\n`,
  "utf8",
);
console.log(JSON.stringify(report, null, 2));
await cdp.send("Browser.close");
