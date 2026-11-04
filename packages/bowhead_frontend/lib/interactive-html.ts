import { parse as parseJavaScript } from "acorn";
import { defaultTreeAdapter, parse, serialize, type DefaultTreeAdapterMap } from "parse5";

import { sha256Hex } from "./sha256-fallback";
import { randomUuid } from "./random-id";

import { HTML_PREVIEW_BUDGET, HtmlPreviewBudgetError } from "./html-preview-budget";

import { INTERACTIVE_HTML_PERMISSIONS, INTERACTIVE_HTML_SANDBOX, type InteractiveHtmlDocument } from "./html-preview-contract";

type Node = DefaultTreeAdapterMap["node"];
type Element = DefaultTreeAdapterMap["element"];

// Literal intrinsics also work when authored global declarations shadow Object,
// Function, window, or globalThis before the first statement executes. Every
// CSP-authorized script and handler starts with this guard, including copies
// that run in a new srcdoc realm. Do not replace hashes with unsafe-inline.
const NETWORK_GUARD = `;(function(){
  const root=(function(){}).constructor("return this")();
  const intrinsic=({}).constructor;
  if(root.__bowheadHtmlNetworkGuardV1===true)return;
  let target=root;
  while(target){
    for(const key of intrinsic.getOwnPropertyNames(target)){
      if(/^(?:RTC|webkitRTC|mozRTC)/.test(key)||key==="WebTransport"){
        intrinsic.defineProperty(target,key,{value:void 0,writable:false,configurable:false});
      }
    }
    target=intrinsic.getPrototypeOf(target);
  }
  intrinsic.defineProperty(root,"__bowheadHtmlNetworkGuardV1",{value:true,writable:false,configurable:false});
})();\n`;

function guarded(source: string, module = false, handler = false): string {
  const ast = parseJavaScript(source, {
    ecmaVersion: "latest", sourceType: module ? "module" : "script",
    allowReturnOutsideFunction: handler,
  });
  // Keep hashbangs and directive prologues, especially "use strict", effective.
  let offset = source.startsWith("#!") ? source.indexOf("\n") + 1 : 0;
  if (source.startsWith("#!") && offset === 0) return `${source}\n${NETWORK_GUARD}`;
  for (const statement of ast.body) {
    if (!("directive" in statement)) break;
    offset = statement.end;
  }
  return source.slice(0, offset) + NETWORK_GUARD + source.slice(offset);
}

