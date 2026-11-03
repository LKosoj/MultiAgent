"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { BackgroundQueue } from "./backgroundQueue";
import type { StorybookClient } from "./client";
import type { NavIntent } from "./nav";
import { shotsList, type ShotRow } from "./shots";
import { EMPTY_FILTERS, loadPrefs, savePrefs, type ShotsUiFilters, type StorageLike } from "./shotsPrefs";
import {
  EMPTY_LIST,
  PAGE_SIZE,
  applyListPage,
  applyPredicate,
  hasMorePages,
  isRunBusy,
  listParams,
  uiFiltersFromIntent,
  type ListData,
} from "./shotsView";
import type { ListMode } from "./shotsWindow";
import { classifyStorybookError, type ClassifiedError } from "./uiErrors";

export type ListStatus = "idle" | "loading" | "ready" | "error";

export type ShotsListApi = {
  status: ListStatus;
  error: ClassifiedError | null;
  data: ListData;
  // строки после клиентского фильтра (missing_*)
  items: ShotRow[];
  found: number;
  filters: ShotsUiFilters;
  setFilters: (patch: Partial<ShotsUiFilters>) => void;
  resetFilters: () => void;
  mode: ListMode;
  setMode: (mode: ListMode) => void;
  loadingMore: boolean;
  hasMore: boolean;
  loadMore: () => void;
  // догружает страницы, пока не найдёт шот; true, если нашёл
  revealKey: (key: string) => Promise<boolean>;
  // тихо: уже показанные страницы перечитываются без скелетона
  reload: (opts?: { silent?: boolean }) => Promise<void>;
  patchRows: (fn: (rows: ShotRow[]) => ShotRow[]) => void;
  // выбранный шот, сохранённый в браузере
  savedSelected: string | null;
  saveSelected: (key: string | null) => void;
};

type Args = {
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  enabled: boolean;
  runState: string | undefined;
  intent: NavIntent | null;
};

const PROBE_MS = 5000;

const browserStorage = (): StorageLike | null => {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
};

