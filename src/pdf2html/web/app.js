(() => {
  const root = document.documentElement;
  const body = document.body;
  const setTheme = (theme) => {
    root.dataset.theme = theme;
    try { localStorage.setItem("pdf2html-theme", theme); } catch (_) { /* file mode */ }
  };
  let savedTheme = "";
  try { savedTheme = localStorage.getItem("pdf2html-theme") || ""; } catch (_) { /* file mode */ }
  if (savedTheme) setTheme(savedTheme);
  document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
    setTheme(root.dataset.theme === "dark" ? "light" : "dark");
  });

  const modeButtons = [...document.querySelectorAll("[data-mode]")];
  modeButtons.forEach((button) => button.addEventListener("click", () => {
    const mode = button.dataset.mode;
    body.classList.remove("mode-bilingual", "mode-source", "mode-target");
    body.classList.add(`mode-${mode}`);
    modeButtons.forEach((candidate) => candidate.setAttribute("aria-pressed", String(candidate === button)));
  }));

  let readerSize = 18;
  document.querySelector("[data-font='up']")?.addEventListener("click", () => {
    readerSize = Math.min(24, readerSize + 1);
    root.style.setProperty("--reader-size", `${readerSize}px`);
  });
  document.querySelector("[data-font='down']")?.addEventListener("click", () => {
    readerSize = Math.max(15, readerSize - 1);
    root.style.setProperty("--reader-size", `${readerSize}px`);
  });

  const progress = document.querySelector(".reading-progress span");
  if (progress) {
    const update = () => {
      const available = document.documentElement.scrollHeight - window.innerHeight;
      const ratio = available > 0 ? Math.min(1, window.scrollY / available) : 0;
      progress.style.width = `${ratio * 100}%`;
    };
    update();
    addEventListener("scroll", update, { passive: true });
    addEventListener("resize", update);
  }

  const dialog = document.querySelector(".image-dialog");
  document.querySelectorAll(".figure-open img").forEach((image) => {
    image.parentElement.addEventListener("click", () => {
      if (!dialog?.showModal) return;
      const dialogImage = dialog.querySelector("img");
      dialogImage.src = image.src;
      dialogImage.alt = image.alt;
      dialog.showModal();
    });
  });
  dialog?.querySelector("button")?.addEventListener("click", () => dialog.close());
  dialog?.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  const search = document.querySelector("[data-shelf-search]");
  const cards = [...document.querySelectorAll(".paper-card")];
  search?.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase();
    cards.forEach((card) => {
      card.hidden = query && !card.dataset.search.toLocaleLowerCase().includes(query);
    });
  });
})();
