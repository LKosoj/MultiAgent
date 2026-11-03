import { describe, expect, it } from "vitest";

import { StudioError } from "../client";
import type { RegeneratePreviewResult } from "../review";
import type { ShotCard, ShotRow, ShotsListResult } from "../shots";
import { EMPTY_FILTERS } from "../shotsPrefs";
import { SHOT_TEXTS, formatRubExact, formatRubInt } from "../shotsTexts";
import {
  formatShotMoney,
  EMPTY_LIST,
  applyListPage,
  applyPredicate,
  applyReviewStatus,
  blockoutRel,
  bulkTargetKeys,
  clientPredicate,
  defaultWhatForRows,
  filterFromIntent,
  formFromCard,
  hasActiveFilters,
  hasMorePages,
  isGenerating,
  isRunBusy,
  listParams,
  mergePages,
  neighborKey,
  reapplyDraft,
  regenView,
  shotErrorFrom,
  uiFiltersFromIntent,
  formatBytes,
  formatCreatedAt,
  pendingKeysFromError,
  pickExpectedDigests,
  reviewToastId,
  signedBytes,
  stepIndex,
  viewerImages,
  staleView,
} from "../shotsView";

const part = (exists: boolean, state = exists ? "ready" : "missing") => ({ state, path: exists ? "x" : null, exists, freshness: null });
const row = (key: string, over: Partial<ShotRow> = {}): ShotRow => {
  const [scene, shot] = key.split("-").map(Number);
  return {
    shot_key: key,
    scene,
    shot,
    state: "ready",
    stale_parts: [],
    parts: { start: part(true), end: part(true), video: part(true) },
    blockout: { start: false, end: false, video: false },
    review: null,
    ...over,
  } as unknown as ShotRow;
};

describe("фильтры", () => {
  it("из «Обзора» в параметры и предикат", () => {
    expect(filterFromIntent("stale")).toEqual({ stale_only: true });
    expect(filterFromIntent("missing_videos")).toEqual({});
    expect(clientPredicate("missing_videos")).toBe("missing_videos");
    expect(clientPredicate("stale")).toBeNull();
    expect(uiFiltersFromIntent("errors")).toEqual({ ...EMPTY_FILTERS, state: "error" });
    expect(uiFiltersFromIntent("missing_images")).toEqual({ ...EMPTY_FILTERS, predicate: "missing_images" });
    expect(uiFiltersFromIntent(null)).toEqual(EMPTY_FILTERS);
  });
  it("listParams выкидывает пустое и обрезает запрос", () => {
    expect(listParams(EMPTY_FILTERS)).toEqual({});
    expect(listParams({ ...EMPTY_FILTERS, scene: 0, query: "  кот  ", withFindings: true })).toEqual({ scene: 0, query: "кот", with_findings: true });
    expect(hasActiveFilters({ ...EMPTY_FILTERS, query: "   " })).toBe(false);
    expect(hasActiveFilters({ ...EMPTY_FILTERS, predicate: "missing_images" })).toBe(true);
  });
  it("предикат missing_images: начало или применимый конец", () => {
    const rows = [
      row("1-1"),
      row("1-2", { parts: { start: part(false), end: part(true), video: part(true) } } as Partial<ShotRow>),
      row("1-3", { parts: { start: part(true), end: part(false), video: part(true) } } as Partial<ShotRow>),
      row("1-4", { parts: { start: part(true), end: part(false, "not_applicable"), video: part(true) } } as Partial<ShotRow>),
    ];
    expect(applyPredicate(rows, "missing_images").map((r) => r.shot_key)).toEqual(["1-2", "1-3"]);
    expect(applyPredicate(rows, null)).toHaveLength(4);
  });
  it("предикат missing_videos: применимо, нет файла, шот не в работе", () => {
    const noVideo = { start: part(true), end: part(true), video: part(false) };
    const rows = [
      row("1-1", { parts: noVideo } as Partial<ShotRow>),
      row("1-2", { parts: noVideo, state: "generating" } as Partial<ShotRow>),
      row("1-3", { parts: { ...noVideo, video: part(false, "not_applicable") } } as Partial<ShotRow>),
    ];
    expect(applyPredicate(rows, "missing_videos").map((r) => r.shot_key)).toEqual(["1-1"]);
  });
});

describe("страницы", () => {
  it("mergePages заменяет страницу и обрезает по total", () => {
    const a = ["1-1", "1-2", "1-3"].map((k) => row(k));
    const fresh = [row("1-2", { state: "stale" }), row("1-3")];
    const merged = mergePages(a, fresh, 1, 3);
    expect(merged.map((r) => r.state)).toEqual(["ready", "stale", "ready"]);
    expect(mergePages(a, [row("2-1")], 3, 4).map((r) => r.shot_key)).toEqual(["1-1", "1-2", "1-3", "2-1"]);
    expect(mergePages(a, [row("2-1")], 9, 4)).toHaveLength(4); // смещение дальше конца: встаёт сразу за концом
    expect(mergePages(a, [], 0, 2)).toHaveLength(2);
  });
  it("applyListPage берёт заголовки и ревизию из ответа", () => {
    const res = {
      items: [row("1-1")],
      total: 2,
      offset: 0,
      facets: null,
      baseline: { missing: 3, outdated: 1 },
      state_titles: { ready: "Готов" },
      review_titles: { approved: "Одобрен" },
      reason: null,
      revision: "r1",
    } as unknown as ShotsListResult;
    const data = applyListPage(EMPTY_LIST, res);
    expect(data.revision).toBe("r1");
    expect(data.baseline.missing).toBe(3);
    expect(hasMorePages(data)).toBe(true);
  });
});

