import React from "react";

type Props = { lines?: number; height?: number; label: string };

// Заглушка на время загрузки
export function Skeleton({ lines = 3, height = 18, label }: Props) {
  return (
    <div role="status" aria-label={label} style={{ display: "grid", gap: 8 }}>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="sb-skeleton" style={{ height, width: i === lines - 1 ? "60%" : "100%" }} />
      ))}
    </div>
  );
}
