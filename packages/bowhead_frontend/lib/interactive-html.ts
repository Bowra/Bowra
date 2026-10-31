import { parse as parseJavaScript } from "acorn";
import { parse, serialize, type DefaultTreeAdapterMap } from "parse5";

import { sha256Hex } from "./sha256-fallback";

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

/** Prepare executable HTML without parsing it in the authenticated DOM. */
export async function buildInteractiveHtmlDocument(raw: string): Promise<InteractiveHtmlDocument> {
  const tree = parse(raw);
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
  const channel = globalThis.crypto?.randomUUID?.() ?? `html-${Date.now()}-${Math.random()}`;
  const relay = guarded(`const child=document.querySelector("iframe");
    addEventListener("message",event=>{
      if(event.source===child.contentWindow&&["bowhead-html-escape","bowhead-html-runtime","bowhead-html-blocked"].includes(event.data)){
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
  const scriptHashes = [...new Set(await Promise.all(scripts.map(hash)))];
  const handlerHashes = [...new Set(await Promise.all(handlers.map(hash)))];
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
  return { document, channel, externalResources };
}
