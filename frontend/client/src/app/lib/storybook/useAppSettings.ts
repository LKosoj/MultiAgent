"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import {
  appSettingsCheckPath,
  appSettingsGet,
  appSettingsReset,
  appSettingsUpdate,
  type AppSettingsResult,
} from "./settings";
import {
  FORM_FIELDS,
  PATH_FIELDS,
  PATH_KIND,
  buildUpdate,
  changedFields,
  fieldForServerError,
  fieldLock,
  formFromSettings,
  isDirty,
  pathCheckView,
  shouldCheckPath,
  type FieldLock,
  type FormField,
  type PathCheckView,
  type PathField,
  type SettingsForm,
} from "./settingsView";
import { SETTINGS_TEXTS } from "./settingsTexts";
import { NO_RIGHTS_TEXT } from "./texts";
import { classifyStorybookError } from "./uiErrors";

const CHECK_DELAY_MS = 400;

type Say = (text: string, type?: "error" | "success" | "info") => void;
type RawCheck = { state: "ok" | "missing" | "not_dir" | "not_writable"; message: string; resolved?: string };

// Несохранённая форма переживает смену раздела; привязана к digest, чтобы не лечь поверх чужих изменений
let settingsDraft: { digest: string; form: SettingsForm } | null = null;

export type AppSettingsApi = {
  phase: "loading" | "ready" | "error";
  error: string | null;
  snapshot: AppSettingsResult | null;
  form: SettingsForm;
  dirty: boolean;
  fieldErrors: Partial<Record<FormField | "paths", string>>;
  locks: Record<FormField, FieldLock>;
  checks: Record<PathField, PathCheckView | null>;
  saving: boolean;
  // ошибка сохранения (не исчезает сама; уходит при правке или новом сохранении)
  saveError: string | null;
  conflict: boolean;
  forbidden: boolean;
  resetOpen: boolean;
  resetError: string | null;
  setField: (field: FormField, value: string) => void;
  blurField: (field: FormField) => void;
  save: () => Promise<void>;
  reload: () => void;
  refreshAfterConflict: () => void;
  openReset: () => void;
  closeReset: () => void;
  confirmReset: () => Promise<void>;
};

const EMPTY_FORM: SettingsForm = {
  projects_directory: "",
  backup_directory: "",
  logs_directory: "",
  max_backup_files: "",
  log_level: "INFO",
};

type Options = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  active: boolean;
  say: Say;
  // после записи: какие поля поменялись (оболочка перечитывает проекты и сбрасывает токены медиа)
  onChanged: (changed: string[]) => void;
};

