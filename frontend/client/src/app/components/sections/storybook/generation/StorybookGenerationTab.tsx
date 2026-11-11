"use client";

import React, { useCallback, useMemo, useRef, useState } from "react";

import type { StorybookClient } from "../../../../lib/storybook/client";
import { confirmBody, collectNotes, bannersFor, isBusyState, modeLabel, settingsLocked, confirmKind, type IntentLike } from "../../../../lib/storybook/generationModel";
import { activeProvider, isDirty } from "../../../../lib/storybook/generationForm";
import { inflightWarning } from "../../../../lib/storybook/generationFlow";
import { gfmt, GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import { historyRows } from "../../../../lib/storybook/historyView";
import { formatShotMoney } from "../../../../lib/storybook/shotsView";
import { makeTitleOf, mergeStepRows, stepRows } from "../../../../lib/storybook/stepsView";
import { useGenerationFlow } from "../../../../lib/storybook/useGenerationFlow";
import { useRefreshTick } from "../../../../lib/storybook/useRefreshTick";
import { useRunSettings } from "../../../../lib/storybook/useRunSettings";
import { useRunStatus } from "../../../../lib/storybook/useRunStatus";
import { Banner } from "../Banner";
import { NoAccess } from "../NoAccess";
import { Skeleton } from "../Skeleton";
import { PreflightSummary } from "./PreflightSummary";
import { RunBanners } from "./RunBanners";
import { RunHistory } from "./RunHistory";
import { RunLog } from "./RunLog";
import { RunModeForm } from "./RunModeForm";
import { RunProgress } from "./RunProgress";
import { RunSettingsForm } from "./RunSettingsForm";
import { StartConfirmDialogs } from "./StartConfirmDialogs";
import { StepsTracker } from "./StepsTracker";

type Props = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  seriesId: string;
  intent: IntentLike;
  active: boolean;
  refreshTick: number;
  isAdmin: boolean;
  say: (text: string, type?: "error" | "success" | "info") => void;
  onOpenBlockout: () => void;
};

type Tab = "settings" | "summary" | "progress" | "log";
const TABS: { id: Tab; text: "tab_settings" | "tab_summary" | "tab_progress" | "tab_log" }[] = [
  { id: "settings", text: "tab_settings" },
  { id: "summary", text: "tab_summary" },
  { id: "progress", text: "tab_progress" },
  { id: "log", text: "tab_log" },
];

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

