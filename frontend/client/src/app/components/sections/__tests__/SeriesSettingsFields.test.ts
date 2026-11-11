import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { editableSettings, SeriesSettingsFields } from "../storybook/SeriesSettingsFields";
import { updateStyleField } from "../storybook/SharedResources";

describe("настройки серии и стиль", () => {
  it("показывает значения серии с запасным значением проекта и сохраняет их тип", () => {
    const defaults = { pages_min: 9, language: "ru", unused: true };
    const settings = { pages_min: 4, untouched: ["value"] };
    expect(editableSettings(defaults, settings)).toEqual([["pages_min", 4], ["language", "ru"]]);
    const html = renderToStaticMarkup(React.createElement(SeriesSettingsFields, { defaults, settings, onChange: vi.fn() }));
    expect(html).toContain('type="number" value="4"');
    expect(html).toContain('type="text" value="ru"');
  });

  it("изменяет поле стиля, сохраняя остальные поля и группы", () => {
    const data = { style_images: { art_style: "акварель", camera_defaults: { lens: "wide" } }, style_text: { narrative_voice: "мягкий" }, negative_list: "надписи" };
    expect(updateStyleField(data, "style_images", "lighting", "утро")).toEqual({
      ...data, style_images: { ...data.style_images, lighting: "утро" },
    });
    expect(data.style_images).not.toHaveProperty("lighting");
  });
});
