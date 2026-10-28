import { PRODUCT_NAME } from "@/lib/product-identity";

/**
 * Static canonical metadata bootstrap shared by both root layouts.
 *
 * Gateway request branding runs before this script. Nodes marked initialized
 * are therefore preserved; a direct static load receives the Bowhead baseline.
 */
export const CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT = String.raw`
(function(){
  try {
    var doc=document,head=doc.head;
    if(!head)return;
    function ensure(id,tag){
      var node=doc.getElementById(id);
      if(node&&node.tagName.toLowerCase()===tag)return node;
      if(node)node.remove();
      node=doc.createElement(tag);
      node.id=id;
      head.appendChild(node);
      return node;
    }
    function own(node){
      node.setAttribute("data-bowhead-metadata-owned","");
      node.setAttribute("data-bowhead-metadata-owner","bootstrap");
    }
    var title=ensure("bowhead-document-title","title");
    own(title);
    if(!title.hasAttribute("data-bowhead-metadata-initialized")){
      title.textContent=${JSON.stringify(`${PRODUCT_NAME} — Verified Intelligence`)};
      title.setAttribute("data-bowhead-metadata-source","canonical");
      title.setAttribute("data-bowhead-metadata-initialized","");
    }
    var favicon=ensure("bowhead-runtime-favicon","link");
    own(favicon);
    if(!favicon.hasAttribute("data-bowhead-metadata-initialized")){
      var context=window.__BOWHEAD_PUBLIC_CONTEXT__;
      var prefix=context&&typeof context.basePath==="string"?context.basePath:"";
      if(!prefix.startsWith("/")||prefix.startsWith("//")||/[?#]/.test(prefix))prefix="";
      prefix=prefix.replace(/\/+$/,"");
      favicon.rel="icon";
      favicon.type="image/svg+xml";
      favicon.setAttribute("href",prefix+"/bowhead-icon.svg");
      favicon.setAttribute("data-bowhead-metadata-source","canonical");
      favicon.setAttribute("data-bowhead-metadata-initialized","");
    }
  } catch (_) {}
})();`;