describe("выбор и ревью", () => {
  it("neighborKey на крайних значениях", () => {
    expect(neighborKey(["a"], "zzz", -1)).toBe("a");
    expect(neighborKey(["a", "b"], "a", 5)).toBe("b");
  });
  it("bulkTargetKeys добавляет отмеченные, которых нет среди загруженных", () => {
    expect(bulkTargetKeys(["1-1", "1-2"], ["1-2", "10-1", "9-1"], "1-1")).toEqual(["1-2", "9-1", "10-1"]);
  });
  it("applyReviewStatus меняет только указанные строки", () => {
    const rows = [row("1-1"), row("1-2", { review: { status: "approved", status_title: "Одобрен" } })];
    const out = applyReviewStatus(rows, ["1-1"], "redo", { redo: "Переделать" });
    expect(out[0].review).toEqual({ status: "redo", status_title: "Переделать" });
    expect(out[1]).toBe(rows[1]);
  });
  it("isRunBusy", () => {
    expect(["running", "pause_requested", "paused", "stopping"].every(isRunBusy)).toBe(true);
    expect(["idle", "completed", "failed", undefined, null].some((s) => isRunBusy(s))).toBe(false);
  });
});

describe("карточка", () => {
  const card = {
    ...row("1-1"),
    start: { prompt: "a", negative_prompt: "n" },
    end: null,
    video: { prompt: "v", duration_s: 5 },
  } as unknown as ShotCard;
  it("форма из карточки и «оставить мою правку»", () => {
    expect(formFromCard(card)).toEqual({
      start_prompt: "a", end_prompt: "", video_prompt: "v", start_negative_prompt: "n", end_negative_prompt: "", duration_s: 5,
    });
    expect(reapplyDraft(card, { video_prompt: "мой", duration_s: 9 })).toMatchObject({ start_prompt: "a", video_prompt: "мой", duration_s: 9 });
  });
  it("болванка по соглашению об именах", () => {
    expect(blockoutRel(2, 3, "start")).toBe("93_blockout/scene_02_shot_03/ref_start.png");
    expect(blockoutRel(12, 10, "end")).toBe("93_blockout/scene_12_shot_10/ref_end.png");
    expect(blockoutRel(1, 1, "video")).toBe("93_blockout/scene_01_shot_01/blockout_ref.mp4");
  });
  it("isGenerating смотрит и на части", () => {
    expect(isGenerating(row("1-1"))).toBe(false);
    expect(isGenerating(row("1-1", { parts: { start: part(true, "generating"), end: part(true), video: part(true) } } as Partial<ShotRow>))).toBe(true);
  });
});

describe("перегенерация", () => {
  it("defaultWhatForRows объединяет устаревшее и отсутствующее", () => {
    const onlyVideo = row("1-1", { stale_parts: ["video"] });
    expect(defaultWhatForRows([onlyVideo])).toBe("video");
    const missingEnd = row("1-2", { parts: { start: part(true), end: part(false), video: part(true) } } as Partial<ShotRow>);
    expect(defaultWhatForRows([onlyVideo, missingEnd])).toBe("both");
  });

  const preview = (over: Partial<RegeneratePreviewResult> = {}): RegeneratePreviewResult =>
    ({
      plan_digest: "d",
      can_start: true,
      problems: [{ level: "warning", code: "x", message: "осторожно" }],
      expanded_keys: ["1-1", "1-2"],
      linked_keys: ["1-2"],
      steps: [],
      will_snapshot: null,
      disk_needed_bytes: 0,
      pending_paid_jobs: [{ shot_key: "1-2" }, { shot_key: "1-2" }, { shot_key: "1-1" }],
      estimate: { total_known_rub: 12.5, complete: false, unknown_components: ["video", 5] },
      confirm_required: { cost_rub: 12.5, unknown_cost: true },
      run_plan: {},
      ...over,
    }) as RegeneratePreviewResult;

  it("regenView собирает кнопку, ключи и замечания", () => {
    const v = regenView(preview());
    expect(v.button.note).toBe("regen_cost_partial");
    expect(v.needsAbandon).toBe(true);
    expect(v.pendingKeys).toEqual(["1-1", "1-2"]);
    expect(v.shotCount).toBe(2);
    expect(v.linkedKeys).toEqual(["1-2"]);
    expect(v.problems).toEqual([{ level: "warning", message: "осторожно" }]);
    expect(regenView(preview({ disk_needed_bytes: -5 })).diskNeeded).toBe(0);
  });
});

