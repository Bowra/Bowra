import { PRODUCT_NAME } from "@/lib/product-identity";

/**
 * Synchronous, authenticated-app-only brand cache bootstrap.
 *
 * The payload is treated as untrusted localStorage data: every color, id and
 * path is checked before it reaches a style or URL sink.
 */
export const BRAND_BOOTSTRAP_SCRIPT = String.raw`
(function(){
  try {
    var doc=document,root=doc.documentElement,head=doc.head;
    if(!head)return;
    var context=window.__BOWHEAD_PUBLIC_CONTEXT__;
    var prefix=context&&typeof context.basePath==="string"?context.basePath:"";
    if(!prefix.startsWith("/")||prefix.startsWith("//")||/[?#]/.test(prefix))prefix="";
    prefix=prefix.replace(/\/+$/,"");
    function ensure(id,tag){
      var node=doc.getElementById(id);
      if(node&&node.tagName.toLowerCase()===tag)return node;
      if(node)node.remove();
      node=doc.createElement(tag);
      node.id=id;
      head.appendChild(node);
      return node;
    }
    function setOptional(node,name,value){
      if(typeof value==="string"&&value)node.setAttribute(name,value);
      else node.removeAttribute(name);
    }
    function applyMetadata(applicationName,faviconHref,source,meta){
      var title=ensure("bowhead-document-title","title");
      title.setAttribute("data-bowhead-metadata-owned","");
      title.setAttribute("data-bowhead-metadata-owner","bootstrap");
      title.setAttribute("data-bowhead-metadata-initialized","");
      title.setAttribute("data-bowhead-metadata-source",source);
      setOptional(title,"data-bowhead-metadata-team",meta&&meta.id);
      setOptional(title,"data-bowhead-metadata-revision",meta&&meta.revisionId);
      title.textContent=applicationName+" — Verified Intelligence";
      var favicon=ensure("bowhead-runtime-favicon","link");
      favicon.setAttribute("data-bowhead-metadata-owned","");
      favicon.setAttribute("data-bowhead-metadata-owner","bootstrap");
      favicon.setAttribute("data-bowhead-metadata-initialized","");
      favicon.setAttribute("data-bowhead-metadata-source",source);
      setOptional(favicon,"data-bowhead-metadata-team",meta&&meta.id);
      setOptional(favicon,"data-bowhead-metadata-revision",meta&&meta.revisionId);
      favicon.rel="icon";
      if(faviconHref.startsWith("data:image/svg+xml")||faviconHref.endsWith(".svg"))favicon.type="image/svg+xml";
      else favicon.removeAttribute("type");
      favicon.setAttribute("href",faviconHref);
    }
    function generatedFavicon(value){
      var initial=Array.from(value||"?")[0].toUpperCase().replace(/[&<>"']/g,function(character){
        return ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&apos;"})[character]||character;
      });
      var svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#161310"/><text x="32" y="42" text-anchor="middle" font-family="system-ui,sans-serif" font-size="34" font-weight="700" fill="#FAF9F6">'+initial+'</text></svg>';
      return "data:image/svg+xml,"+encodeURIComponent(svg);
    }
    applyMetadata(${JSON.stringify(PRODUCT_NAME)},prefix+"/bowhead-icon.svg","canonical",null);
    var team=localStorage.getItem("bowhead.brand.active-team:v2")||"";
    if(!team)return;
    var cache=JSON.parse(localStorage.getItem("bowhead.brand.bundle:v2:"+team)||"null");
    var response=cache&&cache.version===2&&cache.teamId===team?cache.response:null;
    var meta=response&&response.schemaVersion===2&&response.team;
    var effective=response&&response.effective;
    var scheme=effective&&effective.colorScheme;
    var brand=response&&response.brand;
    if(!meta||meta.id!==team||!["legacy","bowhead","preset","custom"].includes(meta.configurationType)||!scheme||scheme.tokenSchemaVersion!==2||!/^sha256:[0-9a-f]{64}$/.test(scheme.checksum||""))return;
    var keys=["background","surface","surface2","surface3","line","line2","ink","ink2","ink3","ink4","accent","accentHover","accentInk","accentSoft","accentTint","focus","positive","positiveSoft","warning","warningSoft","error","errorSoft","info","infoSoft"];
    var names={
      background:["--c-bg","--bg"],surface:["--c-surface","--surface"],surface2:["--c-surface-2","--surface-2"],surface3:["--c-surface-3"],
      line:["--c-line","--border"],line2:["--c-line-2","--border-strong"],ink:["--c-ink","--text"],ink2:["--c-ink-2"],
      ink3:["--c-ink-3","--text-muted","--c-mute"],ink4:["--c-ink-4","--text-subtle","--c-mute-2"],accent:["--c-accent"],
      accentHover:["--c-accent-hover"],accentInk:["--c-accent-ink"],accentSoft:["--c-accent-soft"],accentTint:["--c-accent-tint"],
      focus:["--c-focus"],positive:["--c-pos","--c-green"],positiveSoft:["--c-pos-soft","--c-ok-soft","--c-green-soft"],
      warning:["--c-warn"],warningSoft:["--c-warn-soft"],error:["--c-err","--c-neg","--c-red"],
      errorSoft:["--c-err-soft","--c-neg-soft","--c-red-soft"],info:["--c-info","--c-blue"],infoSoft:["--c-info-soft","--c-blue-soft"]
    };
    function declarations(mode){
      var tokens=scheme[mode];
      if(!tokens||Object.keys(tokens).length!==keys.length)return null;
      var out=[];
      for(var i=0;i<keys.length;i++){
        var key=keys[i],color=tokens[key];
        if(!/^#[0-9A-F]{6}$/.test(color||""))return null;
        for(var j=0;j<names[key].length;j++)out.push(names[key][j]+":"+color);
      }
      return out.join(";");
    }
    var light=declarations("light"),dark=declarations("dark");
    if(!light||!dark)return;
    var style=ensure("bowhead-team-brand-tokens","style");
    style.textContent=':root[data-theme="light"]{'+light+'}:root[data-theme="dark"]{'+dark+'}';
    for(var k=0;k<keys.length;k++)for(var n=0;n<names[keys[k]].length;n++)root.style.removeProperty(names[keys[k]][n]);
    root.dataset.brandChecksum=scheme.checksum;
    root.dataset.brandTeam=team;
    root.dataset.brandRevision=typeof meta.revisionId==="string"?meta.revisionId:"";
    if(brand&&typeof brand.applicationName==="string"&&brand.applicationName.length>0&&brand.applicationName.length<=80&&!/[\u0000-\u001F\u007F\u202A-\u202E\u2066-\u2069]/.test(brand.applicationName)){
      root.dataset.applicationName=brand.applicationName;
      var fav=brand.favicon;
      var faviconHref="";
      if(fav&&fav.kind==="asset"&&/^art_[0-9a-f]{24}$/.test(fav.artifactId||"")&&fav.path==="/api/v1/branding/assets/"+fav.artifactId){
        faviconHref=prefix+fav.path;
      }else if(meta.configurationType==="bowhead"||meta.configurationType==="legacy"){
        faviconHref=prefix+"/bowhead-icon.svg";
      }else if(fav&&fav.kind==="generatedInitial"&&typeof fav.text==="string"&&Array.from(fav.text).length===1){
        faviconHref=generatedFavicon(fav.text);
      }else if(fav&&fav.kind==="text"){
        faviconHref=generatedFavicon(brand.applicationName);
      }
      if(faviconHref)applyMetadata(brand.applicationName,faviconHref,"authenticated-cache",meta);
    }
    window.__BOWHEAD_PREHYDRATED_BRAND__=response;
  } catch (_) {}
})();`;
