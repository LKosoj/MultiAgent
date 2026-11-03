"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { BackgroundQueue } from "./backgroundQueue";
import type { StorybookClient } from "./client";
import { createFilmLoader, FilmAborted } from "./filmLoader";
import {
  changedText,
  filmErrorText,
  loadAreaVersions,
  musicDialogView,
  previewRegen,
  rebuildMontage,
  startAfterConfirm,
  type AreaVersion,
  type MusicDialogView,
} from "./filmRegen";
import { FILM_TEXTS } from "./filmTexts";
import {
  defaultSource,
  filmBanners,
  isMontageRunning,
  resolveFilmState,
  forbiddenAfter,
  runTransition,
  type FilmBanner,
  type FilmScreenState,
  type FilmSource,
} from "./filmView";
import type { SubtitlesResult, TimelineResult } from "./media";
import type { RegeneratePreviewResult } from "./review";
import { isRunBusy } from "./shotsView";
import type { RunState } from "./status";
import { BUSY_HINT, NO_RIGHTS_TEXT } from "./texts";
import { classifyStorybookError } from "./uiErrors";
import { useShotMedia, type ShotMediaApi } from "./useShotMedia";

type Say = (text: string, type?: "error" | "success" | "info") => void;

export type MusicPhase = "idle" | "previewing" | "ready" | "starting";
export type MusicApi = {
  open: boolean;
  phase: MusicPhase;
  view: MusicDialogView | null;
  error: string | null;
  begin: () => void;
  start: () => Promise<void>;
  close: () => void;
};

export type VersionsState = { music: AreaVersion[] | null; film: AreaVersion[] | null; error: string | null };

export type FilmApi = {
  projectId: string;
  state: FilmScreenState;
  timeline: TimelineResult | null;
  subtitles: SubtitlesResult | null;
  subtitlesFailed: boolean;
  error: string | null;
  // повторное чтение не удалось: данные прежние
  refreshFailed: boolean;
  reload: () => void;
  media: ShotMediaApi;
  source: FilmSource;
  setSource: (s: FilmSource) => void;
  banners: FilmBanner[];
  // почему кнопки действий отключены; null - можно
  actionBlock: string | null;
  rebuilding: boolean;
  rebuild: () => Promise<void>;
  music: MusicApi;
  versions: VersionsState;
  loadVersions: () => void;
  resolution: { width: number; height: number } | null;
  setResolution: (r: { width: number; height: number } | null) => void;
};

type Args = {
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  active: boolean;
  runState: RunState | undefined;
  runSteps: readonly string[] | null | undefined;
  say: Say;
  // свежее состояние прогона нужно оболочке сразу после запуска
  onRunStarted: () => void;
};

