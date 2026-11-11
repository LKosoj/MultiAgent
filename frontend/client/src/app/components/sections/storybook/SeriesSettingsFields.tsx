"use client";

import React from "react";

type Settings = Record<string, unknown>;
const titles: Record<string, string> = {
  pages_min: "Страниц от", pages_max: "Страниц до",
  words_per_page_min: "Слов на страницу от", words_per_page_max: "Слов на страницу до",
};

export function editableSettings(defaults: Settings, settings: Settings): [string, string | number][] {
  return Object.entries(defaults).flatMap(([key, fallback]) => {
    const value = settings[key] ?? fallback;
    return typeof value === "string" || typeof value === "number" ? [[key, value]] : [];
  });
}

export function SeriesSettingsFields({ defaults, settings, onChange }: {
  defaults: Settings; settings: Settings; onChange: (settings: Settings) => void;
}) {
  const fields = editableSettings(defaults, settings);
  if (!fields.length) return null;
  return <fieldset><legend>Настройки серии</legend>{fields.map(([key, value]) => <label key={key}>
    {titles[key] ?? key} <input type={typeof value === "number" ? "number" : "text"} value={value} onChange={(event) => {
      const raw = event.target.value;
      if (typeof value === "number" && (!raw.trim() || !Number.isFinite(Number(raw)))) return;
      onChange({ ...settings, [key]: typeof value === "number" ? Number(raw) : raw });
    }} />
  </label>)}</fieldset>;
}
