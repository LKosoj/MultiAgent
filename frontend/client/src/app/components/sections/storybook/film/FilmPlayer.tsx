"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import { FILM_TEXTS } from "../../../../lib/storybook/filmTexts";
import { sourceVideo, type FilmSource } from "../../../../lib/storybook/filmView";
import type { SubtitleCue, TimelineResult } from "../../../../lib/storybook/media";
import { cueAt } from "../../../../lib/storybook/media";
import { clampTime, formatPosition, nextOnError, playerStatus, runRetry } from "../../../../lib/storybook/playerModel";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";

export type PlayerHandle = {
  seek: (t: number) => void;
  seekBy: (delta: number) => void;
  toggle: () => void;
  mute: () => void;
  fullscreen: () => void;
};

type Props = {
  timeline: TimelineResult;
  cues: readonly SubtitleCue[];
  media: ShotMediaApi;
  source: FilmSource;
  onSource: (s: FilmSource) => void;
  time: number;
  onTime: (t: number) => void;
  onResolution: (r: { width: number; height: number } | null) => void;
  handleRef: React.MutableRefObject<PlayerHandle | null>;
};

// Плеер на HTML5-видео с собственными кнопками: так клавиши и доступность совпадают с остальным экраном
export function FilmPlayer({ timeline, cues, media, source, onSource, time, onTime, onResolution, handleRef }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(1);
  const [duration, setDuration] = useState(0);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [retrying, setRetrying] = useState(false);

  const video = sourceVideo(timeline, source);
  const src = video ? media.media(video.path, video.modified) : null;
  const fileDuration = video?.duration_s ?? 0;
  const total = duration > 0 ? duration : fileDuration;
  const cue = cueAt(cues as SubtitleCue[], time);

  // другой файл: ошибка и повтор сбрасываются (смена одного токена файл не меняет)
  const filePath = video?.path ?? null;
  useEffect(() => {
    setFailed(false);
    setAttempt(0);
    setPlaying(false);
  }, [filePath]);

  const seek = useCallback(
    (t: number) => {
      const v = videoRef.current;
      const next = clampTime(t, total);
      if (v) v.currentTime = next;
      onTime(next);
    },
    [total, onTime],
  );
  const toggle = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play().catch(() => setFailed(true));
    else v.pause();
  }, []);
  const mute = useCallback(() => {
    const v = videoRef.current;
    const next = !(v?.muted ?? muted);
    if (v) v.muted = next;
    setMuted(next);
  }, [muted]);
  const fullscreen = useCallback(() => {
    void wrapRef.current?.requestFullscreen?.().catch(() => undefined);
  }, []);

  useEffect(() => {
    handleRef.current = {
      seek,
      seekBy: (delta) => seek((videoRef.current?.currentTime ?? time) + delta),
      toggle,
      mute,
      fullscreen,
    };
    return () => {
      handleRef.current = null;
    };
  }, [handleRef, seek, toggle, mute, fullscreen, time]);

  const onError = () => {
    // один повтор после обновления токена, затем текст ошибки и кнопка «Повторить»
    if (nextOnError(attempt) === "retry" && src) {
      setAttempt(attempt + 1);
      media.onImageError({ currentTarget: { src } });
      return;
    }
    setFailed(true);
  };

  const retry = () => {
    // новый токен принудительно; видео пересоздаём только когда он получен, иначе оно упадёт со старым адресом
    void runRetry(() => media.refresh(), {
      start: () => setRetrying(true),
      done: () => {
        setAttempt(0);
        setFailed(false);
        setRetrying(false);
      },
      fail: () => {
        setFailed(true);
        setRetrying(false);
      },
    });
  };

  const status = playerStatus({ hasVideo: !!video, src, failed, mediaReady: media.ready });
  const bad = status === "failed";
  const shown = retrying ? "loading" : status;

  const hasPhoto = !!sourceVideo(timeline, "photo");
  const hasFilm = !!sourceVideo(timeline, "film");

  return (
    <div className="sb-film-player" ref={wrapRef}>
      <div className="sb-film-sources" role="group" aria-label={FILM_TEXTS.source_label}>
        <button type="button" className="button ghost" aria-pressed={source === "film"} disabled={!hasFilm} onClick={() => onSource("film")}>
          {FILM_TEXTS.source_film}
        </button>
        <button type="button" className="button ghost" aria-pressed={source === "photo"} disabled={!hasPhoto} onClick={() => onSource("photo")}>
          {FILM_TEXTS.source_photo}
        </button>
      </div>
      {src && !bad && !retrying ? (
        <video
          key={`${src}#${attempt}`}
          ref={videoRef}
          className="sb-film-video"
          src={src}
          preload="metadata"
          playsInline
          onClick={toggle}
          onTimeUpdate={(e) => onTime(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => {
            setDuration(e.currentTarget.duration || 0);
            onResolution({ width: e.currentTarget.videoWidth, height: e.currentTarget.videoHeight });
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onError={onError}
        />
      ) : (
        <div className="sb-film-video sb-film-video-empty" role={shown === "failed" ? "alert" : undefined}>
          <p>{shown === "failed" ? FILM_TEXTS.video_failed : shown === "loading" ? FILM_TEXTS.loading : FILM_TEXTS.empty}</p>
          {bad ? (
            <button type="button" className="button" disabled={retrying} onClick={retry}>
              {FILM_TEXTS.retry}
            </button>
          ) : null}
          {bad && !retrying && src ? (
            <a href={src} target="_blank" rel="noreferrer">
              {FILM_TEXTS.video_open_tab}
            </a>
          ) : null}
        </div>
      )}
      <p className="sb-film-cue" aria-live="polite">
        {cue ? cue.text : " "}
      </p>
      <div className="sb-film-controls">
        <button type="button" className="button" onClick={toggle} disabled={!src || bad}>
          {playing ? FILM_TEXTS.pause : FILM_TEXTS.play}
        </button>
        <input
          type="range"
          className="sb-film-seek"
          aria-label={FILM_TEXTS.position}
          min={0}
          max={Math.max(total, 0)}
          step={0.1}
          value={Math.min(time, Math.max(total, 0))}
          disabled={!src || bad}
          onChange={(e) => seek(Number(e.target.value))}
        />
        <span className="sb-film-position">{formatPosition(time, total)}</span>
        <button type="button" className="button ghost" onClick={mute} disabled={!src || bad}>
          {muted ? FILM_TEXTS.unmute : FILM_TEXTS.mute}
        </button>
        <input
          type="range"
          className="sb-film-volume"
          aria-label={FILM_TEXTS.volume}
          min={0}
          max={1}
          step={0.05}
          value={volume}
          disabled={!src || bad}
          onChange={(e) => {
            const v = Number(e.target.value);
            setVolume(v);
            if (videoRef.current) videoRef.current.volume = v;
          }}
        />
        <button type="button" className="button ghost" onClick={fullscreen} disabled={!src || bad}>
          {FILM_TEXTS.fullscreen}
        </button>
      </div>
    </div>
  );
}
