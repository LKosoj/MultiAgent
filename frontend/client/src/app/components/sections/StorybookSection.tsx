"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import { classifyStorybookError } from "../../lib/storybook/uiErrors";
import { runCancel, runSetPause } from "../../lib/storybook/runs";
import { NO_CONNECTION_TEXT, toasts } from "../../lib/storybook/texts";
import type { NextActionTarget } from "../../lib/storybook/status";
import type { ShortcutAction } from "../../lib/storybook/shortcuts";
import { createEditJobs } from "../../lib/storybook/imageEditJob";
import { useProjectActions } from "../../lib/storybook/useProjectActions";
import { useProjectStatus } from "../../lib/storybook/useProjectStatus";
import { useProjects } from "../../lib/storybook/useProjects";
import { useStorybookClient, type RunServiceAction } from "../../lib/storybook/useStorybookClient";
import { useStorybookNav } from "../../lib/storybook/useStorybookNav";
import { refreshKindOf } from "../../lib/storybook/screenKeys";
import { useStorybookShortcuts } from "../../lib/storybook/useStorybookShortcuts";
import { NoAccess } from "./storybook/NoAccess";
import { ProjectPalette } from "./storybook/ProjectPalette";
import { StatusLine, type StatusMessage } from "./storybook/StatusLine";
import { StorybookNav } from "./storybook/StorybookNav";
import { StorybookOverviewTab } from "./storybook/StorybookOverviewTab";
import { StorybookBlockoutTab } from "./storybook/blockout/StorybookBlockoutTab";
import { StorybookFilesTab } from "./storybook/files/StorybookFilesTab";
import { FilmReview } from "./storybook/film/FilmReview";
import { StorybookFilmTab } from "./storybook/film/StorybookFilmTab";
import { StorybookSettingsTab } from "./storybook/settings/StorybookSettingsTab";
import { StorybookGenerationTab } from "./storybook/generation/StorybookGenerationTab";
import { StorybookShotsTab } from "./storybook/shots/StorybookShotsTab";
import { StorybookProjectsTab, type ProjectsCommand } from "./storybook/StorybookProjectsTab";
import { SeriesSettings } from "./storybook/SeriesSettings";
import { SeriesSounds } from "./storybook/SeriesSounds";
import { SeriesSpeech } from "./storybook/SeriesSpeech";
import { SeriesList } from "./storybook/SeriesList";
import { StorybookTopBar } from "./storybook/StorybookTopBar";

export type SectionMode = "no_access" | "dormant" | "live";

// Не админ видит только «нет доступа»; до первого открытия раздел ничего не рисует и не ходит в сеть
export const resolveSectionMode = (a: { isAdmin: boolean; active: boolean; everActive: boolean }): SectionMode =>
  !a.isAdmin ? "no_access" : a.active || a.everActive ? "live" : "dormant";

// projectId null: у workflow нет своего проекта, открываем «Проекты»
export type OpenRequest = { projectId: string | null; n: number } | null;

type Props = {
  runServiceAction: RunServiceAction;
  backendUrl: string;
  isAdmin: boolean;
  active: boolean;
  notify: (message: string, type?: "error" | "success" | "info") => void;
  openRequest: OpenRequest;
};

export function StorybookSection(props: Props) {
  const [everActive, setEverActive] = useState(props.active);
  useEffect(() => {
    if (props.active) setEverActive(true);
  }, [props.active]);
  const mode = resolveSectionMode({ isAdmin: props.isAdmin, active: props.active, everActive });
  if (mode === "no_access") return <NoAccess />;
  if (mode === "dormant") return null;
  return <LiveSection {...props} />;
}