describe("shotErrorFrom", () => {
  it("StudioError идёт через таблицу причин, остальное через классификатор", () => {
    expect(shotErrorFrom(new StudioError("conflict", "x", { reason: "plan_changed" }))).toBe(SHOT_TEXTS.regen_plan_changed);
    expect(shotErrorFrom(new StudioError("internal", "сломалось"))).toBe("сломалось");
    expect(shotErrorFrom(new TypeError("Failed to fetch"))).toContain("Нет связи");
  });
});

describe("отпечатки ревью", () => {
  it("отпечаток нужен только для одного открытого шота", () => {
    expect(pickExpectedDigests(["1-1"], "1-1", { "1-1": "d1" })).toEqual({ "1-1": "d1" });
    expect(pickExpectedDigests(["1-1", "1-2"], "1-1", { "1-1": "d1" })).toBeUndefined();
    expect(pickExpectedDigests(["1-2"], "1-1", { "1-2": "d2" })).toBeUndefined();
    expect(pickExpectedDigests(["1-1"], "1-1", {})).toBeUndefined();
  });

  it("тост: один шот или несколько", () => {
    expect(reviewToastId("approved", false)).toBe("toast_approved");
    expect(reviewToastId("rejected", true)).toBe("toast_bulk_rejected");
    expect(reviewToastId("pending", true)).toBe("toast_bulk_pending");
  });
});

describe("блок «устарел»", () => {
  const base = { stale: true, stale_parts: ["start"], stale_reasons: { start: ["prompt_changed", "странный_код"] }, unknown_reasons: { start: ["no_baseline"] }, cascade: { stale_now: ["start"], stale_after_regeneration: ["start", "video"] }, upstream_stale_keys: ["1-1"] };

  it("причины с подписями, неизвестный код как есть, без повторов", () => {
    const v = staleView(base as never);
    expect(v?.parts).toEqual([{ part: "start", reasons: [SHOT_TEXTS.stale_reason_prompt_changed, "странный_код", SHOT_TEXTS.stale_reason_no_baseline] }]);
    expect(v?.after).toEqual(["video"]);
    expect(v?.upstream).toEqual(["1-1"]);
  });

  it("свежий шот - блока нет", () => {
    const fresh = { ...base, stale: false, stale_parts: [], stale_reasons: {}, unknown_reasons: {}, cascade: { stale_now: [], stale_after_regeneration: [] }, upstream_stale_keys: [] };
    expect(staleView(fresh as never)).toBeNull();
  });
});

describe("числа и даты", () => {
  it("дата по-человечески", () => {
    expect(formatCreatedAt("2026-11-03T10:15:30")).toBe("3 ноября 2026, 10:15");
    expect(formatCreatedAt(null)).toBe("—");
    expect(formatCreatedAt("вчера")).toBe("вчера");
  });

  it("размеры", () => {
    expect(formatBytes(512)).toBe("512 Б");
    expect(formatBytes(1536)).toBe("1,5 КБ");
    expect(signedBytes(-2 * 1024 * 1024)).toBe("-2,0 МБ");
    expect(signedBytes(0)).toBe("0 Б");
  });
});

describe("pendingKeysFromError", () => {
  it("достаёт ключи из details и сортирует по-числовому", () => {
    expect(pendingKeysFromError(new StudioError("conflict", "x", { reason: "paid_job_pending", keys: ["10-1", "9-2", 5] }))).toEqual(["9-2", "10-1"]);
    expect(pendingKeysFromError(new StudioError("conflict", "x", { reason: "paid_job_pending" }))).toEqual([]);
    expect(pendingKeysFromError(new Error("x"))).toEqual([]);
  });
});

describe("просмотр картинок", () => {
  it("порядок: начало, конец, болванка", () => {
    const card = {
      scene: 2,
      shot: 3,
      parts: { start: { exists: true, path: "a.png" }, end: { exists: false, path: null }, video: { exists: true, path: "v.mp4" } },
      blockout: { start: true, end: false, video: true },
    };
    expect(viewerImages(card as never).map((i) => i.rel)).toEqual(["a.png", "93_blockout/scene_02_shot_03/ref_start.png"]);
  });

  it("стрелки упираются в края", () => {
    expect(stepIndex(0, 3, -1)).toBe(0);
    expect(stepIndex(1, 3, 1)).toBe(2);
    expect(stepIndex(2, 3, 1)).toBe(2);
    expect(stepIndex(0, 0, 1)).toBe(0);
  });
});

describe("formatShotMoney", () => {
  it("целые рубли, но малые суммы точно", () => {
    expect(formatShotMoney(6.36)).toBe(formatRubInt(6.36));
    expect(formatShotMoney(0.3)).toBe(formatRubExact(0.3));
    expect(formatShotMoney(0)).toBe(formatRubInt(0));
  });
});
