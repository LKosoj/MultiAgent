import { expect, it } from "vitest";
import { linesForShot } from "../storybook/shots/ShotSpeech";

it("выбирает закадровые и видимые реплики только выбранного кадра", () => {
  const lines = [
    { id: "one", text: "Один", speaker_id: "a", start: 0, end: 1 },
    { id: "two", text: "За кадром", speaker_id: "b", start: 1, end: 2 },
    { id: "three", text: "Рассказчик", speaker_id: "c", start: 1.5, end: 2 },
  ];
  const intervals = [
    { shot_key: "1", start_s: 0, end_s: 1, speaker_schedule: [{ line_id: "one" }] },
    { shot_key: "2", start_s: 1, end_s: 2, speaker_schedule: [{ line_id: "two", kind: "off_screen", start: 0, end: 1 }, { line_id: "three", kind: "narrator", start: 0.5, end: 1 }] },
  ];
  expect(linesForShot(intervals, lines, "2")).toEqual([lines[1], lines[2]]);
  expect(linesForShot(intervals, lines, "3")).toEqual([]);
});
