"use client";

import React, { useMemo, useState, useEffect } from "react";

type FieldType = "text" | "textarea" | "number" | "json" | "boolean" | "select";

type ActionField = {
  name: string;
  label: string;
  type: FieldType;
  placeholder?: string;
  options?: string[];
  required?: boolean;
  defaultValue?: string | number | boolean;
};

type Props = {
  title: string;
  description?: string;
  hint?: string;
  action: string;
  fields?: ActionField[];
  onRun: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  busy: boolean;
};

export function ActionCard({ title, description, hint, action, fields, onRun, busy }: Props) {
  const initialState = useMemo(() => {
    const state: Record<string, string | boolean> = {};
    (fields ?? []).forEach((field) => {
      if (field.type === "boolean") {
        state[field.name] = Boolean(field.defaultValue);
      } else if (field.defaultValue !== undefined) {
        state[field.name] = String(field.defaultValue);
      } else {
        state[field.name] = "";
      }
    });
    return state;
  }, [fields]);

  const [values, setValues] = useState<Record<string, string | boolean>>(initialState);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValues(initialState);
  }, [initialState]);

  const updateValue = (name: string, value: string | boolean) => {
    setValues((prev) => ({ ...prev, [name]: value }));
  };

  const buildPayload = () => {
    const payload: Record<string, unknown> = {};
    for (const field of fields ?? []) {
      const raw = values[field.name];
      if (field.type === "boolean") {
        if (field.required || raw === true || raw === false) {
          payload[field.name] = raw;
        }
        continue;
      }
      const text = String(raw ?? "").trim();
      if (!text) {
        if (field.required) {
          throw new Error(`Поле "${field.label}" обязательно`);
        }
        continue;
      }
      if (field.type === "number") {
        const parsed = Number(text);
        if (Number.isNaN(parsed)) {
          throw new Error(`Поле "${field.label}" должно быть числом`);
        }
        payload[field.name] = parsed;
        continue;
      }
      if (field.type === "json") {
        try {
          payload[field.name] = JSON.parse(text);
        } catch {
          throw new Error(`JSON в поле "${field.label}" некорректен`);
        }
        continue;
      }
      payload[field.name] = text;
    }
    return payload;
  };

  const handleRun = async () => {
    setError(null);
    try {
      const payload = buildPayload();
      await onRun(action, payload);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Не удалось выполнить действие");
    }
  };

  return (
    <div className="card">
      <div className="card-title">{title}</div>
      {description ? <div className="card-description">{description}</div> : null}
      {hint ? <div className="card-hint">{hint}</div> : null}
      {(fields ?? []).length ? (
        <div className="form-grid">
          {(fields ?? []).map((field) => (
            <label key={field.name} className="field">
              <span className="label">{field.label}</span>
              {field.type === "textarea" || field.type === "json" ? (
                <textarea
                  value={String(values[field.name] ?? "")}
                  placeholder={field.placeholder}
                  onChange={(event) => updateValue(field.name, event.target.value)}
                />
              ) : field.type === "boolean" ? (
                <input
                  type="checkbox"
                  checked={Boolean(values[field.name])}
                  onChange={(event) => updateValue(field.name, event.target.checked)}
                />
              ) : field.type === "select" ? (
                <select value={String(values[field.name] ?? "")} onChange={(event) => updateValue(field.name, event.target.value)}>
                  {(field.options ?? []).map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  value={String(values[field.name] ?? "")}
                  placeholder={field.placeholder}
                  onChange={(event) => updateValue(field.name, event.target.value)}
                />
              )}
            </label>
          ))}
        </div>
      ) : null}
      {error ? <div className="card-description">Ошибка: {error}</div> : null}
      <div className="button-row">
        <button className="button" onClick={handleRun} disabled={busy}>
          {busy ? "Выполняется..." : "Запустить"}
        </button>
      </div>
    </div>
  );
}