// Экран «Генерация»: режим и настройки слева, ход прогона, шаги и журнал справа
export function StorybookGenerationTab({ client, runUser, projectId, seriesId, intent, active, refreshTick, isAdmin, say, onOpenBlockout }: Props) {
  const runApi = useRunStatus({ client, projectId, enabled: active });
  const settings = useRunSettings({ client, runUser, projectId, storageId: `${projectId}:${seriesId}`, enabled: active });
  // F5: статус прогона перечитываем всегда, настройки - только если форма не тронута
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  useRefreshTick(refreshTick, () => {
    runApi.refresh();
    const s = settingsRef.current;
    if (!s.saved || !s.draft || !isDirty(s.saved, s.draft)) void s.reload();
  });
  const titleOf = useMemo(() => makeTitleOf(settings.steps), [settings.steps]);
  const flow = useGenerationFlow({
    client,
    runUser,
    projectId,
    storageId: `${projectId}:${seriesId}`,
    enabled: active,
    isAdmin,
    say,
    intent,
    titleOf,
    runApi,
    settings,
  });
  const [tab, setTab] = useState<Tab>("settings");
  const [saving, setSaving] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const status = runApi.status;

  const running = isBusyState(status?.state);
  const shownTab: Tab = running && tab === "settings" ? "progress" : tab;
  const locked = settingsLocked(status?.state, status?.locked ?? false);

  const save = useCallback(async () => {
    if (saving || !isAdmin) return;
    setSaving(true);
    const err = await settings.save();
    setSaving(false);
    if (err) say(err, "error");
    else say(GEN_TEXTS.saved, "success");
  }, [saving, isAdmin, settings, say]);

  const { pickStep: flowPickStep } = flow;
  const pickStep = useCallback(
    (mode: "from_step" | "single_step", id: string) => {
      flowPickStep(mode, id);
      setTab("settings");
    },
    [flowPickStep],
  );
  const { viewLog } = runApi;
  const openHistoryLog = useCallback(
    (runId: string) => {
      viewLog(runId);
      setTab("log");
    },
    [viewLog],
  );

  // стрелки переключают вкладки; фокус переходит на выбранную
  const onTabKey = (ev: React.KeyboardEvent, index: number) => {
    const next = ev.key === "ArrowRight" ? index + 1 : ev.key === "ArrowLeft" ? index - 1 : ev.key === "Home" ? 0 : ev.key === "End" ? TABS.length - 1 : null;
    if (next === null) return;
    ev.preventDefault();
    const target = TABS[(next + TABS.length) % TABS.length].id;
    setTab(target);
    rootRef.current?.querySelector<HTMLElement>(`#sb-gen-tab-${target}`)?.focus();
  };

  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (flow.dialog || document.querySelector("[data-sb-modal]")) return;
    const ctrl = ev.ctrlKey || ev.metaKey;
    if (ctrl && ev.key === "Enter") {
      ev.preventDefault();
      flow.requestStart();
    } else if (ctrl && ev.key === ".") {
      ev.preventDefault();
      if (running) flow.askStop();
    } else if (ctrl && !ev.altKey && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      void save();
    } else if (ev.altKey && !ctrl && (ev.code === "KeyP" || ev.key.toLowerCase() === "p")) {
      ev.preventDefault();
      if (running) void flow.pauseToggle();
    } else if (ev.altKey && !ctrl && (ev.code === "KeyL" || ev.key.toLowerCase() === "l")) {
      ev.preventDefault();
      setTab("log");
      rootRef.current?.querySelector<HTMLElement>(".sb-gen-log")?.focus();
    }
  };

  if (runApi.error?.kind === "forbidden") return <NoAccess />;
  if (settings.state === "error" && settings.error) {
    return (
      <div className="sb-screen">
        <h2 className="sb-title">{GEN_TEXTS.title}</h2>
        <Banner
          tone="error"
          actions={
            <button type="button" className="button secondary" onClick={() => void settings.reload()}>
              {GEN_TEXTS.retry}
            </button>
          }
        >
          {gfmt("load_error", { reason: settings.error.text })}
        </Banner>
      </div>
    );
  }
  if (settings.state === "loading" || !settings.draft) {
    return (
      <div className="sb-screen">
        <h2 className="sb-title">{GEN_TEXTS.title}</h2>
        <Skeleton label={GEN_TEXTS.loading} lines={5} height={24} />
      </div>
    );
  }

  const plan = flow.bundle?.plan ?? null;
  const provider = activeProvider(settings.catalog, settings.draft);
  const readiness = (flow.bundle?.summary?.readiness ?? {}) as Record<string, unknown>;
  const notes = collectNotes({
    plan,
    issues: settings.issues,
    readinessBlocking: strings(readiness.blocking_reasons),
    readinessWarnings: strings(readiness.warnings),
    titleOf,
  });
  const dirty = isDirty(settings.saved, settings.draft);
  const banners = bannersFor(status, titleOf);
  const needsTask = !!plan?.problems.some((p) => p.code === "invalid_task");
  const regenCost = flow.regen?.confirm_required.cost_rub ?? 0;
  const body =
    flow.selection.mode === "regenerate_shots"
      ? gfmt("confirm_start_body", { mode: modeLabel("regenerate_shots"), cost: flow.regen && !flow.regen.confirm_required.unknown_cost ? formatShotMoney(regenCost) : "—" })
      : confirmBody(confirmKind(flow.bundle?.estimate ?? null, flow.bundle?.estimateError ?? null), flow.selection.mode, flow.bundle?.estimate ?? null);

  return (
    <div className="sb-screen sb-gen" ref={rootRef} onKeyDown={onKeyDown} data-tab={shownTab}>
      <h2 className="sb-title">{GEN_TEXTS.title}</h2>
      <div className="sb-gen-tabs" role="tablist" aria-label={GEN_TEXTS.title}>
        {TABS.map((t, i) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`sb-gen-tab-${t.id}`}
            aria-selected={shownTab === t.id}
            aria-controls={`sb-gen-pane-${t.id}`}
            tabIndex={shownTab === t.id ? 0 : -1}
            className="button ghost"
            onClick={() => setTab(t.id)}
            onKeyDown={(ev) => onTabKey(ev, i)}
          >
            {GEN_TEXTS[t.text]}
          </button>
        ))}
      </div>
      {settings.restored ? (
        <Banner
          tone="info"
          actions={
            <button type="button" className="button secondary" onClick={settings.discardRestored}>
              {GEN_TEXTS.restored_discard}
            </button>
          }
        >
          {GEN_TEXTS.restored_settings}
        </Banner>
      ) : null}
      <RunBanners
        banners={banners}
        inflight={inflightWarning(plan)}
        isAdmin={isAdmin}
        onMark={flow.askMark}
        onResume={() => flow.setMode("resume")}
        onRestart={() => flow.setMode("full")}
        onRefresh={runApi.refresh}
      />
      {flow.paidPending.length > 0 ? (
        <Banner
          tone="warn"
          actions={
            <>
              <button type="button" className="button secondary" onClick={flow.dismissPending}>
                {GEN_TEXTS.inflight_wait}
              </button>
              <button type="button" className="button" disabled={flow.busy} onClick={() => void flow.abandonAndStart()} title={GEN_TEXTS.inflight_abandon_hint}>
                {GEN_TEXTS.inflight_abandon}
              </button>
            </>
          }
        >
          {gfmt("inflight", { keys: flow.paidPending.join(", ") })} {GEN_TEXTS.inflight_abandon_hint}.
        </Banner>
      ) : null}
      <div className="sb-gen-grid">
        <div className="sb-gen-left">
          <div className="sb-gen-pane" data-pane="settings" id="sb-gen-pane-settings" role="tabpanel" aria-labelledby="sb-gen-tab-settings">
            <RunModeForm
              selection={flow.selection}
              steps={settings.steps}
              resumable={status?.resumable ?? false}
              disabled={locked || !isAdmin}
              needsTask={needsTask}
              task={flow.task}
              brief={flow.brief}
              planInputs={plan?.inputs ?? null}
              inputError={flow.inputError}
              regenWhat={flow.regenWhat}
              onMode={flow.setMode}
              onStep={flow.setStepId}
              onTask={flow.setTask}
              onBrief={flow.setBriefField}
              onRegenWhat={flow.setRegenWhat}
              onOpenBlockout={onOpenBlockout}
            />
            <RunSettingsForm api={settings} locked={locked} isAdmin={isAdmin} saving={saving} onSave={() => void save()} />
          </div>
          <div className="sb-gen-pane" data-pane="summary" id="sb-gen-pane-summary" role="tabpanel" aria-labelledby="sb-gen-tab-summary">
            <PreflightSummary
              plan={plan}
              estimate={flow.bundle?.estimate ?? null}
              estimateError={flow.bundle?.estimateError ?? null}
              dirty={dirty}
              keyLine={provider ? { found: provider.credentials_found, name: provider.title } : null}
              notes={notes}
              titleOf={titleOf}
              loading={flow.planLoading}
            />
            <div className="sb-gen-start">
              <button type="button" className="button" aria-disabled={!flow.button.enabled || flow.busy || flow.planLoading} onClick={() => !flow.busy && !flow.planLoading && flow.requestStart()} title="Ctrl/Cmd+Enter">
                {flow.button.label}
              </button>
              {flow.button.reason ? <div className="sb-muted" role="status">{flow.button.reason}</div> : null}
              {flow.message ? (
                <div role="alert" className="sb-tone-danger">
                  {flow.message}
                </div>
              ) : null}
              {flow.bundleError ? <div className="sb-muted">{flow.bundleError}</div> : null}
            </div>
          </div>
        </div>
        <div className="sb-gen-right">
          <div className="sb-gen-pane" data-pane="progress" id="sb-gen-pane-progress" role="tabpanel" aria-labelledby="sb-gen-tab-progress">
            <RunProgress status={status} titleOf={titleOf} isAdmin={isAdmin} busy={flow.busy || flow.pauseBusy} onPause={() => void flow.pauseToggle()} onStop={flow.askStop} />
            <StepsTracker rows={stepRows(mergeStepRows(settings.steps, status?.steps ?? []), titleOf, Date.now())} onPick={isAdmin && !running ? pickStep : null} />
          </div>
          <div className="sb-gen-pane" data-pane="log" id="sb-gen-pane-log" role="tabpanel" aria-labelledby="sb-gen-tab-log">
            <RunLog log={runApi.log} onClear={runApi.clearLog} onCopied={() => say(GEN_TEXTS.log_copied, "success")} viewing={runApi.viewRunId !== null} onBack={() => runApi.viewLog(null)} />
          </div>
          <RunHistory rows={historyRows(settings.history)} onOpenLog={openHistoryLog} />
        </div>
      </div>
      <StartConfirmDialogs dialog={flow.dialog} body={body} busy={flow.busy} onConfirm={(c) => void flow.confirmDialog(c)} onClose={flow.closeDialog} />
    </div>
  );
}
