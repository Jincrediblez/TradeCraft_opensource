(function () {
  "use strict";

  const SUPPORTED = ["en", "zh-CN"];
  const FALLBACK = "en";
  let locale = FALLBACK;
  let catalog = {};
  let observer = null;
  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();

  function normalize(value) {
    const raw = String(value || "").trim().replaceAll("_", "-").toLowerCase();
    if (raw === "zh-cn") return "zh-CN";
    return FALLBACK;
  }

  function resolve(requested) {
    return normalize(requested);
  }

  function lookup(key) {
    return String(key || "").split(".").reduce((value, part) => (
      value && typeof value === "object" ? value[part] : undefined
    ), catalog);
  }

  function t(key, params = {}, fallback = "") {
    let text = lookup(key);
    if (typeof text !== "string") text = fallback || key;
    Object.entries(params || {}).forEach(([name, value]) => {
      text = text.replaceAll(`{${name}}`, String(value));
    });
    return text;
  }

  function translateUiText(text) {
    if (locale !== "zh-CN") return text;
    const trimmed = String(text || "").trim();
    if (!trimmed) return text;
    const translated = catalog.translations?.[trimmed];
    if (translated) return String(text).replace(trimmed, translated);
    let result = String(text);
    Object.entries(catalog.partialTranslations || {}).forEach(([source, target]) => {
      result = result.replaceAll(source, target);
    });
    return result;
  }

  function apply(root = document) {
    root.querySelectorAll?.("[data-i18n]").forEach((element) => {
      element.textContent = t(element.dataset.i18n, {}, element.textContent);
    });
    for (const attr of ["placeholder", "title", "aria-label"]) {
      root.querySelectorAll?.(`[data-i18n-${attr}]`).forEach((element) => {
        const key = element.dataset[`i18n${attr.split("-").map(x => x[0].toUpperCase() + x.slice(1)).join("")}`];
        element.setAttribute(attr, t(key, {}, element.getAttribute(attr) || ""));
      });
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const parent = walker.currentNode.parentElement;
      if (!parent || ["SCRIPT", "STYLE"].includes(parent.tagName) || parent.closest("[data-i18n]")) continue;
      nodes.push(walker.currentNode);
    }
    nodes.forEach((node) => {
      if (!originalText.has(node)) originalText.set(node, node.nodeValue);
      const source = originalText.get(node);
      const translated = translateUiText(source);
      if (translated !== node.nodeValue) node.nodeValue = translated;
    });
    root.querySelectorAll?.("[placeholder],[title],[aria-label]").forEach((element) => {
      if (!originalAttributes.has(element)) originalAttributes.set(element, {});
      const originals = originalAttributes.get(element);
      for (const attr of ["placeholder", "title", "aria-label"]) {
        if (!element.hasAttribute(attr)) continue;
        if (element.hasAttribute(`data-i18n-${attr}`)) continue;
        if (!(attr in originals)) originals[attr] = element.getAttribute(attr);
        const current = element.getAttribute(attr);
        const source = originals[attr];
        const translated = translateUiText(source);
        if (translated !== current) element.setAttribute(attr, translated);
      }
    });
  }

  async function setLocale(requested, options = {}) {
    const resolved = resolve(requested || FALLBACK);
    const response = await fetch(`/static/locales/${resolved}.json`, {cache: "no-store"});
    if (!response.ok) throw new Error(`Locale catalog unavailable: ${resolved}`);
    catalog = await response.json();
    locale = SUPPORTED.includes(resolved) ? resolved : FALLBACK;
    document.documentElement.lang = locale;
    if (options.persist !== false) {
      localStorage.setItem("tradecraft-locale", requested || locale);
    }
    if (observer) observer.disconnect();
    apply(document);
    observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => mutation.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) apply(node);
        if (node.nodeType === Node.TEXT_NODE && node.parentElement) {
          if (!originalText.has(node)) originalText.set(node, node.nodeValue);
          const source = originalText.get(node);
          node.nodeValue = translateUiText(source);
        }
      }));
    });
    observer.observe(document.body, {childList: true, subtree: true});
    window.dispatchEvent(new CustomEvent("tradecraft:localechange", {detail: {locale}}));
    return locale;
  }

  function formatNumber(value, options = {}) {
    return new Intl.NumberFormat(locale, options).format(Number(value || 0));
  }

  function formatDate(value, options = {}) {
    const date = value instanceof Date ? value : new Date(value);
    return Number.isNaN(date.getTime()) ? String(value || "") : new Intl.DateTimeFormat(locale, options).format(date);
  }

  window.TradeCraftI18n = {
    apply,
    formatDate,
    formatNumber,
    get locale() { return locale; },
    normalize,
    resolve,
    setLocale,
    t,
  };

  document.addEventListener("DOMContentLoaded", () => {
    const saved = localStorage.getItem("tradecraft-locale") || FALLBACK;
    setLocale(saved, {persist: false}).catch(() => {});
  });
})();
