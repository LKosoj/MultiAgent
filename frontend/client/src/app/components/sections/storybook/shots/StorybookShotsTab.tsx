"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type { BackgroundQueue } from "../../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../../lib/storybook/client";
import type { NavIntent } from "../../../../lib/storybook/nav";
import { nextStatusOnKey, type ReviewStatus } from "../../../../lib/storybook/review";
import { shotsAdoptBaseline, shotsKeys } from "../../../../lib/storybook/shots";
import type { ShotKeyAction } from "../../../../lib/storybook/shotsKeys";
import { GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import { IMAGE_TEXTS } from "../../../../lib/storybook/imageToolsTexts";
import { SHOT_TEXTS, fmt, shotCardTitle } from "../../../../lib/storybook/shotsTexts";
import {
  bulkTargetKeys,
  defaultWhatForRows,
  hasActiveFilters,
  isGenerating,
  isRunBusy,
  listParams,
  neighborKey,
  pickExpectedDigests,
  reviewToastId,
  shotErrorFrom,
  viewerImages,
} from "../../../../lib/storybook/shotsView";
import type { EditJobs } from "../../../../lib/storybook/imageEditJob";
import { useImageEdit } from "../../../../lib/storybook/useImageEdit";
import { useRegeneration } from "../../../../lib/storybook/useRegeneration";
import { useShotCard } from "../../../../lib/storybook/useShotCard";
import { useShotMedia } from "../../../../lib/storybook/useShotMedia";
import { useShotReview } from "../../../../lib/storybook/useShotReview";
import { useShotSecondary } from "../../../../lib/storybook/useShotSecondary";
import { shotsRefreshPlan } from "../../../../lib/storybook/screenKeys";
import { useRefreshTick } from "../../../../lib/storybook/useRefreshTick";
import { useShotsKeyboard } from "../../../../lib/storybook/useShotsKeyboard";
import { useShotsList } from "../../../../lib/storybook/useShotsList";
import { useVersions } from "../../../../lib/storybook/useVersions";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { ImageEditDialog } from "../imagetools/ImageEditDialog";
import { ConflictDialog } from "./ConflictDialog";
import { RegenerateDialog } from "./RegenerateDialog";
import { ShotCard } from "./ShotCard";
import { ShotViewer } from "./ShotViewer";
import { ShotsFilterBar } from "./ShotsFilterBar";
import { ShotsList } from "./ShotsList";
import { UnsavedDialog } from "./UnsavedDialog";

const EMPTY_EDITED: Readonly<Record<string, number>> = {};

type Props = {
  // ожидание правки картинки ИИ живёт в разделе и переживает смену вкладок
  editJobs: EditJobs;
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  intent: NavIntent | null;
  runState: string | undefined;
  active: boolean;
  refreshTick: number;
  say: (text: string, type?: "error" | "success" | "info") => void;
  onGoGeneration: () => void;
  // перейти в «Генерацию» с отмеченными шотами
  onGenerateShots: (keys: string[]) => void;
  onResetClient: () => void;
};


const LETTER: Record<ReviewStatus, string> = { pending: "", approved: "a", redo: "r", rejected: "x" };

// Экран «Кадры»: слева список шотов, справа открытый шот
export function StorybookShotsTab({ editJobs, client, background, runUser, projectId, intent, runState, active, refreshTick, say, onGoGeneration, onGenerateShots, onResetClient }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const list = useShotsList({ client, background, runUser, projectId, enabled: active, runState, intent });
  const media = useShotMedia({ client, background, projectId, enabled: active });
  const [selectedKey, setSelectedKey] = useState<string | null>(list.savedSelected);
  const [marked, setMarked] = useState<ReadonlySet<string>>(new Set());
  const [viewerRel, setViewerRel] = useState<string | null>(null);
  const [pendingSelect, setPendingSelect] = useState<{ key: string | null } | null>(null);
  const [baselineBusy, setBaselineBusy] = useState(false);

  const listRef = useRef(list);
  useEffect(() => {
    listRef.current = list;
  });

  const reloadList = useCallback(() => void listRef.current.reload({ silent: true }), []);
  const card = useShotCard({ client, runUser, projectId, shotKey: selectedKey, enabled: active, say, onSaved: reloadList });
  const cardRefreshRef = useRef(card);
  cardRefreshRef.current = card;
  useRefreshTick(refreshTick, () => {
    const plan = shotsRefreshPlan(cardRefreshRef.current.dirty);
    if (plan.list) reloadList();
    if (plan.card) void cardRefreshRef.current.reload();
  });
  const secondary = useShotSecondary({ client, background, projectId, shotKey: selectedKey, enabled: active && card.status === "ready" });
  const versions = useVersions({
    client,
    runUser,
    background,
    projectId,
    shotKey: selectedKey,
    enabled: active && card.status === "ready",
    say,
    onRestored: () => {
      media.bust();
      void card.reload();
      reloadList();
    },
  });
  const cardReloadReview = card.reloadReview;
  const review = useShotReview({
    client,
    runUser,
    projectId,
    say,
    getTitles: () => listRef.current.data.reviewTitles,
    patchRows: list.patchRows,
    onChanged: useCallback(() => {
      void cardReloadReview();
      reloadList();
    }, [cardReloadReview, reloadList]),
  });
  const regen = useRegeneration({
    client,
    runUser,
    projectId,
    say,
    onStarted: () => {
      setMarked(new Set());
      media.bust();
      reloadList();
      void card.reload();
    },
  });

  // время изменения картинок, правленых ИИ: по нему ссылки обходят браузерный кэш
  const edited = useSyncExternalStore(editJobs.subscribe, () => editJobs.getSnapshot().edited, () => EMPTY_EDITED);
  const imageEdit = useImageEdit({
    client,
    runUser,
    projectId,
    say,
    jobs: editJobs,
    onDone: () => {
      media.bust();
      reloadList();
      void card.reload();
    },
  });

  const keys = useMemo(() => list.items.map((r) => r.shot_key), [list.items]);
  const rowOf = useMemo(() => new Map(list.items.map((r) => [r.shot_key, r])), [list.items]);
  const busyRun = isRunBusy(runState);
  const generating = !!card.card && isGenerating(card.card);
  const lockedText = busyRun ? SHOT_TEXTS.busy_run : generating ? SHOT_TEXTS.busy_shot : null;
  const editBlocked = lockedText ?? (imageEdit.running ? IMAGE_TEXTS.edit_running : null);

  // правка ИИ только у начала и конца шота; из окна просмотра картинка находится по пути файла
  const partOfRel = (rel: string): "start" | "end" | null => {
    const parts = card.card?.parts;
    if (!parts) return null;
    return parts.start.exists && parts.start.path === rel ? "start" : parts.end.exists && parts.end.path === rel ? "end" : null;
  };
  const beginEdit = (part: "start" | "end") => {
    const path = card.card?.parts[part].path;
    if (!selectedKey || !path) return;
    imageEdit.begin({ kind: "shot", shotKey: selectedKey, part, path, name: path.split("/").pop() ?? path });
  };

  // выбор шота: при несохранённых правках сначала спрашиваем
  const select = useCallback(
    (key: string | null) => {
      if (key === selectedKey) return;
      if (card.dirty && selectedKey) {
        setPendingSelect({ key });
        return;
      }
      setSelectedKey(key);
      list.saveSelected(key);
    },
    [selectedKey, card.dirty, list],
  );

  const selectRef = useRef(select);
  useEffect(() => {
    selectRef.current = select;
  });
  const stableSelect = useCallback((key: string | null) => selectRef.current(key), []);

  // переход на нужный шот (с другого экрана или при открытии): ждём загрузки списка без фильтров и догружаем страницы
  const handledShot = useRef<string | null>(null);
  const intentShot = intent?.shotKey ?? null;
  const listReady = list.status === "ready";
  const filtersClean = !hasActiveFilters(list.filters);
  useEffect(() => {
    if (!intentShot || handledShot.current === intentShot || !listReady || !filtersClean) return;
    handledShot.current = intentShot;
    void listRef.current.revealKey(intentShot).then(() => selectRef.current(intentShot));
  }, [intentShot, listReady, filtersClean]);

  // сохранённый выбор мог исчезнуть из проекта
  useEffect(() => {
    if (list.status === "ready" && list.data.total > 0 && selectedKey && list.data.items.length === list.data.total && !rowOf.has(selectedKey) && !hasActiveFilters(list.filters)) {
      setSelectedKey(null);
    }
  }, [list.status, list.data, list.filters, selectedKey, rowOf]);

  // выбрать первый шот, если ничего не выбрано
  useEffect(() => {
    if (!selectedKey && keys.length > 0) setSelectedKey(keys[0]);
  }, [selectedKey, keys]);

  const toggleMark = useCallback((key: string) => {
    setMarked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const targets = useCallback(() => bulkTargetKeys(keys, marked, selectedKey), [keys, marked, selectedKey]);

  // самые свежие отпечатки записей ревью: из ответов сервера и из загруженной карточки
  const digests = useRef<Record<string, string>>({});
  useEffect(() => {
    if (card.card && card.entry) digests.current[card.card.shot_key] = card.entry.entry_digest;
  }, [card.card, card.entry]);

  const cardRef = useRef(card);
  useEffect(() => {
    cardRef.current = card;
  });

  const applyStatus = useCallback(
    async (status: ReviewStatus, forKeys: string[]) => {
      if (forKeys.length === 0) return;
      const res = await review.setStatus(forKeys, status, pickExpectedDigests(forKeys, selectedKey, digests.current));
      if (!res) return;
      for (const [k, d] of Object.entries(res.entries)) digests.current[k] = d;
      if (selectedKey && res.entries[selectedKey]) cardRef.current.patchEntry(status, list.data.reviewTitles[status] ?? null, res.entries[selectedKey]);
      if (forKeys.length === 1) say(fmt(reviewToastId(status, false), { key: forKeys[0] }), "success");
      else setMarked(new Set());
    },
    [review, selectedKey, list.data.reviewTitles, say],
  );

  // кнопка и клавиша ведут себя одинаково: повторное нажатие возвращает «Ожидает»
  const statusFor = useCallback(
    (status: ReviewStatus, t: string[]): ReviewStatus => {
      if (t.length !== 1) return status;
      const cur = (rowOf.get(t[0])?.review?.status as ReviewStatus | undefined) ?? "pending";
      return nextStatusOnKey(cur, LETTER[status]) ?? status;
    },
    [rowOf],
  );

  const setStatusForCurrent = useCallback(
    (status: ReviewStatus) => {
      const t = targets();
      void applyStatus(statusFor(status, t), t);
    },
    [targets, statusFor, applyStatus],
  );

  const beginRegen = useCallback(
    (forKeys: string[]) => {
      const rows = forKeys.map((k) => rowOf.get(k)).filter((r): r is NonNullable<typeof r> => !!r);
      regen.begin(forKeys, defaultWhatForRows(rows));
    },
    [rowOf, regen],
  );

  // Ctrl+A: все шоты под текущими фильтрами, а не только загруженные страницы
  const markAll = useCallback(async () => {
    const cur = listRef.current;
    if (cur.filters.predicate || !cur.hasMore) {
      setMarked(new Set(cur.items.map((r) => r.shot_key)));
      return;
    }
    say(SHOT_TEXTS.keys_loading_more, "info");
    try {
      const res = await runUser(() => shotsKeys(client, { project_id: projectId, ...listParams(cur.filters) }));
      setMarked(new Set(res.keys));
      if (res.truncated) say(fmt("marked_truncated", { n: res.keys.length, total: res.total }), "info");
    } catch (err) {
      say(shotErrorFrom(err), "error");
    }
  }, [client, projectId, runUser, say]);

  const onKey = useCallback(
    (a: ShotKeyAction) => {
      switch (a.type) {
        case "move":
          select(neighborKey(keys, selectedKey, a.delta));
          break;
        case "status": {
          const t = targets();
          void applyStatus(statusFor(a.status, t), t);
          break;
        }
        case "mark":
          if (selectedKey) toggleMark(selectedKey);
          break;
        case "mark_all":
          void markAll();
          break;
        case "open": {
          const rel = card.card?.parts.start.exists ? card.card.parts.start.path : null;
          if (rel) setViewerRel(rel);
          break;
        }
        case "save":
          void card.save();
          break;
      }
    },
    [select, keys, selectedKey, targets, statusFor, applyStatus, toggleMark, card, markAll],
  );

  useShotsKeyboard({
    enabled: active,
    getContext: () => ({
      modalOpen: document.querySelector("[data-sb-modal]") !== null,
      focusInside: !!rootRef.current && (rootRef.current.contains(document.activeElement) || document.activeElement === document.body),
    }),
    onAction: onKey,
  });

  const adoptBaseline = async () => {
    setBaselineBusy(true);
    try {
      const res = await runUser(() => shotsAdoptBaseline(client, { project_id: projectId }));
      say(fmt("baseline_done", { n: res.adopted }), "success");
      reloadList();
    } catch (err) {
      say(shotErrorFrom(err), "error");
    } finally {
      setBaselineBusy(false);
    }
  };

  const confirmUnsaved = async (choice: "save" | "discard") => {
    const target = pendingSelect ? pendingSelect.key : null;
    if (choice === "save") {
      const ok = await card.save();
      if (!ok) {
        // не вышло (конфликт или ошибка): остаёмся на шоте, второго окна поверх быть не должно
        setPendingSelect(null);
        return;
      }
    } else {
      card.discard();
    }
    setPendingSelect(null);
    setSelectedKey(target);
    list.saveSelected(target);
  };

  // ---- состояния экрана ----
  const errorBanner =
    list.status === "error" ? (
      <Banner
        tone="error"
        actions={
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              if (list.error && (list.error.kind === "network" || list.error.kind === "timeout")) onResetClient();
              void list.reload();
            }}
          >
            {SHOT_TEXTS.retry}
          </button>
        }
      >
        {fmt("load_error", { reason: list.error?.text ?? "" })}
      </Banner>
    ) : null;
  if (list.status === "error" && list.data.items.length === 0) {
    return (
      <div className="sb-shots" ref={rootRef}>
        {errorBanner}
      </div>
    );
  }
  if (list.status !== "ready" && list.data.items.length === 0) {
    return (
      <div className="sb-shots" ref={rootRef}>
        <Skeleton lines={6} label={SHOT_TEXTS.loading} />
      </div>
    );
  }
  const filtered = hasActiveFilters(list.filters);
  if (list.data.total === 0 && !filtered) {
    return (
      <div className="sb-shots sb-empty" ref={rootRef}>
        <p>{SHOT_TEXTS.empty}</p>
        <button type="button" className="button" onClick={onGoGeneration}>
          {SHOT_TEXTS.empty_goto}
        </button>
      </div>
    );
  }

  const markedCount = marked.size;
  const unsavedTitle = card.card ? shotCardTitle(card.card.shot_key, card.card.scene, card.card.video.duration_s) : (selectedKey ?? "");
  return (
    <div className="sb-shots" ref={rootRef}>
      <ShotsFilterBar
        filters={list.filters}
        data={list.data}
        found={list.found}
        mode={list.mode}
        onFilters={list.setFilters}
        onReset={list.resetFilters}
        onMode={list.setMode}
      />
      {list.data.baseline.missing > 0 ? (
        <Banner
          tone="info"
          actions={
            <button type="button" className="button secondary" disabled={baselineBusy} onClick={() => void adoptBaseline()}>
              {SHOT_TEXTS.baseline_button}
            </button>
          }
        >
          {fmt("baseline_banner", { n: list.data.baseline.missing })}
        </Banner>
      ) : null}
      {list.status === "error" && list.data.items.length > 0 ? errorBanner : null}
      <div className="sb-shots-body">
        <div className="sb-shots-left">
          {markedCount > 0 ? (
            <div className="sb-shots-bulk" role="group">
              <span>{fmt("bulk_selected", { n: markedCount })}</span>
              <button type="button" className="button secondary" disabled={review.busy} onClick={() => void applyStatus("approved", targets())}>
                {SHOT_TEXTS.bulk_approve}
              </button>
              <button type="button" className="button secondary" disabled={review.busy} onClick={() => void applyStatus("redo", targets())}>
                {SHOT_TEXTS.bulk_redo}
              </button>
              <button type="button" className="button" disabled={busyRun} onClick={() => beginRegen(targets())}>
                {SHOT_TEXTS.bulk_regen}
              </button>
              <button type="button" className="button secondary" onClick={() => onGenerateShots(targets())}>
                {GEN_TEXTS.open_in_generation}
              </button>
            </div>
          ) : null}
          {list.found === 0 ? (
            <div className="sb-empty">
              <p>{SHOT_TEXTS.empty_filter}</p>
              <button type="button" className="button secondary" onClick={list.resetFilters}>
                {SHOT_TEXTS.reset_filters}
              </button>
            </div>
          ) : (
            <ShotsList
              items={list.items}
              mode={list.mode}
              selectedKey={selectedKey}
              marked={marked}
              media={media}
              hasMore={list.hasMore}
              loadingMore={list.loadingMore}
              onLoadMore={list.loadMore}
              onSelect={stableSelect}
              onMark={toggleMark}
            />
          )}
          <p className="sb-muted sb-shots-help">{SHOT_TEXTS.keys_help}</p>
        </div>
        <div className="sb-shots-right">
          {selectedKey ? (
            <ShotCard
              api={card}
              review={review}
              secondary={secondary}
              versions={versions}
              media={media}
              lockedText={lockedText}
              onSetStatus={setStatusForCurrent}
              onRegenerate={() => beginRegen(selectedKey ? [selectedKey] : [])}
              onView={setViewerRel}
              onEditImage={beginEdit}
              edited={edited}
              onRetry={() => void card.reload()}
            />
          ) : null}
        </div>
      </div>
      {imageEdit.running ? <Banner tone="info">{IMAGE_TEXTS.edit_running}</Banner> : null}
      {regen.open ? <RegenerateDialog api={regen} /> : null}
      {imageEdit.open && imageEdit.target ? (
        <ImageEditDialog api={imageEdit} imageUrl={media.media(imageEdit.target.path, edited[imageEdit.target.path] != null ? String(edited[imageEdit.target.path]) : null)} />
      ) : null}
      {card.conflict ? <ConflictDialog onRefresh={() => void card.resolveConflict("refresh")} onKeep={() => void card.resolveConflict("keep")} /> : null}
      {pendingSelect ? (
        <UnsavedDialog
          what={unsavedTitle}
          busy={card.saving}
          onSave={() => void confirmUnsaved("save")}
          onDiscard={() => void confirmUnsaved("discard")}
          onStay={() => setPendingSelect(null)}
        />
      ) : null}
      {viewerRel && card.card ? (
        <ShotViewer
          images={viewerImages(card.card)}
          startRel={viewerRel}
          media={media}
          edited={edited}
          canEdit={(rel) => partOfRel(rel) !== null}
          editBlocked={editBlocked}
          onEdit={(rel) => {
            // два окна друг на друге не нужны: просмотр закрывается, открывается правка
            const part = partOfRel(rel);
            setViewerRel(null);
            if (part) beginEdit(part);
          }}
          onClose={() => setViewerRel(null)}
        />
      ) : null}
    </div>
  );
}