export const useShotsList = ({ client, background, runUser, projectId, enabled, runState, intent }: Args): ShotsListApi => {
  // начальные фильтры: переход из «Обзора» важнее сохранённых
  const [initial] = useState(() => {
    const prefs = loadPrefs(browserStorage(), projectId);
    const fromIntent = intent && intent.filter !== undefined && intent.shotKey == null;
    const reset = intent && intent.shotKey != null;
    return {
      // невидимый фильтр missing_* между сессиями не помним: иначе список окажется неполным без объяснения
      filters: fromIntent ? uiFiltersFromIntent(intent.filter) : reset ? EMPTY_FILTERS : { ...prefs.filters, predicate: null },
      mode: prefs.mode,
      selected: intent?.shotKey ?? prefs.selected,
    };
  });
  const [filters, setFiltersState] = useState<ShotsUiFilters>(initial.filters);
  const [mode, setModeState] = useState<ListMode>(initial.mode);
  const [selected, setSelected] = useState<string | null>(initial.selected);
  const [data, setData] = useState<ListData>(EMPTY_LIST);
  const [status, setStatus] = useState<ListStatus>("idle");
  const [error, setError] = useState<ClassifiedError | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const statusRef = useRef<ListStatus>("idle");
  const dataRef = useRef(data);
  const filtersRef = useRef(filters);
  const epoch = useRef(0);
  const moreBusy = useRef(false);
  const alive = useRef(true);
  const handledIntent = useRef<NavIntent | null>(intent);
  useEffect(() => {
    dataRef.current = data;
    filtersRef.current = filters;
    statusRef.current = status;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // настройки экрана запоминаем
  useEffect(() => {
    savePrefs(browserStorage(), projectId, { filters: { ...filters, predicate: null }, mode, selected });
  }, [projectId, filters, mode, selected]);

  const fetchPage = useCallback(
    (offset: number, f: ShotsUiFilters) =>
      shotsList(client, { project_id: projectId, limit: PAGE_SIZE, offset, ...listParams(f) }),
    [client, projectId],
  );

  // keep: перечитать уже загруженные страницы; иначе первую (с клиентским фильтром - все)
  const load = useCallback(
    async (opts: { keep: boolean }) => {
      if (!enabled) return;
      const mine = ++epoch.current;
      const f = filtersRef.current;
      const loaded = dataRef.current.items.length;
      const wantAll = f.predicate !== null;
      if (!opts.keep) {
        setStatus("loading");
        setError(null);
      }
      try {
        let acc = EMPTY_LIST;
        let offset = 0;
        for (;;) {
          const res = await runUser(() => fetchPage(offset, f));
          if (!alive.current || mine !== epoch.current) return;
          acc = applyListPage(acc, res);
          offset += PAGE_SIZE;
          const target = wantAll ? res.total : opts.keep ? Math.max(loaded, PAGE_SIZE) : PAGE_SIZE;
          if (offset >= Math.min(target, res.total) || res.items.length === 0) break;
        }
        setData(acc);
        setError(null);
        setStatus("ready");
      } catch (err) {
        if (!alive.current || mine !== epoch.current) return;
        // при тихом перечитывании старый список остаётся; но если полная загрузка была прервана им, статус не должен залипнуть
        if (!opts.keep || statusRef.current === "loading") {
          setError(classifyStorybookError(err));
          setStatus("error");
        }
      }
    },
    [enabled, fetchPage, runUser],
  );

  const filterSig = JSON.stringify(filters);
  useEffect(() => {
    if (!enabled) return;
    void load({ keep: false });
    // filterSig - сериализованные фильтры: новая выборка = новая загрузка
  }, [enabled, filterSig, load]);

  const reload = useCallback((opts?: { silent?: boolean }) => load({ keep: opts?.silent === true }), [load]);

  const loadMore = useCallback(() => {
    const cur = dataRef.current;
    if (!enabled || moreBusy.current || !hasMorePages(cur)) return;
    moreBusy.current = true;
    setLoadingMore(true);
    const mine = epoch.current;
    runUser(() => fetchPage(cur.items.length, filtersRef.current))
      .then((res) => {
        if (!alive.current || mine !== epoch.current) return;
        setData((prev) => applyListPage(prev, res));
      })
      .catch(() => undefined)
      .finally(() => {
        moreBusy.current = false;
        if (alive.current) setLoadingMore(false);
      });
  }, [enabled, fetchPage, runUser]);

  const revealKey = useCallback(
    async (key: string): Promise<boolean> => {
      const mine = epoch.current;
      let guard = 0;
      while (alive.current && mine === epoch.current && guard < 100) {
        const cur = dataRef.current;
        if (cur.items.some((r) => r.shot_key === key)) return true;
        if (!hasMorePages(cur)) return false;
        guard += 1;
        try {
          const res = await runUser(() => fetchPage(cur.items.length, filtersRef.current));
          if (!alive.current || mine !== epoch.current) return false;
          const next = applyListPage(dataRef.current, res);
          dataRef.current = next;
          setData(next);
        } catch {
          return false;
        }
      }
      return false;
    },
    [fetchPage, runUser],
  );

  // пока идёт прогон: раз в 5 с спрашиваем ревизию, при смене перечитываем показанное
  const busy = isRunBusy(runState);
  const wasBusy = useRef(busy);
  useEffect(() => {
    if (!enabled) return;
    let inFlight = false;
    let stop = false;
    const probe = async () => {
      if (inFlight || stop || (typeof document !== "undefined" && document.hidden)) return;
      if (dataRef.current.revision === null) return;
      inFlight = true;
      try {
        const res = await background.run(() => shotsList(client, { project_id: projectId, limit: 1 }));
        if (!stop && alive.current && res.revision !== dataRef.current.revision) void load({ keep: true });
      } catch {
        // следующая проба попробует снова
      } finally {
        inFlight = false;
      }
    };
    // прогон только что закончился: итоговое состояние читаем сразу
    if (wasBusy.current && !busy) void load({ keep: true });
    wasBusy.current = busy;
    if (!busy) return;
    const timer = setInterval(() => void probe(), PROBE_MS);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [enabled, busy, background, client, projectId, load]);

  // переход с другого экрана, пока этот открыт: «Обзор» и т.п. присылают новую цель
  useEffect(() => {
    if (!intent || intent === handledIntent.current) return;
    handledIntent.current = intent;
    if (intent.shotKey != null) {
      setFiltersState(EMPTY_FILTERS);
      setSelected(intent.shotKey);
    } else if (intent.filter !== undefined) {
      setFiltersState(uiFiltersFromIntent(intent.filter));
    }
  }, [intent]);

  const setFilters = useCallback((patch: Partial<ShotsUiFilters>) => setFiltersState((prev) => ({ ...prev, ...patch })), []);
  const resetFilters = useCallback(() => setFiltersState(EMPTY_FILTERS), []);
  const setMode = useCallback((m: ListMode) => setModeState(m), []);
  const patchRows = useCallback((fn: (rows: ShotRow[]) => ShotRow[]) => setData((prev) => ({ ...prev, items: fn(prev.items) })), []);

  const items = useMemo(() => applyPredicate(data.items, filters.predicate), [data.items, filters.predicate]);

  return {
    status,
    error,
    data,
    items,
    found: filters.predicate ? items.length : data.total,
    filters,
    setFilters,
    resetFilters,
    mode,
    setMode,
    loadingMore,
    hasMore: hasMorePages(data) && !filters.predicate,
    loadMore,
    revealKey,
    reload,
    patchRows,
    savedSelected: selected,
    saveSelected: setSelected,
  };
};
