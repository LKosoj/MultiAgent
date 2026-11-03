"use client";

import React, { useEffect, useId, useRef, useState } from "react";

import { fieldText, valueAt, type FormField } from "../../../../lib/storybook/artifactForm";
import { FILES_TEXTS } from "../../../../lib/storybook/filesTexts";
import type { Finding } from "../../../../lib/storybook/artifacts";

type Props = {
  fields: readonly FormField[];
  content: unknown;
  readOnly: boolean;
  findings: readonly Finding[];
  // указатели блоков, которые надо раскрыть (например, чтобы показать поле с ошибкой)
  forceOpen: ReadonlySet<string>;
  focusPointer: string | null;
  onEdit: (field: FormField, input: string | boolean) => string | null;
};

const NEEDS_DRAFT = new Set(["number", "numlines", "lines"]);

function Leaf({ field, content, readOnly, issues, autoFocus, onEdit }: {
  field: FormField;
  content: unknown;
  readOnly: boolean;
  issues: readonly Finding[];
  autoFocus: boolean;
  onEdit: Props["onEdit"];
}) {
  const id = useId();
  const elRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (autoFocus) elRef.current?.focus();
  }, [autoFocus]);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const value = valueAt(content, field.path);
  const shown = draft ?? fieldText(field, value);
  const describedBy = `${id}-msg`;
  const change = (input: string | boolean) => {
    if (NEEDS_DRAFT.has(field.kind) && typeof input === "string") setDraft(input);
    setError(onEdit(field, input));
  };
  const hasIssue = issues.length > 0 || error !== null;
  const common = {
    id,
    disabled: readOnly,
    "aria-invalid": hasIssue ? true : undefined,
    "aria-describedby": describedBy,
    "data-pointer": field.pointer,
    ref: elRef as React.RefObject<never>,
  } as const;
  let control: React.ReactNode;
  if (field.kind === "bool") {
    control = (
      <input
        {...common}
        type="checkbox"
        checked={value === true}
        onChange={(e) => change(e.target.checked)}
      />
    );
  } else if (field.kind === "raw") {
    control = (
      <>
        <pre className="sb-files-raw" id={id}>
          {fieldText(field, value)}
        </pre>
        <span className="sb-muted">{FILES_TEXTS.form_raw_hint}</span>
      </>
    );
  } else if (field.kind === "text" || field.kind === "lines" || field.kind === "numlines") {
    control = (
      <textarea
        {...common}
        rows={field.kind === "text" ? 4 : Math.min(8, Math.max(2, (Array.isArray(value) ? value.length : 1) + 1))}
        value={shown}
        onChange={(e) => change(e.target.value)}
        onBlur={() => {
          setDraft(null);
          setError(null);
        }}
      />
    );
  } else {
    control = (
      <input
        {...common}
        type="text"
        inputMode={field.kind === "number" ? "decimal" : undefined}
        value={shown}
        placeholder={field.kind === "null" ? FILES_TEXTS.field_unset : undefined}
        onChange={(e) => change(e.target.value)}
        onBlur={() => {
          setDraft(null);
          setError(null);
        }}
      />
    );
  }
  return (
    <div className="sb-files-field" data-kind={field.kind} data-invalid={hasIssue ? "true" : undefined}>
      <label htmlFor={id} className="label" title={field.pointer || "/"}>
        {field.label}
      </label>
      {control}
      <div id={describedBy} className="sb-files-field-msg">
        {field.kind === "lines" || field.kind === "numlines" ? <span className="sb-muted">{FILES_TEXTS.form_lines_hint}</span> : null}
        {error ? (
          <span role="alert" className="sb-tone-danger">
            {error}
          </span>
        ) : null}
        {issues.map((f, i) => (
          <span key={i} className={f.severity === "error" ? "sb-tone-danger" : "sb-muted"}>
            {f.message}
          </span>
        ))}
      </div>
    </div>
  );
}

// Форма: вложенные блоки раскрываются по требованию и рисуют детей только в раскрытом виде
export function ArtifactForm({ fields, content, readOnly, findings, forceOpen, focusPointer, onEdit }: Props) {
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const byPointer = new Map<string, Finding[]>();
  for (const f of findings) {
    const list = byPointer.get(f.pointer) ?? [];
    list.push(f);
    byPointer.set(f.pointer, list);
  }

  const renderRange = (from: number, to: number): React.ReactNode[] => {
    const out: React.ReactNode[] = [];
    let i = from;
    while (i < to) {
      const field = fields[i];
      if (field.kind === "group" || field.kind === "list" || field.kind === "item") {
        const end = i + 1 + field.size;
        const isOpen = opened.has(field.pointer) || forceOpen.has(field.pointer) || (field.depth === 0 && field.kind !== "item" && !opened.has(`~${field.pointer}`));
        const start = i + 1;
        out.push(
          <details
            key={field.pointer || "root"}
            className="sb-files-group"
            data-kind={field.kind}
            open={isOpen}
            onToggle={(e) => {
              const open = (e.currentTarget as HTMLDetailsElement).open;
              if (open === isOpen) return;
              setOpened((prev) => {
                const next = new Set(prev);
                if (open) {
                  next.add(field.pointer);
                  next.delete(`~${field.pointer}`);
                } else {
                  next.delete(field.pointer);
                  next.add(`~${field.pointer}`);
                }
                return next;
              });
            }}
          >
            <summary title={field.pointer || "/"}>{field.label}</summary>
            {isOpen ? (
              field.size === 0 ? <p className="sb-muted">{FILES_TEXTS.form_list_empty}</p> : <div className="sb-files-children">{renderRange(start, end)}</div>
            ) : null}
          </details>,
        );
        i = end;
      } else {
        out.push(
          <Leaf
            key={field.pointer}
            field={field}
            content={content}
            readOnly={readOnly}
            issues={byPointer.get(field.pointer) ?? []}
            autoFocus={focusPointer === field.pointer}
            onEdit={onEdit}
          />,
        );
        i += 1;
      }
    }
    return out;
  };

  return <div className="sb-files-form">{renderRange(0, fields.length)}</div>;
}
