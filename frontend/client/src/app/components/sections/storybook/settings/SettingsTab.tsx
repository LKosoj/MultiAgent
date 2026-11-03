"use client";

import { resolveScreenKey } from "../../../../lib/storybook/screenKeys";
import React, { useEffect, useRef } from "react";

import { SETTINGS_TEXTS, settingsFmt } from "../../../../lib/storybook/settingsTexts";
import {
  FIELD_TITLE,
  LOG_LEVELS,
  MAX_BACKUPS_MAX,
  MAX_BACKUPS_MIN,
  defaultMark,
  logLevelLabel,
  type PathField as PathFieldId,
} from "../../../../lib/storybook/settingsView";
import type { AppSettingsApi } from "../../../../lib/storybook/useAppSettings";
import { Banner } from "../Banner";
import { ConfirmDialog } from "../ConfirmDialog";
import { PathField } from "./PathField";

type Props = { api: AppSettingsApi };

const PATHS: PathFieldId[] = ["projects_directory", "backup_directory", "logs_directory"];

// Экран «Настройки»: папки, число бэкапов, уровень журнала. Ctrl+S сохраняет, F5 перечитывает.
export function SettingsTab({ api }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const { snapshot, form, phase } = api;
  const readOnly = api.forbidden;
  // во время сохранения поля закрыты: ответ сервера заменит значения формы
  const frozen = readOnly || api.saving;

  const apiRef = useRef(api);
  apiRef.current = api;
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    const onKey = (e: KeyboardEvent) => {
      const r = resolveScreenKey(e, root.querySelector("[data-sb-modal]") !== null);
      if (!r.swallow) return;
      e.preventDefault();
      e.stopPropagation();
      if (r.action === "save") {
        if (apiRef.current.dirty && !apiRef.current.forbidden) void apiRef.current.save();
      } else if (r.action === "reload" && !apiRef.current.dirty) {
        apiRef.current.reload();
      }
    };
    root.addEventListener("keydown", onKey);
    return () => root.removeEventListener("keydown", onKey);
  }, []);

  if (phase === "loading" && !snapshot) {
    return (
      <div className="sb-settings" ref={rootRef}>
        <h2 className="sb-title">{SETTINGS_TEXTS.title}</h2>
        <p className="sb-muted" role="status">
          {SETTINGS_TEXTS.loading}
        </p>
      </div>
    );
  }
  if (phase === "error" && !snapshot) {
    return (
      <div className="sb-settings" ref={rootRef}>
        <h2 className="sb-title">{SETTINGS_TEXTS.title}</h2>
        <Banner
          tone="error"
          actions={
            <button type="button" className="button secondary" onClick={api.reload}>
              {SETTINGS_TEXTS.retry}
            </button>
          }
        >
          {SETTINGS_TEXTS.load_error.replace("{reason}", (api.error ?? "").replace(/\.$/, ""))}
        </Banner>
      </div>
    );
  }
  if (!snapshot) return null;

  const levelDisabled = frozen;
  const maxErr = api.fieldErrors.max_backup_files ?? null;
  const levelErr = api.fieldErrors.log_level ?? null;

  return (
    <div className="sb-settings" ref={rootRef}>
      <h2 className="sb-title">
        {SETTINGS_TEXTS.title}
        {api.dirty ? <span className="sb-muted"> ({SETTINGS_TEXTS.unsaved})</span> : null}
      </h2>
      {readOnly ? <Banner tone="warn">{SETTINGS_TEXTS.no_rights}</Banner> : null}
      {api.conflict ? (
        <Banner
          tone="warn"
          actions={
            <button type="button" className="button secondary" onClick={api.refreshAfterConflict}>
              {SETTINGS_TEXTS.conflict_refresh}
            </button>
          }
        >
          {SETTINGS_TEXTS.conflict}
        </Banner>
      ) : null}
      {snapshot.warnings.map((w) => (
        <Banner key={w} tone="warn">
          {w}
        </Banner>
      ))}
      {api.saveError ? (
        <Banner tone="error">{settingsFmt("save_error", { reason: api.saveError.replace(/\.$/, "") })}</Banner>
      ) : null}
      {api.fieldErrors.paths ? <Banner tone="error">{api.fieldErrors.paths}</Banner> : null}
      <div className="sb-settings-form">
        {PATHS.map((f) => (
          <PathField
            key={f}
            field={f}
            label={SETTINGS_TEXTS[FIELD_TITLE[f]]}
            value={form[f]}
            lock={api.locks[f]}
            readOnly={frozen}
            check={api.checks[f]}
            error={api.fieldErrors[f] ?? null}
            mark={defaultMark(snapshot.settings[f])}
            onChange={(v) => api.setField(f, v)}
            onBlur={() => api.blurField(f)}
          />
        ))}
        <div className="sb-settings-field" data-field="max_backup_files">
          <label htmlFor="sb-settings-max" className="label">
            {SETTINGS_TEXTS.field_max_backups}
            {defaultMark(snapshot.settings.max_backup_files) ? <span className="sb-muted"> ({SETTINGS_TEXTS.source_default})</span> : null}
          </label>
          <input
            id="sb-settings-max"
            type="number"
            inputMode="numeric"
            min={MAX_BACKUPS_MIN}
            max={MAX_BACKUPS_MAX}
            step={1}
            value={form.max_backup_files}
            disabled={frozen}
            aria-invalid={maxErr ? true : undefined}
            onChange={(e) => api.setField("max_backup_files", e.target.value)}
          />
          <div className="sb-settings-note">
            <span className="sb-muted">{SETTINGS_TEXTS.max_backups_hint}</span>
            {maxErr ? (
              <span role="alert" className="sb-tone-danger">
                {maxErr}
              </span>
            ) : null}
          </div>
        </div>
        <div className="sb-settings-field" data-field="log_level">
          <label htmlFor="sb-settings-level" className="label">
            {SETTINGS_TEXTS.field_log_level}
            {defaultMark(snapshot.settings.log_level) ? <span className="sb-muted"> ({SETTINGS_TEXTS.source_default})</span> : null}
          </label>
          <select
            id="sb-settings-level"
            value={form.log_level}
            disabled={levelDisabled}
            aria-invalid={levelErr ? true : undefined}
            onChange={(e) => api.setField("log_level", e.target.value)}
          >
            {LOG_LEVELS.map((l) => (
              <option key={l} value={l}>
                {logLevelLabel(l)}
              </option>
            ))}
          </select>
          {levelErr ? (
            <div className="sb-settings-note">
              <span role="alert" className="sb-tone-danger">
                {levelErr}
              </span>
            </div>
          ) : null}
        </div>
      </div>
      <div className="button-row">
        <button type="button" className="button" onClick={() => void api.save()} disabled={!api.dirty || api.saving || readOnly}>
          {api.saving ? SETTINGS_TEXTS.saving : SETTINGS_TEXTS.save}
        </button>
        <button type="button" className="button ghost" onClick={api.openReset} disabled={api.saving || readOnly}>
          {SETTINGS_TEXTS.reset}
        </button>
      </div>
      {api.dirty ? <p className="sb-muted">{SETTINGS_TEXTS.unsaved_note}</p> : null}
      <p className="sb-muted">{SETTINGS_TEXTS.tk_note}</p>
      {api.resetOpen ? (
        <ConfirmDialog
          title={SETTINGS_TEXTS.reset_title}
          body={SETTINGS_TEXTS.reset_body}
          confirmLabel={SETTINGS_TEXTS.reset_go}
          cancelLabel={SETTINGS_TEXTS.cancel}
          busy={api.saving}
          error={api.resetError}
          onConfirm={() => void api.confirmReset()}
          onCancel={api.closeReset}
        />
      ) : null}
    </div>
  );
}
