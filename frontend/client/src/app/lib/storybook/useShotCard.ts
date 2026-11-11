"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { createLatestGate } from "./latestGate";
import { reviewGet, reviewSetNote, type ReviewEntry, type ReviewStatus } from "./review";
import {
  dirtyChanges,
  isConflict,
  shotGet,
  shotUpdate,
  type ShotCard,
  type ShotChanges,
  type ShotForm,
} from "./shots";
import { clearDraft, loadDraft, saveDraft, type StorageLike } from "./shotsPrefs";
import { SHOT_TEXTS } from "./shotsTexts";
import { errorReason, formFromCard, reapplyDraft, shotErrorFrom } from "./shotsView";

export type CardStatus = "idle" | "loading" | "ready" | "error";
type Say = (text: string, type?: "error" | "success" | "info") => void;

// Правки, которые нужно сохранить при «Оставить мою правку»
export type MyEdits = { changes: ShotChanges; note: string | null };
export type CardConflict = { kind: "shot" | "note"; mine: MyEdits };

type Loaded = { key: string; card: ShotCard; entry: ReviewEntry; baseDigest: string };

export type ShotCardApi = {
  status: CardStatus;
  error: string | null;
  card: ShotCard | null;
  entry: ReviewEntry | null;
  form: ShotForm | null;
  note: string;
  setField: <K extends keyof ShotForm>(name: K, value: ShotForm[K]) => void;
  setNote: (value: string) => void;
  dirtyFields: ShotChanges;
  noteDirty: boolean;
  dirty: boolean;
  draftRestored: boolean;
  saving: boolean;
  // сообщение сервера после правки («Картинка не обновится сама…»)
  effectsNote: string | null;
  save: () => Promise<boolean>;
  discard: () => void;
  reload: () => Promise<void>;
  reloadReview: () => Promise<void>;
  // свежий статус и отпечаток записи из ответа сервера, без нового запроса
  patchEntry: (status: ReviewStatus, statusTitle: string | null, entryDigest: string) => void;
  conflict: CardConflict | null;
  resolveConflict: (choice: "refresh" | "keep") => Promise<void>;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  storageId?: string;
  shotKey: string | null;
  enabled: boolean;
  say: Say;
  // карточка поменялась на сервере (сохранили правку): список обновит строку
  onSaved?: () => void;
};

const LOAD_DELAY_MS = 150;
const DRAFT_DELAY_MS = 500;

const sessionStorageOrNull = (): StorageLike | null => {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
};

