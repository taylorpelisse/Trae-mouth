const originals = new WeakMap<Text, string>();
const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA"]);
let toTraditional: ((text: string) => string) | null = null;

async function ensureConverter() {
  if (toTraditional) return toTraditional;
  const module = await import("opencc-js");
  toTraditional = module.Converter({ from: "cn", to: "twp" });
  return toTraditional;
}

function convertNode(node: Text, locale: "zh-CN" | "zh-TW") {
  const parent = node.parentElement;
  if (parent && SKIP.has(parent.tagName)) return;
  if (locale === "zh-CN") {
    const original = originals.get(node);
    if (original !== undefined && node.nodeValue !== original) {
      node.nodeValue = original;
    }
    if (original !== undefined) originals.delete(node);
    return;
  }
  if (!toTraditional) return;
  if (!originals.has(node)) originals.set(node, node.nodeValue ?? "");
  const source = originals.get(node) ?? "";
  const next = toTraditional(source);
  if (next !== node.nodeValue) node.nodeValue = next;
}

function walk(root: Node, locale: "zh-CN" | "zh-TW") {
  if (root.nodeType === Node.TEXT_NODE) {
    convertNode(root as Text, locale);
    return;
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    convertNode(node as Text, locale);
    node = walker.nextNode();
  }
}

let observer: MutationObserver | null = null;

function observeTraditional() {
  observer?.disconnect();
  observer = new MutationObserver((records) => {
    for (const record of records) {
      if (
        record.type === "characterData" &&
        record.target.nodeType === Node.TEXT_NODE
      ) {
        convertNode(record.target as Text, "zh-TW");
        continue;
      }
      for (const node of record.addedNodes) {
        if (node.nodeType === Node.TEXT_NODE) {
          convertNode(node as Text, "zh-TW");
        } else if (node.nodeType === Node.ELEMENT_NODE) {
          walk(node, "zh-TW");
        }
      }
    }
  });
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });
}

export function applyLocale(locale: "zh-CN" | "zh-TW") {
  document.documentElement.lang = locale === "zh-TW" ? "zh-Hant" : "zh-CN";
  observer?.disconnect();
  observer = null;
  if (locale === "zh-CN") {
    walk(document.body, "zh-CN");
    return;
  }
  void ensureConverter().then(() => {
    walk(document.body, "zh-TW");
    observeTraditional();
  });
}
