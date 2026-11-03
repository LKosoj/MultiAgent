"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import {
  INITIAL_NAV,
  dropMissingProject,
  loadNav,
  navReducer,
  saveNav,
  type NavAction,
  type NavState,
  type StorageLike,
  type StorybookScreen,
  type TargetLike,
} from "./nav";

const browserStorage = (): StorageLike | null => {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
};

export type StorybookNavApi = {
  nav: NavState;
  openProject: (projectId: string) => void;
  goTo: (screen: StorybookScreen) => void;
  applyTarget: (target: TargetLike) => void;
  // открыть экран «Кадры» на нужном шоте
  openShot: (shotKey: string) => void;
  clearProject: () => void;
  // после первой загрузки списка: выбранного проекта нет - возвращаемся к «Проектам»
  reconcile: (knownIds: readonly string[]) => void;
};

export const useStorybookNav = (): StorybookNavApi => {
  const [nav, dispatch] = useReducer(navReducer, INITIAL_NAV);
  const [hydrated, setHydrated] = useState(false);
  const navRef = useRef(nav);
  useEffect(() => {
    navRef.current = nav;
  }, [nav]);

  // восстановление после монтирования: на сервере localStorage нет
  useEffect(() => {
    const saved = loadNav(browserStorage());
    if (saved.projectId !== null) {
      dispatch({ type: "open_project", projectId: saved.projectId });
      if (saved.screen !== "overview") dispatch({ type: "go", screen: saved.screen });
    } else if (saved.screen !== "projects") {
      dispatch({ type: "go", screen: saved.screen });
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (hydrated) saveNav(browserStorage(), nav);
  }, [hydrated, nav]);

  const send = useCallback((action: NavAction) => dispatch(action), []);

  return {
    nav,
    openProject: useCallback((projectId) => send({ type: "open_project", projectId }), [send]),
    goTo: useCallback((screen) => send({ type: "go", screen }), [send]),
    applyTarget: useCallback((target) => send({ type: "apply_target", target }), [send]),
    openShot: useCallback((shotKey) => send({ type: "open_shot", shotKey }), [send]),
    clearProject: useCallback(() => send({ type: "clear_project" }), [send]),
    reconcile: useCallback(
      (knownIds) => {
        if (dropMissingProject(navRef.current, knownIds) !== navRef.current) send({ type: "clear_project" });
      },
      [send],
    ),
  };
};
