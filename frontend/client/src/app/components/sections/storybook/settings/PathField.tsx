"use client";

import React, { useId } from "react";

import type { FieldLock, FormField } from "../../../../lib/storybook/settingsView";
import type { PathCheckView } from "../../../../lib/storybook/settingsView";
import { settingsFmt } from "../../../../lib/storybook/settingsTexts";

type Props = {
  field: FormField;
  label: string;
  value: string;
  lock: FieldLock;
  readOnly: boolean;
  check: PathCheckView | null;
  error: string | null;
  mark: string | null;
  onChange: (value: string) => void;
  onBlur: () => void;
};

// Поле папки: ввод, результат проверки на сервере (текстом, не только цветом), ошибка
export function PathField({ field, label, value, lock, readOnly, check, error, mark, onChange, onBlur }: Props) {
  const id = useId();
  const noteId = `${id}-note`;
  return (
    <div className="sb-settings-field" data-field={field}>
      <label htmlFor={id} className="label">
        {label}
        {mark ? <span className="sb-muted"> ({mark})</span> : null}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        disabled={lock.disabled || readOnly}
        aria-invalid={error ? true : undefined}
        aria-describedby={noteId}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        spellCheck={false}
      />
      <div id={noteId} className="sb-settings-note">
        {lock.note ? <span className="sb-muted">{lock.note}</span> : null}
        {check ? (
          <span className="sb-settings-check" data-tone={check.tone} role="status">
            {check.text}
            {check.resolved ? <span className="sb-muted"> {settingsFmt("path_resolved", { path: check.resolved })}</span> : null}
          </span>
        ) : null}
        {error ? (
          <span role="alert" className="sb-tone-danger">
            {error}
          </span>
        ) : null}
      </div>
    </div>
  );
}

