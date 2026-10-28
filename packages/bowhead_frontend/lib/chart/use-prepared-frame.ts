import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/** One paused clock per root; visibility returns never resume playback. */
export function usePreparedFrame(enabled: boolean, epoch: string, keys: readonly (string | number)[], staticKey: string | number | undefined, host: RefObject<HTMLElement | null>) {
  const initial = Math.max(0, keys.indexOf(staticKey!));
  const [local, setLocal] = useState<{ epoch: string; index: number; playing: boolean } | null>(null);
  const index = enabled && local?.epoch === epoch && local.index < keys.length ? local.index : initial;
  const playing = enabled && local?.epoch === epoch && local.playing && index < keys.length - 1;
  const visible = useRef(true);
  const pause = useCallback(() => setLocal((state) => state?.playing ? { ...state, playing: false } : state), []);
  const reset = useCallback(() => setLocal(null), []);
  const seek = useCallback((index: number) => { if (enabled && Number.isSafeInteger(index) && index >= 0 && index < keys.length) setLocal({ epoch, index, playing: false }); }, [enabled, epoch, keys.length]);
  const play = useCallback(() => {
    if (enabled && visible.current && !document.hidden && index < keys.length - 1) setLocal({ epoch, index, playing: true });
  }, [enabled, epoch, index, keys.length]);
  useEffect(() => {
    if (!enabled) return;
    const onVisibility = () => { if (document.hidden) pause(); };
    document.addEventListener("visibilitychange", onVisibility);
    const observer = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries) => {
      visible.current = entries.some((entry) => entry.isIntersecting);
      if (!visible.current) pause();
    }) : null;
    if (host.current) observer?.observe(host.current);
    return () => { document.removeEventListener("visibilitychange", onVisibility); observer?.disconnect(); };
  }, [enabled, epoch, host, pause]);
  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => setLocal((state) => state?.epoch === epoch && state.playing && visible.current && !document.hidden
      ? { ...state, index: Math.min(state.index + 1, keys.length - 1), playing: state.index + 1 < keys.length - 1 }
      : state ? { ...state, playing: false } : state), 1000);
    return () => window.clearTimeout(timer);
  }, [epoch, playing, index, keys.length]);
  return { index, key: keys[index], playing, changed: index !== initial, seek, play, pause, reset };
}
