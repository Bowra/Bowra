"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Play } from "@/components/icons";

export const VIDEO_PREVIEW_MIMES = new Set([
  "video/mp4",
  "video/webm",
  "video/ogg",
]);

export function isVideoPreviewMime(mime: string | null | undefined): boolean {
  return VIDEO_PREVIEW_MIMES.has((mime ?? "").toLowerCase());
}

export type VideoPlaybackState = {
  currentTime: number;
  volume: number;
  muted: boolean;
  playbackRate: number;
  started: boolean;
};

export function VideoArtifactPreview({
  src,
  title,
  mime,
  expanded = false,
  onError,
  resumeState,
  onPlaybackStateChange,
}: {
  src: string;
  title: string;
  mime: string;
  expanded?: boolean;
  onError?: () => void;
  resumeState?: VideoPlaybackState;
  onPlaybackStateChange?: (state: VideoPlaybackState) => void;
}) {
  const t = useTranslations("artifact_viewer");
  const videoRef = useRef<HTMLVideoElement>(null);
  const [started, setStarted] = useState(resumeState?.started ?? false);
  const startedRef = useRef(resumeState?.started ?? false);
  const [failed, setFailed] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  const [playbackRate, setPlaybackRate] = useState(resumeState?.playbackRate ?? 1);
  const errorReportedRef = useRef(false);
  const playbackStateCallbackRef = useRef(onPlaybackStateChange);

  useEffect(() => {
    playbackStateCallbackRef.current = onPlaybackStateChange;
  }, [onPlaybackStateChange]);

  useEffect(() => {
    errorReportedRef.current = false;
  }, [src]);

  const reportError = () => {
    setFailed(true);
    if (errorReportedRef.current) return;
    errorReportedRef.current = true;
    onError?.();
  };

  useEffect(() => {
    const player = videoRef.current;
    const publishState = () => {
      if (!player) return;
      playbackStateCallbackRef.current?.({
        currentTime: Number.isFinite(player.currentTime) ? player.currentTime : 0,
        volume: player.volume,
        muted: player.muted,
        playbackRate: player.playbackRate,
        started: startedRef.current,
      });
    };
    const pause = () => {
      if (player && !player.paused) player.pause();
      publishState();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") pause();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", pause);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pagehide", pause);
      pause();
    };
  }, []);

  const startPlayback = () => {
    const player = videoRef.current;
    if (!player) return;
    setFailed(false);
    try {
      void player.play().catch(reportError);
    } catch {
      reportError();
    }
  };

  return (
    <div
      className={
        expanded
          ? "video-artifact-preview expanded"
          : "video-artifact-preview"
      }
    >
      <div className="video-artifact-stage">
        <video
          ref={videoRef}
          src={src}
          controls
          playsInline
          preload="metadata"
          aria-label={t("video_player_label", { filename: title })}
          onPlay={() => {
            startedRef.current = true;
            setStarted(true);
          }}
          onLoadedMetadata={(event) => {
            const player = event.currentTarget;
            const seconds = player.duration;
            setDuration(Number.isFinite(seconds) ? seconds : null);
            if (resumeState) {
              if (Number.isFinite(resumeState.currentTime)) {
                player.currentTime = Math.min(
                  Math.max(0, resumeState.currentTime),
                  Number.isFinite(seconds) ? seconds : resumeState.currentTime,
                );
              }
              player.volume = resumeState.volume;
              player.muted = resumeState.muted;
              player.playbackRate = resumeState.playbackRate;
            }
          }}
          onTimeUpdate={(event) => onPlaybackStateChange?.({
            currentTime: event.currentTarget.currentTime,
            volume: event.currentTarget.volume,
            muted: event.currentTarget.muted,
            playbackRate: event.currentTarget.playbackRate,
            started: true,
          })}
          onVolumeChange={(event) => onPlaybackStateChange?.({
            currentTime: event.currentTarget.currentTime,
            volume: event.currentTarget.volume,
            muted: event.currentTarget.muted,
            playbackRate: event.currentTarget.playbackRate,
            started: startedRef.current,
          })}
          onError={reportError}
        />
        {!started && !failed ? (
          <button
            type="button"
            className="video-artifact-cover"
            onClick={startPlayback}
            aria-label={t("video_play", { filename: title })}
          >
            <span className="video-artifact-cover-mark">
              <Play size={23} aria-hidden="true" />
            </span>
            <strong>{title}</strong>
            <span>{t("video_ready")}</span>
          </button>
        ) : null}
        {failed ? (
          <div className="video-artifact-error" role="alert">
            <strong>{t("video_unavailable")}</strong>
            <span>{t("video_download_hint")}</span>
          </div>
        ) : null}
      </div>
      <div className="video-artifact-meta">
        <span>{mime}</span>
        {duration !== null ? <span>{formatDuration(duration)}</span> : null}
        <label>
          <span>{t("video_speed")}</span>
          <select
            value={playbackRate}
            onChange={(event) => {
              const next = Number(event.target.value);
              setPlaybackRate(next);
              if (videoRef.current) videoRef.current.playbackRate = next;
            }}
          >
            {[0.5, 1, 1.5, 2].map((rate) => (
              <option key={rate} value={rate}>{rate}×</option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

function formatDuration(totalSeconds: number): string {
  const rounded = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const seconds = rounded % 60;
  if (hours > 0) {
    return [hours, minutes, seconds]
      .map((value) => String(value).padStart(2, "0"))
      .join(":");
  }
  return [minutes, seconds]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}