export const useAppSettings = (o: Options): AppSettingsApi => {
  const { client, runUser, active, say, onChanged } = o;
  const [phase, setPhase] = useState<AppSettingsApi["phase"]>("loading");
  const [error, setError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<AppSettingsResult | null>(null);
  const [form, setForm] = useState<SettingsForm>(EMPTY_FORM);
  const [fieldErrors, setFieldErrors] = useState<AppSettingsApi["fieldErrors"]>({});
  const [liveChecks, setLiveChecks] = useState<Partial<Record<PathField, RawCheck | "pending">>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);

  const saySaved = useRef({ say, onChanged });
  saySaved.current = { say, onChanged };
  const loadSeq = useRef(0);
  const checkSeq = useRef<Record<string, number>>({});
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const formRef = useRef(form);
  formRef.current = form;

  const base = useMemo(() => (snapshot ? formFromSettings(snapshot) : EMPTY_FORM), [snapshot]);

  const apply = useCallback((res: AppSettingsResult, keepForm: SettingsForm | null) => {
    setSnapshot(res);
    setForm(keepForm ?? formFromSettings(res));
    setFieldErrors({});
    setLiveChecks({});
    setConflict(false);
  }, []);

  const load = useCallback(async () => {
    const n = ++loadSeq.current;
    setPhase("loading");
    try {
      const res = await appSettingsGet(client);
      if (n !== loadSeq.current) return;
      const draft = settingsDraft && settingsDraft.digest === res.digest ? settingsDraft.form : null;
      settingsDraft = null;
      apply(res, draft);
      setPhase("ready");
      setError(null);
    } catch (err) {
      if (n !== loadSeq.current) return;
      const c = classifyStorybookError(err);
      if (c.kind === "forbidden") setForbidden(true);
      setError(c.text);
      setPhase("error");
    }
  }, [client, apply]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  // уход с экрана: несохранённая форма запоминается
  useEffect(() => {
    const pending = timers.current;
    return () => {
      loadSeq.current += 1;
      Object.values(pending).forEach(clearTimeout);
    };
  }, []);
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;
  useEffect(
    () => () => {
      const s = snapshotRef.current;
      if (s && isDirty(formRef.current, formFromSettings(s))) settingsDraft = { digest: s.digest, form: formRef.current };
    },
    [],
  );

  const dirty = snapshot ? isDirty(form, base) : false;
  useEffect(() => {
    if (!dirty) return undefined;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  const runCheck = useCallback(
    async (field: PathField, value: string) => {
      const n = (checkSeq.current[field] = (checkSeq.current[field] ?? 0) + 1);
      setLiveChecks((m) => ({ ...m, [field]: "pending" }));
      try {
        const res = await appSettingsCheckPath(client, { kind: PATH_KIND[field], path: value.trim() });
        if (n !== checkSeq.current[field]) return;
        setLiveChecks((m) => ({ ...m, [field]: res }));
      } catch {
        if (n !== checkSeq.current[field]) return;
        setLiveChecks((m) => {
          const next = { ...m };
          delete next[field];
          return next;
        });
      }
    },
    [client],
  );

  const setField = useCallback(
    (field: FormField, value: string) => {
      setForm((f) => ({ ...f, [field]: value }));
      setSaveError(null);
      setFieldErrors((e) => {
        if (!e[field] && !e.paths) return e;
        const next = { ...e };
        delete next[field];
        delete next.paths;
        return next;
      });
      if ((PATH_FIELDS as readonly string[]).includes(field)) {
        const pf = field as PathField;
        clearTimeout(timers.current[pf]);
        if (!shouldCheckPath(value)) {
          checkSeq.current[pf] = (checkSeq.current[pf] ?? 0) + 1;
          setLiveChecks((m) => {
            const next = { ...m };
            delete next[pf];
            return next;
          });
          return;
        }
        timers.current[pf] = setTimeout(() => void runCheck(pf, value), CHECK_DELAY_MS);
      }
    },
    [runCheck],
  );

  const blurField = useCallback(
    (field: FormField) => {
      if (!(PATH_FIELDS as readonly string[]).includes(field)) return;
      const pf = field as PathField;
      clearTimeout(timers.current[pf]);
      const value = formRef.current[pf];
      if (shouldCheckPath(value)) void runCheck(pf, value);
    },
    [runCheck],
  );

  const handleWrite = useCallback((err: unknown) => {
    const c = classifyStorybookError(err);
    if (c.kind === "conflict") {
      setConflict(true);
    } else if (c.kind === "forbidden") {
      setForbidden(true);
      saySaved.current.say(NO_RIGHTS_TEXT, "error");
    } else if (c.kind === "invalid") {
      const where = fieldForServerError(c.text);
      setFieldErrors((e) => ({ ...e, [where]: c.text }));
    } else {
      setSaveError(c.text);
    }
  }, []);

  const save = useCallback(async () => {
    if (!snapshot || saving) return;
    const built = buildUpdate(form, base, snapshot);
    if (!built.ok) {
      setFieldErrors(built.errors);
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const res = await runUser(() => appSettingsUpdate(client, built.payload));
      apply(res, null);
      saySaved.current.say(SETTINGS_TEXTS.toast_saved, "success");
      saySaved.current.onChanged(res.changed ?? changedFields(form, base));
    } catch (err) {
      handleWrite(err);
    } finally {
      setSaving(false);
    }
  }, [snapshot, saving, form, base, client, runUser, apply, handleWrite]);

  const confirmReset = useCallback(async () => {
    setSaving(true);
    setResetError(null);
    try {
      const res = await runUser(() => appSettingsReset(client, { confirm: true }));
      apply(res, null);
      setResetOpen(false);
      saySaved.current.say(SETTINGS_TEXTS.toast_saved, "success");
      saySaved.current.onChanged(res.changed ?? [...FORM_FIELDS]);
    } catch (err) {
      const c = classifyStorybookError(err);
      if (c.kind === "forbidden") setForbidden(true);
      setResetError(c.text);
    } finally {
      setSaving(false);
    }
  }, [client, runUser, apply]);

  // «Обновить» при конфликте: снимок и digest новые, введённое остаётся и подсвечивается как «не сохранено»
  const refreshAfterConflict = useCallback(() => {
    void (async () => {
      try {
        const res = await appSettingsGet(client);
        apply(res, formRef.current);
      } catch (err) {
        setSaveError(classifyStorybookError(err).text);
      }
    })();
  }, [client, apply]);

  const locks = useMemo(() => {
    const out = {} as Record<FormField, FieldLock>;
    for (const f of FORM_FIELDS) {
      out[f] = snapshot ? fieldLock(snapshot, f) : { disabled: false, note: null };
    }
    return out;
  }, [snapshot]);

  const checks = useMemo(() => {
    const out = {} as Record<PathField, PathCheckView | null>;
    for (const f of PATH_FIELDS) {
      const live = liveChecks[f];
      if (live === "pending") out[f] = pathCheckView(null, true);
      else if (live) out[f] = pathCheckView(live, false);
      else if (snapshot && form[f].trim() === base[f].trim()) out[f] = pathCheckView(snapshot.checks[f], false);
      else out[f] = null;
    }
    return out;
  }, [liveChecks, snapshot, form, base]);

  return {
    phase,
    error,
    snapshot,
    form,
    dirty,
    fieldErrors,
    locks,
    checks,
    saving,
    saveError,
    conflict,
    forbidden,
    resetOpen,
    resetError,
    setField,
    blurField,
    save,
    reload: () => void load(),
    refreshAfterConflict,
    openReset: () => {
      setResetError(null);
      setResetOpen(true);
    },
    closeReset: () => setResetOpen(false),
    confirmReset,
  };
};
