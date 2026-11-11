import { describe, expect, it } from "vitest";
import { screenplayLines } from "../storybook/SeriesSpeech";

describe("редактор речи", () => {
  it("берёт диалог и дикторские реплики из сценария до синтеза", () => {
    expect(screenplayLines({ screenplay: [{ dialogue: [{ id: "a", line: "Привет", direction: "тихо" }], storyboard: [{ voiceover_id: "b", voiceover: "Рассказ", pause_before: 1 }] }] })).toEqual([
      { id: "a", text: "Привет", delivery: "тихо", pause_before: 0, pause_after: 0 },
      { id: "b", text: "Рассказ", delivery: "", pause_before: 1, pause_after: 0 },
    ]);
  });
});
