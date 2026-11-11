"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { BackgroundQueue } from "./backgroundQueue";
import type { StorybookClient } from "./client";
import {
  projectsGet,
  projectsList,
  projectsSize,
  type ProjectCard,
  type ProjectsGetResult,
} from "./projects";
import { classifyStorybookError, type ClassifiedError } from "./uiErrors";

export type ListState = "idle" | "loading" | "ready" | "error";

export type ProjectExtra = {
  details: ProjectsGetResult["details"] | null;
};

export type ProjectsApi = {
  state: ListState;
  error: ClassifiedError | null;
  projects: ProjectCard[];
  projectsDir: string | null;
  sizes: ReadonlyMap<string, number>;
  extras: ReadonlyMap<string, ProjectExtra>;
  load: () => Promise<void>;
  loadSize: (id: string) => void;
  loadAllSizes: () => void;
  loadExtra: (id: string) => void;
};

type Args = { client: StorybookClient; background: BackgroundQueue; enabled: boolean };

export const useProjects = ({ client, background, enabled }: Args): ProjectsApi => {
  const [state, setState] = useState<ListState>("idle");
  const [error, setError] = useState<ClassifiedError | null>(null);
  const [projects, setProjects] = useState<ProjectCard[]>([]);
  const [projectsDir, setProjectsDir] = useState<string | null>(null);
  const [sizes, setSizes] = useState<ReadonlyMap<string, number>>(new Map());
  const [extras, setExtras] = useState<ReadonlyMap<string, ProjectExtra>>(new Map());

  const alive = useRef(true);
  const epoch = useRef(0); // смена клиента или выключение отбрасывают поздние ответы
  const requested = useRef(new Set<string>());
  const extraRequested = useRef(new Set<string>());
  const clientRef = useRef(client);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    clientRef.current = client;
    epoch.current += 1;
    requested.current = new Set();
    extraRequested.current = new Set();
  }, [client, enabled]);

  const load = useCallback(async () => {
    if (!enabled) return;
    const mine = epoch.current;
    setState((prev) => (prev === "ready" ? prev : "loading"));
    try {
      const result = await projectsList(client, { sort: "modified" });
      if (!alive.current || mine !== epoch.current) return;
      setProjects(result.projects);
      setProjectsDir(result.projects_dir);
      setError(null);
      setState("ready");
      // обновление списка устаревляет кэш карточек справа
      extraRequested.current = new Set();
    } catch (err) {
      if (!alive.current || mine !== epoch.current) return;
      setError(classifyStorybookError(err));
      setState("error");
    }
  }, [client, enabled]);

  const loadSize = useCallback(
    (id: string) => {
      if (!enabled || requested.current.has(id)) return;
      requested.current.add(id);
      const mine = epoch.current;
      background
        .run(() => projectsSize(client, { project_id: id }))
        .then((r) => {
          if (!alive.current || mine !== epoch.current) return;
          setSizes((prev) => new Map(prev).set(id, r.size_bytes));
        })
        .catch(() => {
          requested.current.delete(id); // можно попробовать позже
        });
    },
    [background, client, enabled],
  );

  const projectIds = useMemo(() => projects.map((p) => p.project_id), [projects]);
  const loadAllSizes = useCallback(() => projectIds.forEach(loadSize), [projectIds, loadSize]);

  const loadExtra = useCallback(
    (id: string) => {
      if (!enabled || extraRequested.current.has(id)) return;
      extraRequested.current.add(id);
      const mine = epoch.current;
      const put = (patch: Partial<ProjectExtra>) => {
        if (!alive.current || mine !== epoch.current) return;
        setExtras((prev) => {
          const old = prev.get(id) ?? { details: null };
          return new Map(prev).set(id, { ...old, ...patch });
        });
      };
      projectsGet(client, { project_id: id })
        .then((r) => put({ details: r.details }))
        .catch(() => extraRequested.current.delete(id));

    },
    [client, enabled],
  );

  return { state, error, projects, projectsDir, sizes, extras, load, loadSize, loadAllSizes, loadExtra };
};
