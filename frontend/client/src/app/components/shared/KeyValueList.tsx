"use client";

import React from "react";

type Props = {
  data: unknown;
  emptyLabel?: string;
  maxDepth?: number;
  depth?: number;
};

const isPrimitive = (value: unknown) =>
  value === null || value === undefined || ["string", "number", "boolean"].includes(typeof value);

const describeValue = (value: unknown): string => {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `Массив (${value.length})`;
  return "Объект";
};

export function KeyValueList({ data, emptyLabel = "Нет данных", maxDepth = 2, depth = 0 }: Props) {
  if (data === null || data === undefined) {
    return <div className="card-description">{emptyLabel}</div>;
  }

  if (typeof data !== "object") {
    return (
      <div className="kv-value" style={{ whiteSpace: "pre-wrap" }}>
        {String(data)}
      </div>
    );
  }

  if (Array.isArray(data)) {
    if (data.length === 0) {
      return <div className="card-description">{emptyLabel}</div>;
    }
    return (
      <div className="kv-array">
        {data.map((item, idx) => {
          if (isPrimitive(item)) {
            return (
              <div className="kv-chip" key={`${String(item)}-${idx}`}>
                {String(item)}
              </div>
            );
          }
          if (depth >= maxDepth) {
            return (
              <div className="kv-chip" key={`obj-${idx}`}>
                {describeValue(item)}
              </div>
            );
          }
          return (
            <details className="details" key={`obj-${idx}`}>
              <summary>Элемент {idx + 1}</summary>
              <KeyValueList data={item} emptyLabel={emptyLabel} maxDepth={maxDepth} depth={depth + 1} />
            </details>
          );
        })}
      </div>
    );
  }

  const entries = Object.entries(data as Record<string, unknown>);
  if (!entries.length) {
    return <div className="card-description">{emptyLabel}</div>;
  }

  return (
    <div className="kv-list">
      {entries.map(([key, value]) => (
        <div className="kv-row" key={key}>
          <div className="kv-key">{key}</div>
          <div className="kv-value" style={{ whiteSpace: "pre-wrap" }}>
            {isPrimitive(value) ? (
              String(value)
            ) : depth >= maxDepth ? (
              describeValue(value)
            ) : (
              <details className="details">
                <summary>{describeValue(value)}</summary>
                <KeyValueList data={value} emptyLabel={emptyLabel} maxDepth={maxDepth} depth={depth + 1} />
              </details>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