function LiveSection({ runServiceAction, backendUrl, active, notify, openRequest }: Props) {
  const { client, resetClient, background, runUser } = useStorybookClient(runServiceAction, backendUrl);
  const navApi = useStorybookNav();
  const [selection, setSelection] = useState<{ projectId: string; seriesId: string } | null>(null);
  const { nav } = navApi;
  const seriesId = selection?.projectId === nav.projectId ? selection.seriesId : undefined;
  const { client: seriesClient, resetClient: resetSeriesClient } = useStorybookClient(runServiceAction, backendUrl, seriesId);
  const rootRef = useRef<HTMLDivElement>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [command, setCommand] = useState<ProjectsCommand>(null);
  const [message, setMessage] = useState<StatusMessage | null>(null);
  const commandSeq = useRef(0);

  // ожидание правки картинки ИИ: один объект на проект, переживает смену вкладок
  // (при смене проекта вкладки пересоздаются по key, старое ожидание обрывается)
  const editJobsRef = useRef<{ id: string | null; jobs: ReturnType<typeof createEditJobs> } | null>(null);
  const editScope = `${nav.projectId}:${seriesId}`;
  if (!editJobsRef.current || editJobsRef.current.id !== editScope) {
    editJobsRef.current?.jobs.dispose();
    editJobsRef.current = { id: editScope, jobs: createEditJobs() };
  }
  const editJobs = editJobsRef.current.jobs;

  const say = useCallback(
    (text: string, type: "error" | "success" | "info" = "info") => {
      setMessage({ text, at: Date.now() });
      notify(text, type);
    },
    [notify],
  );

  const projects = useProjects({ client, background, enabled: active });
  const { load, state: listState, projects: list } = projects;
  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  // выбранного проекта нет в списке: возвращаемся к «Проектам»
  const { reconcile } = navApi;
  useEffect(() => {
    if (listState === "ready") reconcile(list.map((p) => p.project_id));
  }, [listState, list, reconcile]);

  const status = useProjectStatus({
    client: seriesClient,
    scopeId: seriesId,
    projectId: nav.projectId,
    mode: nav.screen === "overview" ? "full" : "brief",
    enabled: active && nav.projectId !== null && !!seriesId,
  });

  const { openProject: navigateToProject, clearProject } = navApi;
  const openProject = useCallback((id: string) => { setSelection(null); navigateToProject(id); }, [navigateToProject]);
  const lastOpen = useRef(0);
  useEffect(() => {
    if (openRequest && openRequest.n !== lastOpen.current) {
      lastOpen.current = openRequest.n;
      if (openRequest.projectId) openProject(openRequest.projectId);
      else clearProject();
    }
  }, [openRequest, openProject, clearProject]);

  const actions = useProjectActions({
    client,
    runUser,
    enabled: active,
    notifySuccess: (text) => say(text, "success"),
    onProjectsChanged: load,
    onOpenProject: openProject,
    onSelectProject: setSelectedId,
    onProjectRemoved: (id) => {
      if (navApi.nav.projectId === id) navApi.clearProject();
      setSelectedId((cur) => (cur === id ? null : cur));
    },
  });

  // после сохранения настроек: смена папки проектов = другой набор проектов и другие токены медиа
  const onSettingsChanged = useCallback(
    (changed: string[]) => {
      if (changed.includes("projects_directory")) resetClient();
      void load();
    },
    [resetClient, load],
  );

  // счётчик F5 для экранов «Кадры», «Генерация», «Фильм», «Болванка»: они перечитывают свои данные
  const [refreshTick, setRefreshTick] = useState(0);
  const refresh = useCallback(() => {
    const netError = (e: { kind: string } | null) => !!e && (e.kind === "network" || e.kind === "timeout");
    const kind = refreshKindOf(nav.screen);
    if (kind === "tick") {
      if (netError(status.error)) resetClient();
      setRefreshTick((n) => n + 1);
      status.refresh();
    } else if (kind === "none") {
      // у «Файлов» и «Настроек» свой F5
    } else if (kind === "overview") {
      if (netError(status.error)) resetClient();
      status.refresh();
    } else {
      if (netError(projects.error)) resetClient();
      else void load();
    }
  }, [nav.screen, status, projects.error, resetClient, load]);

  // Пауза/Продолжить: без подтверждения (как в Tk)
  const pauseToggle = useCallback(
    async (action: "pause" | "resume") => {
      const id = nav.projectId;
      if (!id) return;
      try {
        await runUser(() => runSetPause(seriesClient, { project_id: id, paused: action === "pause" }));
        say(action === "pause" ? toasts.pauseRequested : toasts.resumed, "success");
      } catch (err) {
        say(classifyStorybookError(err).text, "error");
      }
      status.refresh();
    },
    [nav.projectId, seriesClient, runUser, say, status],
  );

  // Стоп: окно подтверждения в верхней полосе, здесь только вызов
  const stopRun = useCallback(async (): Promise<string | null> => {
    const id = nav.projectId;
    if (!id) return null;
    try {
      await runUser(() => runCancel(seriesClient, { project_id: id }));
    } catch (err) {
      return classifyStorybookError(err).text;
    }
    say(toasts.stopRequested, "success");
    status.refresh();
    return null;
  }, [nav.projectId, seriesClient, runUser, say, status]);

  const onShortcut = useCallback(
    (action: ShortcutAction) => {
      switch (action.type) {
        case "screen":
          navApi.goTo(action.screen);
          break;
        case "palette":
          setPaletteOpen(true);
          break;
        case "refresh":
          refresh();
          break;
        case "create_project":
        case "focus_search":
          commandSeq.current += 1;
          setCommand({ type: action.type, n: commandSeq.current });
          break;
      }
    },
    [navApi, refresh],
  );

  const getContext = useCallback(
    () => ({
      modalOpen: paletteOpen || document.querySelector("[data-sb-modal]") !== null,
      focusInside: !!rootRef.current && rootRef.current.contains(document.activeElement),
      screen: nav.screen,
      hasProject: nav.projectId !== null,
    }),
    [paletteOpen, nav.screen, nav.projectId],
  );
  useStorybookShortcuts({ enabled: active, getContext, onAction: onShortcut });

  const goTarget = (target: Pick<NextActionTarget, "screen"> & Partial<NextActionTarget>) =>
    navApi.applyTarget({ screen: target.screen, filter: target.filter ?? null, step_id: target.step_id ?? null, mode: target.mode ?? null });

  const offline = status.offline || (status.state === "error" && status.error?.kind === "network");
  // момент потери связи фиксируем один раз, чтобы время в строке не менялось при каждом рендере
  const offlineSince = useRef(0);
  if (!offline) offlineSince.current = 0;
  else if (offlineSince.current === 0) offlineSince.current = Date.now();
  const screen = nav.screen;

  return (
    <div className="sb-root" ref={rootRef} data-sb-screen={screen}>
      <StorybookTopBar
        projectId={nav.projectId}
        seriesId={seriesId}
        brief={status.brief}
        offline={offline}
        onOpenPalette={() => setPaletteOpen(true)}
        onOpenProjects={navApi.clearProject}
        onOpenGeneration={() => navApi.goTo("generation")}
        onRerun={(mode) => navApi.applyTarget({ screen: "generation", mode })}
        onPauseToggle={pauseToggle}
        onStop={stopRun}
      />
      <div className="sb-body">
        <StorybookNav
          screen={screen}
          projectId={nav.projectId}
          onGo={navApi.goTo}
          onBlocked={(hint) => say(hint, "info")}
        />
        <div className="sb-screen">
          {seriesId && nav.projectId && <button type="button" onClick={() => { setSelection(null); navApi.goTo("overview"); }}>К сериям проекта</button>}
          {nav.projectId && !seriesId && screen !== "projects" && screen !== "settings" ? (
            <SeriesList key={`${nav.projectId}:${seriesId}`} client={client} projectId={nav.projectId} onOpen={(id) => { setSelection({ projectId: nav.projectId!, seriesId: id }); navApi.goTo("overview"); }} />
          ) : screen === "projects" ? (
            <StorybookProjectsTab
              projects={projects}
              actions={actions}
              client={client}
              background={background}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onOpen={openProject}
              command={command}
              onOpenSettings={() => navApi.goTo("settings")}
              onRefresh={refresh}
            />
          ) : screen === "overview" && nav.projectId ? (
            <>
            <SeriesSettings key={`${nav.projectId}:${seriesId}:settings`} client={client} projectId={nav.projectId} seriesId={seriesId!} />
            <SeriesSpeech key={`${nav.projectId}:${seriesId}`} client={seriesClient} projectId={nav.projectId} />
            <StorybookOverviewTab
              api={status}
              onTarget={goTarget}
              onWatchFinal={() => navApi.goTo("film")}
              onGoGeneration={() => navApi.goTo("generation")}
              onBackToProjects={navApi.clearProject}
              onRefresh={refresh}
            />
            </>
          ) : screen === "shots" && nav.projectId ? (
            <StorybookShotsTab
              seriesId={seriesId!}
              editJobs={editJobs}
              key={`${nav.projectId}:${seriesId}`}
              refreshTick={refreshTick}
              client={seriesClient}
              background={background}
              runUser={runUser}
              projectId={nav.projectId}
              intent={nav.intent}
              runState={status.brief?.run.state}
              active={active}
              say={say}
              onGoGeneration={() => navApi.goTo("generation")}
              onGenerateShots={(keys) => navApi.applyTarget({ screen: "generation", mode: "regenerate_shots", shot_keys: keys })}
              onResetClient={resetSeriesClient}
            />
          ) : screen === "generation" && nav.projectId ? (
            <StorybookGenerationTab
              seriesId={seriesId!}
              key={`${nav.projectId}:${seriesId}`}
              refreshTick={refreshTick}
              client={seriesClient}
              runUser={runUser}
              projectId={nav.projectId}
              intent={nav.intent}
              active={active}
              isAdmin
              say={say}
              onOpenBlockout={() => navApi.goTo("blockout")}
            />
          ) : screen === "film" && nav.projectId ? (
            <>
            <SeriesSounds key={`${nav.projectId}:${seriesId}:sounds`} client={seriesClient} projectId={nav.projectId} />
            <FilmReview key={`${nav.projectId}:${seriesId}:review`} client={client} projectId={nav.projectId} seriesId={seriesId!} refreshTick={refreshTick} runState={status.brief?.run.state} />
            <StorybookFilmTab
              key={`${nav.projectId}:${seriesId}:film`}
              refreshTick={refreshTick}
              client={seriesClient}
              background={background}
              runUser={runUser}
              projectId={nav.projectId}
              projectName={status.brief?.project.title ?? nav.projectId}
              active={active}
              runState={status.brief?.run.state}
              runSteps={status.brief?.run.step ? [status.brief.run.step.id] : null}
              say={say}
              onGoGeneration={() => navApi.goTo("generation")}
              onOpenShot={navApi.openShot}
              onOpenFiles={() => navApi.goTo("files")}
              onRunStarted={status.refresh}
            />
            </>
          ) : screen === "blockout" && nav.projectId ? (
            <StorybookBlockoutTab
              key={`${nav.projectId}:${seriesId}`}
              refreshTick={refreshTick}
              client={seriesClient}
              background={background}
              runUser={runUser}
              projectId={nav.projectId}
              active={active}
              runState={status.brief?.run.state}
              say={say}
              onGoGeneration={() => navApi.goTo("generation")}
              onOpenShot={navApi.openShot}
              onRunStarted={status.refresh}
            />
          ) : screen === "files" && nav.projectId ? (
            <StorybookFilesTab
              editJobs={editJobs}
              key={`${nav.projectId}:${seriesId}`}
              client={seriesClient}
              background={background}
              runUser={runUser}
              projectId={nav.projectId}
              runState={status.brief?.run.state}
              active={active}
              say={say}
            />
          ) : screen === "settings" ? (
            <StorybookSettingsTab client={client} runUser={runUser} active={active} say={say} onSettingsChanged={onSettingsChanged} />
          ) : null}
        </div>
      </div>
      <StatusLine
        message={offline ? { text: NO_CONNECTION_TEXT, at: offlineSince.current } : message}
        onOpenJournal={() => navApi.goTo("generation")}
        journalEnabled={nav.projectId !== null}
      />
      {paletteOpen ? (
        <ProjectPalette
          projects={list}
          onPick={(id) => {
            setPaletteOpen(false);
            setSelectedId(id);
            openProject(id);
          }}
          onClose={() => setPaletteOpen(false)}
        />
      ) : null}
    </div>
  );
}
