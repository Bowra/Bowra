/**
 * Color-scheme following for public pages (the (public) layout): personal appearance settings are unavailable before sign-in, so follow the system `prefers-color-scheme`.
 *
 * The gateway's team-brand script runs first; a `data-theme` it wrote is kept. Without an injection (unregistered entry,
 * first activation, direct static access) this script writes it, so the product default brand also follows system dark mode, and updates when the system switches.
 * It only changes `<html>` attributes and sends no request; errors are silent and the page renders normally.
 * Internal export pages (`/internal/canvas-export/*`, also in the public layout) do not follow: exports must not change with the rendering host's color scheme.
 */
export const PUBLIC_COLOR_SCHEME_BOOTSTRAP_SCRIPT = String.raw`
(function(){
  try {
    if(/\/internal\/canvas-export\//.test(location.pathname))return;
    var root=document.documentElement;
    var query=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)");
    if(!root.dataset.theme){
      root.dataset.theme=query&&query.matches?"dark":"light";
      root.dataset.themeMode="system";
    }
    root.style.colorScheme=root.dataset.theme;
    if(query&&root.dataset.themeMode==="system"){
      var follow=function(event){
        var mode=event.matches?"dark":"light";
        root.dataset.theme=mode;
        root.style.colorScheme=mode;
      };
      if(query.addEventListener)query.addEventListener("change",follow);
      else if(query.addListener)query.addListener(follow);
    }
  } catch (_) {}
})();`;