export const useShotCard = ({ client, runUser, projectId, storageId = projectId, shotKey, enabled, say, onSaved }: Args): ShotCardApi => {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [status, setStatus] = useState<CardStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<ShotForm | null>(null);
  const [note, setNoteState] = useState("");
  const [draftRestored, setDraftRestored] = useState(false);
  const [saving, setSaving] = useState(false);
  const [effectsNote, setEffectsNote] = useState<string | null>(null);
  const [conflict, setConflict] = useState<CardConflict | null>(null);

  const gate = useRef(createLatestGate()).current;
  // у перечитывания ревью свой счётчик: оно не должно обесценивать загрузку карточки
  const reviewGate = useRef(createLatestGate()).current;
  const alive = useRef(true);
  const sayRef = useRef(say);
  const onSavedRef = useRef(onSaved);
  useEffect(() => {
    sayRef.current = say;
    onSavedRef.current = onSaved;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // карточка принадлежит выбранному шоту; пока грузится другой, старую не показываем
  const current = loaded && loaded.key === shotKey ? loaded : null;
  const card = current?.card ?? null;
  const entry = current?.entry ?? null;

  const dirtyFields: ShotChanges = card && form ? dirtyChanges(card, form) : {};
  const noteDirty = !!entry && note !== entry.note;
  const dirty = Object.keys(dirtyFields).length > 0 || noteDirty;

  // Читает карточку и ревью; override - мои правки, которые надо наложить поверх свежих данных
  const fetchAndApply = useCallback(
    async (key: string, token: number, opts: { override?: MyEdits; restoreDraft?: boolean } = {}) => {
      try {
        const [shot, review] = await runUser(async () => {
          const a = await shotGet(client, { project_id: projectId, shot_key: key });
          const b = await reviewGet(client, { project_id: projectId, key });
          return [a, b] as const;
        });
        if (!alive.current || !gate.isCurrent(token)) return;
        const fresh = shot.shot;
        const freshEntry = review.shots[key] ?? review.shots[Object.keys(review.shots)[0]];
        let nextForm = formFromCard(fresh);
        let nextNote = freshEntry?.note ?? "";
        let base = fresh.digest;
        let restored = false;
        if (opts.override) {
          nextForm = reapplyDraft(fresh, opts.override.changes);
          if (opts.override.note !== null) nextNote = opts.override.note;
        } else if (opts.restoreDraft) {
          const draft = loadDraft(sessionStorageOrNull(), storageId, key);
          if (draft) {
            nextForm = { ...nextForm, ...draft.changes };
            if (draft.note !== null) nextNote = draft.note;
            // карточка могла поменяться, пока черновик лежал: старый отпечаток даст честный конфликт при сохранении
            base = draft.baseDigest ?? fresh.digest;
            restored = true;
          }
        }
        setLoaded({ key, card: fresh, entry: freshEntry, baseDigest: base });
        setForm(nextForm);
        setNoteState(nextNote);
        setDraftRestored(restored);
        setError(null);
        setStatus("ready");
      } catch (err) {
        if (!alive.current || !gate.isCurrent(token)) return;
        setError(shotErrorFrom(err));
        setStatus("error");
      }
    },
    [client, projectId, storageId, runUser, gate],
  );

  // смена шота: небольшая задержка, чтобы листание стрелками не слало запрос на каждый шаг
  useEffect(() => {
    const token = gate.next();
    if (!enabled || !shotKey) {
      setStatus("idle");
      return;
    }
    setStatus("loading");
    setError(null);
    setEffectsNote(null);
    const timer = setTimeout(() => void fetchAndApply(shotKey, token, { restoreDraft: true }), LOAD_DELAY_MS);
    return () => clearTimeout(timer);
  }, [enabled, shotKey, fetchAndApply, gate]);

  // черновик: пишем с задержкой, при уходе - сразу
  const draftRef = useRef<{ key: string; dirty: boolean; changes: ShotChanges; note: string | null; baseDigest: string } | null>(null);
  const flushDraft = useCallback(() => {
    const d = draftRef.current;
    if (!d) return;
    draftRef.current = null;
    const storage = sessionStorageOrNull();
    if (d.dirty) saveDraft(storage, storageId, d.key, { changes: d.changes, note: d.note, baseDigest: d.baseDigest });
    else clearDraft(storage, storageId, d.key);
  }, [storageId]);

  const currentKey = current?.key ?? null;
  const baseDigest = current?.baseDigest ?? null;
  const dirtySig = JSON.stringify(dirtyFields);
  useEffect(() => {
    // шот сменился, новый ещё грузится: пишем то, что было, и ничего не затираем
    if (!currentKey || baseDigest === null || !form) {
      flushDraft();
      return;
    }
    draftRef.current = { key: currentKey, dirty, changes: dirtyFields, note: noteDirty ? note : null, baseDigest };
    const timer = setTimeout(flushDraft, DRAFT_DELAY_MS);
    return () => clearTimeout(timer);
    // dirtySig заменяет dirtyFields (новый объект на каждый рендер)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentKey, baseDigest, dirty, dirtySig, noteDirty, note, flushDraft]);
  useEffect(() => flushDraft, [flushDraft]);

  const setField = useCallback(<K extends keyof ShotForm>(name: K, value: ShotForm[K]) => {
    setForm((prev) => (prev ? { ...prev, [name]: value } : prev));
  }, []);

  const reload = useCallback(async () => {
    if (!shotKey) return;
    await fetchAndApply(shotKey, gate.next());
  }, [shotKey, fetchAndApply, gate]);

  // после смены статуса ревью обновляем только запись ревью: поля формы не трогаем
  const reloadReview = useCallback(async () => {
    const key = shotKey;
    if (!key) return;
    const token = reviewGate.next();
    try {
      const review = await runUser(() => reviewGet(client, { project_id: projectId, key }));
      if (!alive.current || !reviewGate.isCurrent(token)) return;
      const fresh = review.shots[key];
      if (!fresh) return;
      setLoaded((prev) => (prev && prev.key === key ? { ...prev, entry: fresh } : prev));
      setNoteState((prevNote) => (noteDirty ? prevNote : fresh.note));
    } catch {
      // запись ревью обновится при следующей загрузке карточки
    }
  }, [shotKey, client, projectId, runUser, reviewGate, noteDirty]);

  const patchEntry = useCallback((status: ReviewStatus, statusTitle: string | null, entryDigest: string) => {
    setLoaded((prev) =>
      prev ? { ...prev, entry: { ...prev.entry, status, status_title: statusTitle ?? prev.entry.status_title, entry_digest: entryDigest } } : prev,
    );
  }, []);

  const discard = useCallback(() => {
    if (!loaded) return;
    setForm(formFromCard(loaded.card));
    setNoteState(loaded.entry.note);
    setDraftRestored(false);
    draftRef.current = null;
    clearDraft(sessionStorageOrNull(), storageId, loaded.key);
  }, [loaded, storageId]);

  const save = useCallback(async (): Promise<boolean> => {
    if (!current || !form || saving) return false;
    const key = current.key;
    const changes = dirtyChanges(current.card, form);
    const hasShotChanges = Object.keys(changes).length > 0;
    const noteChanged = note !== current.entry.note;
    if (!hasShotChanges && !noteChanged) return true;
    setSaving(true);
    let step = "shot" as "shot" | "note";
    let serverNote: string | null = null;
    try {
      await runUser(async () => {
        if (hasShotChanges) {
          const res = await shotUpdate(client, {
            project_id: projectId,
            shot_key: key,
            changes,
            base_digest: current.baseDigest,
          });
          serverNote = res.effects.note;
        }
        step = "note";
        if (noteChanged) {
          await reviewSetNote(client, {
            project_id: projectId,
            key,
            note,
            expected_digest: current.entry.entry_digest,
            surface: "react",
          });
        }
      });
    } catch (err) {
      if (alive.current) setSaving(false);
      if (isConflict(err) && (step === "shot" || errorReason(err) === "review_changed")) {
        // правки шота, уже сохранённые первым вызовом, повторять не нужно
        const shotSaved = step === "note";
        setConflict({ kind: step, mine: { changes: shotSaved ? {} : changes, note: noteChanged ? note : null } });
      } else {
        sayRef.current(shotErrorFrom(err), "error");
      }
      return false;
    }
    if (!alive.current) return true;
    setSaving(false);
    clearDraft(sessionStorageOrNull(), storageId, key);
    draftRef.current = null;
    sayRef.current(SHOT_TEXTS.saved_toast, "success");
    setEffectsNote(serverNote);
    await fetchAndApply(key, gate.next());
    onSavedRef.current?.();
    return true;
  }, [current, form, note, saving, runUser, client, projectId, storageId, fetchAndApply, gate]);

  const resolveConflict = useCallback(
    async (choice: "refresh" | "keep") => {
      const c = conflict;
      const key = shotKey;
      setConflict(null);
      if (!key) return;
      if (choice === "refresh" || !c) {
        clearDraft(sessionStorageOrNull(), storageId, key);
        draftRef.current = null;
        await fetchAndApply(key, gate.next());
      } else {
        await fetchAndApply(key, gate.next(), { override: c.mine });
      }
    },
    [conflict, shotKey, projectId, storageId, fetchAndApply, gate],
  );

  return {
    status,
    error,
    card,
    entry,
    form: current ? form : null,
    note,
    setField,
    setNote: setNoteState,
    dirtyFields,
    noteDirty,
    dirty,
    draftRestored,
    saving,
    effectsNote,
    save,
    discard,
    reload,
    reloadReview,
    patchEntry,
    conflict,
    resolveConflict,
  };
};
