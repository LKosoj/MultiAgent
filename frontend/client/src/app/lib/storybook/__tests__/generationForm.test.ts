import { describe, expect, it } from "vitest";

import {
  budgetText,
  changeProvider,
  collectInputs,
  isDirty,
  isDurationOn,
  parseBudget,
  patchBudget,
  styleValue,
  toggleDuration,
  withStyle,
} from "../generationForm";
import type { RunSettings } from "../runSettings";

const settings = (): RunSettings => ({
  version: 1,
  video: { provider: "veo", model: "m1", fallback_model: null, retry_cycles: null, resolution: "720p", allowed_durations: null },
  style: { profile: null, preset: null },
  audio: { music: true, sfx: null, subtitles: null, narration_path: null },
  budget_rub: null,
  advanced: { force_update_prompts: null, skip_prompt_enhancement: null, sample_before_batch: null, sample_shot_key: null, final_allow_missing_audio: null },
});

describe("настройки", () => {
  it("изменение черновика не трогает сохранённые", () => {
    const saved = settings();
    const draft = patchBudget(saved, 500);
    expect(saved.budget_rub).toBeNull();
    expect(isDirty(saved, draft)).toBe(true);
    expect(isDirty(saved, settings())).toBe(false);
  });
  it("смена провайдера сбрасывает модель и разрешение", () => {
    const next = changeProvider(settings(), "kling");
    expect(next.video).toMatchObject({ provider: "kling", model: null, resolution: null, allowed_durations: null });
  });
  it("стиль хранится парой профиль|пресет", () => {
    const s = withStyle(settings(), "anime|soft");
    expect(s.style).toEqual({ profile: "anime", preset: "soft" });
    expect(styleValue(s)).toBe("anime|soft");
    expect(withStyle(s, "").style).toEqual({ profile: null, preset: null });
  });
  it("длительности: null значит любые, возврат всех снова даёт null", () => {
    const all = [5, 8, 10];
    expect(isDurationOn(null, 8)).toBe(true);
    const some = toggleDuration(null, all, 8);
    expect(some).toEqual([5, 10]);
    expect(isDurationOn(some, 8)).toBe(false);
    expect(toggleDuration(some, all, 8)).toBeNull();
  });
});

describe("бюджет", () => {
  it("разбор", () => {
    expect(parseBudget("")).toEqual({ value: null, error: null });
    expect(parseBudget("1 200")).toMatchObject({ value: null });
    expect(parseBudget("12,5")).toEqual({ value: 12.5, error: null });
    expect(parseBudget("-3").error).toBe("Бюджет должен быть числом не меньше нуля.");
    expect(budgetText(12.5)).toBe("12,5");
  });
});

describe("поля брифа", () => {
  it("уходят только затронутые поля", () => {
    expect(collectInputs({}, {})).toEqual({ inputs: {}, error: null });
    expect(collectInputs({ pages_min: "3", language: " ru " }, {})).toEqual({ inputs: { pages_min: 3, language: "ru" }, error: null });
  });
  it("ошибки целых чисел и пар", () => {
    expect(collectInputs({ pages_max: "0" }, {}).error).toBe("Поле «Страниц, не больше» должно быть целым числом больше нуля.");
    expect(collectInputs({ pages_min: "9" }, { pages_max: 4 }).error).toBe("Поле «Страниц, не меньше»: минимум не может быть больше максимума.");
  });
});
