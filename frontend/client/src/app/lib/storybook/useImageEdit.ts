"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import type { StorybookClient } from "./client";
import { imageEditPreview, imageEditStart, previewArgs } from "./imageEdit";
import {
  buildStartPayload,
  editCanSubmit,
  editButton,
  editableExtension,
  normalizeReferences,
  referenceCandidates,
  toggleReference,
  type EditButton,
  type EditTarget,
  type ImageEditPreview,
} from "./imageEditFlow";
import { editFailureText, type EditJobs, type EditSettled } from "./imageEditJob";
import { mediaInventory } from "./media";
import { errorReason, shotErrorFrom } from "./shotsView";

type Say = (text: string, type?: "error" | "success" | "info") => void;
export type EditPhase = "idle" | "loading" | "ready" | "starting";
export type Candidate = { path: string; name: string; label: string };

export type ImageEditApi = {
  open: boolean;
  phase: EditPhase;
  target: EditTarget | null;
  prompt: string;
  references: string[];
  candidates: Candidate[];
  preview: ImageEditPreview | null;
  button: EditButton | null;
  canSubmit: boolean;
  supported: boolean;
  error: string | null;
  // идёт фоновая правка (после закрытия окна): имя картинки
  running: string | null;
  begin: (target: EditTarget) => void;
  setPrompt: (value: string) => void;
  toggleRef: (path: string) => void;
  start: () => Promise<boolean>;
  close: () => void;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  say: Say;
  // фоновое ожидание правки живёт выше экрана (в разделе) и переживает смену вкладок
  jobs: EditJobs;
  // правка готова: экран перечитывает картинку и списки
  onDone: (result: EditSettled["result"]) => void;
};

export const useImageEdit = ({ client, runUser, projectId, say, jobs, onDone }: Args): ImageEditApi => {
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<EditPhase>("idle");
  const [target, setTarget] = useState<EditTarget | null>(null);
  const [prompt, setPrompt] = useState("");
  const [references, setReferences] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [preview, setPreview] = useState<ImageEditPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = useSyncExternalStore(
    jobs.subscribe,
    () => jobs.getSnapshot().running,
    () => null,
  );
  const seq = useRef(0);
  const inventorySeq = useRef(0);
  const alive = useRef(true);
  const sayRef = useRef(say);
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    sayRef.current = say;
    onDoneRef.current = onDone;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // итог правки приходит и после ухода с экрана, но обновить картинки может только открытый экран
  useEffect(() => jobs.onSettled((e) => onDoneRef.current(e.result)), [jobs]);

  const fetchPreview = useCallback(
    async (t: EditTarget, refs: string[]): Promise<void> => {
      const my = ++seq.current;
      setPhase("loading");
      setError(null);
      try {
        const res = await runUser(() => imageEditPreview(client, previewArgs(projectId, t, refs)));
        if (!alive.current || my !== seq.current) return;
        setPreview(res);
        setPhase("ready");
      } catch (err) {
        if (!alive.current || my !== seq.current) return;
        setPreview(null);
        setError(shotErrorFrom(err));
        setPhase("ready");
      }
    },
    [client, runUser, projectId],
  );

  const begin = useCallback(
    (t: EditTarget) => {
      setTarget(t);
      setPrompt("");
      setReferences([]);
      setPreview(null);
      setCandidates([]);
      setOpen(true);
      const inv = ++inventorySeq.current;
      void fetchPreview(t, []);
      // список образцов не нужен для цены: его сбой не мешает правке
      void runUser(() => mediaInventory(client, { project_id: projectId, kinds: ["image"], limit: 500, probe_limit: 0 }))
        .then((res) => {
          if (alive.current && inv === inventorySeq.current) setCandidates(referenceCandidates(res.items, t.path));
        })
        .catch(() => undefined);
    },
    [client, runUser, projectId, fetchPreview],
  );

  const toggleRef = useCallback(
    (path: string) => {
      if (!target) return;
      const max = preview?.limits.references_max;
      const next = normalizeReferences(toggleReference(references, path, max), target.path, max);
      setReferences(next);
      void fetchPreview(target, next);
    },
    [target, references, preview, fetchPreview],
  );

  const close = useCallback(() => {
    seq.current += 1;
    inventorySeq.current += 1;
    setOpen(false);
    setPhase("idle");
    setPreview(null);
    setError(null);
  }, []);

  const start = useCallback(async (): Promise<boolean> => {
    if (!target || !preview || phase !== "ready") return false;
    if (!editCanSubmit(prompt, references.length, true) || !preview.can_start) return false;
    const payload = buildStartPayload(projectId, target, prompt, references, preview);
    setPhase("starting");
    setError(null);
    try {
      const { job_id: jobId } = await runUser(() => imageEditStart(client, payload));
      // задание принято: ждёт его раздел, даже если экран уже закрыт
      jobs.track({ client, jobId, name: target.name, say: (text, type) => sayRef.current(text, type) });
      if (!alive.current) return true;
      close();
      return true;
    } catch (err) {
      if (!alive.current) return false;
      const reason = errorReason(err);
      const text = editFailureText(err);
      // сервер видит другой план: показываем свежий предпросмотр и причину
      if (reason === "plan_changed" || reason === "cost_changed" || reason === "cost_confirmation_required") {
        await fetchPreview(target, references);
        if (alive.current) setError(text);
      } else {
        setError(text);
        setPhase("ready");
      }
      return false;
    }
  }, [target, preview, phase, prompt, references, projectId, client, runUser, jobs, close, fetchPreview]);

  const supported = target ? editableExtension(target.name) : true;
  const ready = phase === "ready" && !!preview && preview.can_start && supported;
  return {
    open,
    phase,
    target,
    prompt,
    references,
    candidates,
    preview,
    button: preview ? editButton(preview.estimate.cost_rub, preview.estimate.unknown_cost) : null,
    canSubmit: ready && editCanSubmit(prompt, references.length, true),
    supported,
    error,
    running,
    begin,
    setPrompt,
    toggleRef,
    start,
    close,
  };
};