function escapeAttribute(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

async function hash(source: string): Promise<string> {
  const hex = await sha256Hex(new TextEncoder().encode(source));
  return `'sha256-${btoa(hex.match(/../g)!.map((pair) => String.fromCharCode(parseInt(pair, 16))).join(""))}'`;
}

/** Parse inert text only; this function never evaluates authored JavaScript. */
export function validateHtmlComplexity(raw: string) {
  if (raw.length > HTML_PREVIEW_BUDGET.inputBytes || new TextEncoder().encode(raw).length > HTML_PREVIEW_BUDGET.inputBytes) {
    throw new HtmlPreviewBudgetError();
  }
  let allocations = 0;
  const allocate = () => {
    if (++allocations > HTML_PREVIEW_BUDGET.nodes) throw new HtmlPreviewBudgetError();
  };
  const treeAdapter: typeof defaultTreeAdapter = {
    ...defaultTreeAdapter,
    createDocument() {
      allocate();
      return defaultTreeAdapter.createDocument();
    },
    createDocumentFragment() {
      allocate();
      return defaultTreeAdapter.createDocumentFragment();
    },
    createElement(tagName, namespaceURI, attrs) {
      allocate();
      return defaultTreeAdapter.createElement(tagName, namespaceURI, attrs);
    },
    createCommentNode(data) {
      allocate();
      return defaultTreeAdapter.createCommentNode(data);
    },
    insertText(parentNode, text) {
      const previous = defaultTreeAdapter.getChildNodes(parentNode).at(-1);
      if (!previous || !defaultTreeAdapter.isTextNode(previous)) allocate();
      defaultTreeAdapter.insertText(parentNode, text);
    },
    insertTextBefore(parentNode, text, referenceNode) {
      const children = defaultTreeAdapter.getChildNodes(parentNode);
      const previous = children[children.indexOf(referenceNode) - 1];
      if (!previous || !defaultTreeAdapter.isTextNode(previous)) allocate();
      defaultTreeAdapter.insertTextBefore(parentNode, text, referenceNode);
    },
    setDocumentType(document, name, publicId, systemId) {
      if (!defaultTreeAdapter.getChildNodes(document).some(defaultTreeAdapter.isDocumentTypeNode)) allocate();
      defaultTreeAdapter.setDocumentType(document, name, publicId, systemId);
    },
  };
  const tree = parse(raw, { treeAdapter });
  const pending: Array<{ node: Node; depth: number }> = [{ node: tree, depth: 0 }];
  let nodes = 0;
  let handlers = 0;
  let scriptBytes = 0;
  while (pending.length) {
    const { node, depth } = pending.pop()!;
    if (++nodes > HTML_PREVIEW_BUDGET.nodes || depth > HTML_PREVIEW_BUDGET.depth) throw new HtmlPreviewBudgetError();
    if ("tagName" in node) {
      for (const attribute of node.attrs) {
        if (attribute.name.startsWith("on")) {
          handlers++;
          scriptBytes += new TextEncoder().encode(attribute.value).length;
        }
      }
      if (node.tagName === "script") {
        scriptBytes += node.childNodes.reduce((total, child) => total + (child.nodeName === "#text"
          ? new TextEncoder().encode((child as DefaultTreeAdapterMap["textNode"]).value).length : 0), 0);
      }
      if ("content" in node) pending.push({ node: node.content as Node, depth: depth + 1 });
    }
    if (handlers > HTML_PREVIEW_BUDGET.handlers || scriptBytes > HTML_PREVIEW_BUDGET.scriptBytes) throw new HtmlPreviewBudgetError();
    if ("childNodes" in node) for (const child of node.childNodes) pending.push({ node: child, depth: depth + 1 });
  }
  return tree;
}

/** Prepare executable HTML without parsing it in the authenticated DOM. */
export async function buildInteractiveHtmlDocument(raw: string): Promise<InteractiveHtmlDocument> {
  const tree = validateHtmlComplexity(raw);
  const scripts: string[] = [];
  const handlers: string[] = [];
  let externalResources = false;
  function visit(node: Node): void {
    if (!("childNodes" in node)) return;
    for (const child of [...node.childNodes]) {
      if (!("tagName" in child)) continue;
      const element = child as Element;
      const attr = (name: string) => element.attrs.find((item) => item.name === name)?.value;
      const remove = () => { node.childNodes.splice(node.childNodes.indexOf(child), 1); };
      if (element.tagName === "base" || (element.tagName === "meta" && attr("http-equiv"))) {
        remove(); continue;
      }
      if (element.tagName === "script") {
        const type = (attr("type") ?? "").trim().toLowerCase();
        if (attr("src")) { externalResources = true; remove(); continue; }
        if (["", "module", "text/javascript", "application/javascript"].includes(type)) {
          const source = element.childNodes.filter((item) => item.nodeName === "#text")
            .map((item) => (item as DefaultTreeAdapterMap["textNode"]).value).join("");
          const executable = guarded(source, type === "module");
          element.childNodes = [{ nodeName: "#text", value: executable, parentNode: element }];
          scripts.push(executable);
        } else if (!type.includes("json")) {
          remove(); continue;
        }
      }
      for (const attribute of element.attrs) {
        if (attribute.name.startsWith("on")) {
          attribute.value = guarded(attribute.value, false, true);
          handlers.push(attribute.value);
        }
        if (attribute.name === "href" && ["a", "area"].includes(element.tagName)
          && attribute.value.trim().startsWith("#")) {
          // A bare fragment in srcdoc otherwise resolves against the host URL.
          attribute.value = `about:srcdoc${attribute.value.trim()}`;
          continue;
        }
        if (["src", "href", "action", "poster", "srcset"].includes(attribute.name)
          && attribute.value && !/^(?:#|data:)/i.test(attribute.value)) externalResources = true;
      }
      // Network hints and nested browsing contexts have no offline presentation
      // purpose. Dynamic contexts still inherit both sandbox and CSP boundaries.
      if (["link", "iframe", "frame", "object", "embed", "portal"].includes(element.tagName)) {
        remove(); continue;
      }
      visit(element);
      if ("content" in element) visit(element.content as Node);
    }
  }
  visit(tree);
  const channel = randomUuid();
  const relay = guarded(`const child=document.querySelector("iframe");
    const reported=new Set();let lastEscape=-Infinity;
    addEventListener("message",event=>{
      if(event.source===child.contentWindow&&["bowhead-html-escape","bowhead-html-runtime","bowhead-html-blocked"].includes(event.data)){
        if(event.data==="bowhead-html-escape"){
          const now=performance.now();if(now-lastEscape<250)return;lastEscape=now;
        }else{if(reported.has(event.data))return;reported.add(event.data)}
        parent.postMessage({type:event.data,channel:${JSON.stringify(channel)}},"*");
      }
    });`);
  const keyboard = guarded(`addEventListener("keydown",event=>{
    if(event.key==="Escape")parent.postMessage("bowhead-html-escape","*");
  },true);
  const reported=new Set();
  function report(kind){if(!reported.has(kind)){reported.add(kind);parent.postMessage("bowhead-html-"+kind,"*")}}
  addEventListener("error",()=>report("runtime"));
  addEventListener("unhandledrejection",()=>report("runtime"));
  addEventListener("securitypolicyviolation",()=>report("blocked"));`);
  scripts.push(relay, keyboard);
  const scriptHashes = await Promise.all([...new Set(scripts)].map(hash));
  const handlerHashes = await Promise.all([...new Set(handlers)].map(hash));
  const csp = [
    "default-src 'none'", `script-src 'unsafe-eval' ${scriptHashes.join(" ")}`,
    `script-src-attr ${handlerHashes.length ? `'unsafe-hashes' ${handlerHashes.join(" ")}` : "'none'"}`,
    "style-src 'unsafe-inline'", "img-src data:", "font-src data:", "media-src data:",
    "connect-src 'none'", "frame-src 'none'", "child-src 'none'", "worker-src 'none'",
    "object-src 'none'", "form-action 'none'", "base-uri 'none'", "manifest-src 'none'",
  ].join("; ");
  const content = serialize(tree).replace(/<head>/i,
    `<head><meta name="referrer" content="no-referrer"><script>${keyboard}</script>`);
  // A trusted parent with frame-src 'none' prevents the executable child from
  // navigating itself to a network URL. Initial srcdoc documents inherit CSP.
  const document = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${escapeAttribute(csp)}"><meta http-equiv="x-dns-prefetch-control" content="off"><meta name="referrer" content="no-referrer"><style>html,body{margin:0;height:100%;overflow:hidden}iframe{display:block;border:0;width:100%;height:100%}</style></head><body><iframe title="HTML content" sandbox="${INTERACTIVE_HTML_SANDBOX}" allow="${INTERACTIVE_HTML_PERMISSIONS}" referrerpolicy="no-referrer" srcdoc="${escapeAttribute(content)}"></iframe><script>${relay}</script></body></html>`;
  if (document.length > HTML_PREVIEW_BUDGET.outputBytes || new TextEncoder().encode(document).length > HTML_PREVIEW_BUDGET.outputBytes) {
    throw new HtmlPreviewBudgetError();
  }
  return { document, channel, externalResources };
}
