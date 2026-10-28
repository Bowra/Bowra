import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isVideoPreviewMime,
  VideoArtifactPreview,
} from "./video-artifact-preview";

vi.mock("next-intl", () => ({
  useTranslations: () => (
    key: string,
    values?: Record<string, unknown>,
  ) => {
    const labels: Record<string, string> = {
      video_download_hint: "Download the original",
      video_player_label: `Video player for ${values?.filename ?? ""}`,
      video_play: `Play ${values?.filename ?? ""}`,
      video_ready: "Click to play",
      video_speed: "Speed",
      video_unavailable: "This video could not be played",
    };
    return labels[key] ?? key;
  },
}));

describe("VideoArtifactPreview", () => {
  let play: ReturnType<typeof vi.spyOn>;
  let pause: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
    pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("accepts only the explicitly supported video MIME types", () => {
    expect(isVideoPreviewMime("video/mp4")).toBe(true);
    expect(isVideoPreviewMime("VIDEO/WEBM")).toBe(true);
    expect(isVideoPreviewMime("video/ogg")).toBe(true);
    expect(isVideoPreviewMime("video/quicktime")).toBe(false);
    expect(isVideoPreviewMime("text/html")).toBe(false);
  });

  it("uses native controls, metadata preload, inline playback, and no autoplay", () => {
    render(
      <VideoArtifactPreview
        src="/api/video?disposition=inline"
        title="result.mp4"
        mime="video/mp4"
      />,
    );

    const player = screen.getByLabelText("Video player for result.mp4");
    expect(player).toHaveAttribute("controls");
    expect(player).toHaveAttribute("preload", "metadata");
    expect(player).toHaveAttribute("playsinline");
    expect(player).not.toHaveAttribute("autoplay");
    expect(player).toHaveAttribute(
      "src",
      "/api/video?disposition=inline",
    );
  });

  it("starts only after the neutral cover is activated", () => {
    render(
      <VideoArtifactPreview
        src="/api/video"
        title="result.mp4"
        mime="video/mp4"
      />,
    );

    expect(play).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Play result.mp4" }));
    expect(play).toHaveBeenCalledTimes(1);
  });

  it("pauses when hidden and when the viewer unmounts", () => {
    const rendered = render(
      <VideoArtifactPreview
        src="/api/video"
        title="result.mp4"
        mime="video/mp4"
      />,
    );
    const player = screen.getByLabelText("Video player for result.mp4");
    Object.defineProperty(player, "paused", {
      configurable: true,
      value: false,
    });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });

    fireEvent(document, new Event("visibilitychange"));
    expect(pause).toHaveBeenCalledTimes(1);
    rendered.unmount();
    expect(pause).toHaveBeenCalledTimes(2);
  });

  it("applies the selected playback rate to the media element", () => {
    render(
      <VideoArtifactPreview
        src="/api/video"
        title="result.mp4"
        mime="video/mp4"
      />,
    );
    const player = screen.getByLabelText(
      "Video player for result.mp4",
    ) as HTMLVideoElement;

    fireEvent.change(screen.getByLabelText("Speed"), {
      target: { value: "1.5" },
    });
    expect(player.playbackRate).toBe(1.5);
  });

  it("restores and republishes playback state across viewer remounts", () => {
    const onPlaybackStateChange = vi.fn();
    render(
      <VideoArtifactPreview
        src="/api/video"
        title="result.mp4"
        mime="video/mp4"
        resumeState={{
          currentTime: 42,
          volume: 0.4,
          muted: true,
          playbackRate: 1.5,
          started: true,
        }}
        onPlaybackStateChange={onPlaybackStateChange}
      />,
    );
    const player = screen.getByLabelText(
      "Video player for result.mp4",
    ) as HTMLVideoElement;
    Object.defineProperty(player, "duration", {
      configurable: true,
      value: 120,
    });
    fireEvent.loadedMetadata(player);

    expect(player.currentTime).toBe(42);
    expect(player.volume).toBe(0.4);
    expect(player.muted).toBe(true);
    expect(player.playbackRate).toBe(1.5);
    player.currentTime = 43;
    fireEvent.timeUpdate(player);
    expect(onPlaybackStateChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ currentTime: 43, started: true }),
    );
  });

  it("shows a generic recovery state on decoder errors", () => {
    render(
      <VideoArtifactPreview
        src="/api/video"
        title="result.mp4"
        mime="video/mp4"
      />,
    );

    fireEvent.error(screen.getByLabelText("Video player for result.mp4"));
    expect(
      screen.getByRole("alert"),
    ).toHaveTextContent("This video could not be played");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Download the original",
    );
  });
});
