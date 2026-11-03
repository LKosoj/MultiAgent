import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { StorybookGenerationTab } from "../storybook/generation/StorybookGenerationTab";
import { StartConfirmDialogs } from "../storybook/generation/StartConfirmDialogs";
import { RunBanners } from "../storybook/generation/RunBanners";
import { RunLog } from "../storybook/generation/RunLog";
import { RunProgress } from "../storybook/generation/RunProgress";
import { StepsTracker } from "../storybook/generation/StepsTracker";
import { PreflightSummary } from "../storybook/generation/PreflightSummary";
import { EMPTY_LOG } from "../../../lib/storybook/logView";
import type { RunStatusResult } from "../../../lib/storybook/runs";

const h = (c: unknown, p: object) => renderToStaticMarkup(React.createElement(c as React.ComponentType<object>, p));

const running = {
  state: "running",
  progress: { step_id: "a", step_index: 2, step_total: 5, percent: 40, run_id: "r1" },
  video: { done: 1, total: 4, failed: 1 },
  cost: { spent_rub: 12, budget_rub: 100, scope: "video_jobs" },
  eta: { seconds: 600, basis: "history" },
  steps: [],
} as unknown as RunStatusResult;

describe("экран «Генерация»: части", () => {
  it("прогон идёт: полоса с aria-valuenow, строка шага, кнопки Пауза и Стоп", () => {
    const html = h(RunProgress, { status: running, titleOf: () => "Сценарий", isAdmin: true, busy: false, onPause: () => undefined, onStop: () => undefined });
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="40"');
    expect(html).toContain("Шаг 2 из 5: Сценарий");
    expect(html).toContain("Видео: 1 из 4, не получилось: 1");
    expect(html).toContain("Осталось примерно 10 мин");
    expect(html).toContain(">Пауза<");
    expect(html).toContain(">Стоп<");
  });
  it("простой: «Прогон не идёт», кнопок нет", () => {
    const html = h(RunProgress, { status: null, titleOf: (i: string) => i, isAdmin: true, busy: false, onPause: () => undefined, onStop: () => undefined });
    expect(html).toContain("Прогон не идёт");
    expect(html).not.toContain("<button");
  });
  it("шаги: текущий отмечен aria-current, статус словом", () => {
    const html = h(StepsTracker, {
      rows: [
        { id: "a", index: 1, title: "Сценарий", status: "completed", word: "Готово", duration: "1 мин", error: null, current: false },
        { id: "b", index: 2, title: "Раскадровка", status: "running", word: "Выполняется", duration: null, error: null, current: true },
      ],
      onPick: null,
    });
    expect(html).toContain('aria-current="step"');
    expect(html).toContain("Выполняется");
    expect((html.match(/aria-current/g) ?? []).length).toBe(1);
    expect(html).not.toContain("С этого шага");
  });
  it("шаги: «С этого шага» и «Только этот шаг», если выбор разрешён", () => {
    const html = h(StepsTracker, {
      rows: [{ id: "a", index: 1, title: "Сценарий", status: "pending", word: "Ожидает", duration: null, error: null, current: false }],
      onPick: () => undefined,
    });
    expect(html).toContain("С этого шага");
    expect(html).toContain("Только этот шаг");
  });
  it("журнал: role=log, уровень словом, пустой журнал", () => {
    const empty = h(RunLog, { log: EMPTY_LOG, onClear: () => undefined, onCopied: () => undefined, viewing: false, onBack: () => undefined });
    expect(empty).toContain('role="log"');
    expect(empty).toContain('aria-live="off"');
    expect(empty).toContain("Журнал пока пуст");
    const full = h(RunLog, { log: { ...EMPTY_LOG, lines: ["2026-11-03T12:00:03 ERROR Сбой"] }, onClear: () => undefined, onCopied: () => undefined, viewing: true, onBack: () => undefined });
    expect(full).toContain("ОШИБКА");
    expect(full).toContain("12:00:03");
    expect(full).toContain("К журналу текущего прогона");
    expect(empty).not.toContain("К журналу текущего прогона");
  });
  it("баннер «Пометить прерванным» только для администратора", () => {
    const banner = { kind: "unresponsive", tone: "warn", text: "Приложение давно не отвечает", action: "mark_interrupted" };
    const props = { banners: [banner], inflight: null, onMark: () => undefined, onResume: () => undefined, onRestart: () => undefined, onRefresh: () => undefined };
    expect(h(RunBanners, { ...props, isAdmin: true })).toContain("Пометить прерванным");
    expect(h(RunBanners, { ...props, isAdmin: false })).not.toContain("Пометить прерванным");
  });
  it("окна: три кнопки при превышении бюджета, текст про замок при «Пометить»", () => {
    const over = h(StartConfirmDialogs, { dialog: { type: "over_budget" }, body: "тело", busy: false, onConfirm: () => undefined, onClose: () => undefined });
    expect(over).toContain("Оценка выше бюджета");
    expect(over).toContain("Поднять бюджет и запустить");
    expect(over).toContain("Запустить как есть");
    expect(over).toContain("Отмена");
    const mark = h(StartConfirmDialogs, {
      dialog: { type: "mark", error: "Приложение, где шёл прогон, ещё держит проект. Закройте его и повторите." },
      body: "",
      busy: false,
      onConfirm: () => undefined,
      onClose: () => undefined,
    });
    expect(mark).toContain("Пометить прогон прерванным?");
    expect(mark).toContain("ещё держит проект");
    expect(h(StartConfirmDialogs, { dialog: null, body: "", busy: false, onConfirm: () => undefined, onClose: () => undefined })).toBe("");
  });
  it("сводка: оценка, ориентировочно, бюджет, ключ и список замечаний", () => {
    const estimate = {
      complete: true,
      total_known_rub: 1180,
      unknown_components: [],
      budget_verdict: "short",
      short_by_rub: 180,
      components: [{ id: "video", units: 3, amount_rub: 1180, price_source: "x", basis: "rough" }],
    };
    const html = h(PreflightSummary, {
      plan: { steps_to_run: ["a"], skip_steps: [] },
      estimate,
      estimateError: null,
      dirty: true,
      keyLine: { found: false, name: "Veo" },
      notes: [{ level: "error", text: "Нужен ключ" }],
      titleOf: () => "Сценарий",
      loading: false,
    });
    expect(html).toContain("Оценка: 1&nbsp;180&nbsp;₽".replace(/&nbsp;/g, " "));
    expect(html).toContain("ориентировочно");
    expect(html).toContain("по несохранённым настройкам");
    expect(html).toContain("Бюджет меньше оценки на 180");
    expect(html).toContain("Нет ключа для провайдера Veo");
    expect(html).toContain("Ошибка:");
  });
  it("сводка: оценку получить не удалось - явный текст про «Запустить без оценки»", () => {
    const html = h(PreflightSummary, { plan: null, estimate: null, estimateError: "сервер не ответил", dirty: false, keyLine: null, notes: [], titleOf: (i: string) => i, loading: false });
    expect(html).toContain("Оценку стоимости получить не удалось: сервер не ответил.");
  });
  it("экран целиком: до загрузки настроек - заголовок и скелет", () => {
    const html = h(StorybookGenerationTab, {
      client: { call: async () => ({}) },
      runUser: async (task: () => Promise<unknown>) => task(),
      projectId: "p1",
      intent: null,
      active: true,
      isAdmin: true,
      say: () => undefined,
      onOpenBlockout: () => undefined,
    });
    expect(html).toContain("Генерация");
    expect(html).toContain("Загрузка настроек и оценки…");
  });
});