export const useFilm = ({ client, background, runUser, projectId, active, runState, runSteps, say, onRunStarted }: Args): FilmApi => {
  const [timeline, setTimeline] = useState<TimelineResult | null>(null);
  const [subtitles, setSubtitles] = useState<SubtitlesResult | null>(null);
  const [subtitlesFailed, setSubtitlesFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [sourceChoice, setSourceChoice] = useState<FilmSource | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [resolution, setResolution] = useState<{ width: number; height: number } | null>(null);
  const [versions, setVersions] = useState<VersionsState>({ music: null, film: null, error: null });
  const [musicOpen, setMusicOpen] = useState(false);
  const [musicPhase, setMusicPhase] = useState<MusicPhase>("idle");
  const [musicPreview, setMusicPreview] = useState<RegeneratePreviewResult | null>(null);
  const [musicError, setMusicError] = useState<string | null>(null);
  const alive = useRef(true);
  const musicSeq = useRef(0);
  const sayRef = useRef(say);
  const startedRef = useRef(onRunStarted);
  useEffect(() => {
    sayRef.current = say;
    startedRef.current = onRunStarted;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const media = useShotMedia({ client, background, projectId, enabled: active });

  // загрузка таймлайна и субтитров; смена проекта и уход с экрана прерывают повторы
  useEffect(() => {
    if (!active) return;
    const ctl = new AbortController();
    setError(null);
    let gotData = false;
    const loader = createFilmLoader({
      client,
      projectId,
      signal: ctl.signal,
      onUpdate: (d) => {
        if (ctl.signal.aborted) return;
        setTimeline(d.timeline);
        setSubtitles(d.subtitles);
        setSubtitlesFailed(d.subtitlesFailed);
        setRefreshFailed(!!d.refreshFailed);
        gotData = true;
        setForbidden((f) => forbiddenAfter(f, "load_ok"));
      },
    });
    runUser(() => loader.load()).catch((err: unknown) => {
      if (ctl.signal.aborted || err instanceof FilmAborted) return;
      if (gotData) {
        setRefreshFailed(true);
        return;
      }
      setError(classifyStorybookError(err).text);
    });
    return () => ctl.abort();
  }, [client, runUser, projectId, active, reloadKey]);

  // другой проект: старые данные не показываем
  useEffect(() => {
    setTimeline(null);
    setSubtitles(null);
    setError(null);
    setRefreshFailed(false);
    setSourceChoice(null);
    setResolution(null);
    setVersions({ music: null, film: null, error: null });
  }, [projectId]);

  const reload = useCallback(() => setReloadKey((n) => n + 1), []);

  // прогон закончился: перечитываем экран; после успешного конца - тост
  const prevRun = useRef<RunState | undefined>(runState);
  useEffect(() => {
    const t = runTransition(prevRun.current, runState);
    prevRun.current = runState;
    if (t.toast === "done") sayRef.current(FILM_TEXTS.toast_done, "success");
    if (t.reload) {
      setReloadKey((n) => n + 1);
      media.bust();
    }
  }, [runState, media]);

  const runBusy = isRunBusy(runState);
  const actionBlock = forbidden ? NO_RIGHTS_TEXT : runBusy ? BUSY_HINT : null;

  const noteError = useCallback((err: unknown): string => {
    const c = classifyStorybookError(err);
    if (c.kind === "forbidden") setForbidden(true);
    return c.kind === "locked" || c.kind === "forbidden" ? c.text : filmErrorText(err);
  }, []);

  const rebuild = useCallback(async () => {
    if (actionBlock || rebuilding) return;
    setRebuilding(true);
    try {
      const out = await runUser(() => rebuildMontage(client, projectId));
      if (!alive.current) return;
      if (out.kind === "started") {
        sayRef.current(FILM_TEXTS.toast_rebuilding, "success");
        startedRef.current();
      } else if (out.kind === "blocked") {
        sayRef.current(out.preview.problems.find((p) => p.level === "error")?.message ?? FILM_TEXTS.rebuild_blocked, "error");
      } else if (out.kind === "needs_confirm") {
        sayRef.current(FILM_TEXTS.rebuild_confirm_needed, "error");
      } else {
        sayRef.current(changedText(out.reason), "error");
      }
    } catch (err) {
      if (alive.current) sayRef.current(noteError(err), "error");
    } finally {
      if (alive.current) setRebuilding(false);
    }
  }, [actionBlock, rebuilding, runUser, client, projectId, noteError]);

  const begin = useCallback(async () => {
    if (actionBlock) return;
    const my = ++musicSeq.current;
    setMusicOpen(true);
    setMusicPhase("previewing");
    setMusicError(null);
    setMusicPreview(null);
    try {
      const preview = await runUser(() => previewRegen(client, projectId, "music"));
      if (!alive.current || my !== musicSeq.current) return;
      setMusicPreview(preview);
    } catch (err) {
      if (!alive.current || my !== musicSeq.current) return;
      setMusicError(noteError(err));
    }
    setMusicPhase("ready");
  }, [actionBlock, runUser, client, projectId, noteError]);

  const closeMusic = useCallback(() => {
    musicSeq.current += 1;
    setMusicOpen(false);
    setMusicPhase("idle");
    setMusicPreview(null);
    setMusicError(null);
  }, []);

  const startMusic = useCallback(async () => {
    if (!musicPreview || musicPhase !== "ready") return;
    setMusicPhase("starting");
    setMusicError(null);
    try {
      const out = await runUser(() => startAfterConfirm(client, projectId, "music", musicPreview));
      if (!alive.current) return;
      if (out.kind === "started") {
        sayRef.current(FILM_TEXTS.toast_music, "success");
        closeMusic();
        startedRef.current();
        return;
      }
      setMusicPreview(out.preview);
      if (out.kind === "changed") setMusicError(changedText(out.reason));
    } catch (err) {
      if (!alive.current) return;
      setMusicError(noteError(err));
    }
    setMusicPhase("ready");
  }, [musicPreview, musicPhase, runUser, client, projectId, closeMusic, noteError]);

  const loadVersions = useCallback(() => {
    background
      .run(async () => ({
        music: await loadAreaVersions(client, projectId, "music"),
        film: await loadAreaVersions(client, projectId, "film"),
      }))
      .then((v) => alive.current && setVersions({ ...v, error: null }))
      .catch((err: unknown) => alive.current && setVersions((cur) => ({ ...cur, error: classifyStorybookError(err).text })));
  }, [background, client, projectId]);

  useEffect(() => {
    if (active && timeline) loadVersions();
    // версии перечитываются вместе с экраном
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, projectId, reloadKey, timeline === null]);

  const source = sourceChoice ?? (timeline ? defaultSource(timeline) : "film");
  const state = resolveFilmState({ timeline, error });

  return {
    projectId,
    state,
    timeline,
    subtitles,
    subtitlesFailed,
    refreshFailed,
    error,
    reload,
    media,
    source,
    setSource: setSourceChoice,
    banners: filmBanners(timeline, isMontageRunning(runState, runSteps)),
    actionBlock,
    rebuilding,
    rebuild,
    music: {
      open: musicOpen,
      phase: musicPhase,
      view: musicPreview ? musicDialogView(musicPreview) : null,
      error: musicError,
      begin: () => void begin(),
      start: startMusic,
      close: closeMusic,
    },
    versions,
    loadVersions,
    resolution,
    setResolution,
  };
};
