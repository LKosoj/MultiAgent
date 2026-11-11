"use client";

import React, { useEffect, useState } from "react";
import { speechGet } from "../../../../lib/storybook/series";
import type { StorybookClient } from "../../../../lib/storybook/client";

type Decision = { shot_keys: string[]; reason?: string; method?: string };
type ShotInterval = { shot_key: string; start_s: number; end_s: number; speaker_schedule: { line_id: string }[] };
type Line = { id: string; text: string; speaker_id: string; start: number; end: number };
export function linesForShot(intervals: ShotInterval[], lines: Line[], shotKey: string): Line[] {
  const interval = intervals.find((item) => item.shot_key === shotKey);
  const ids = new Set(interval?.speaker_schedule.map((item) => item.line_id) ?? []);
  return lines.filter((line) => ids.has(line.id));
}
export function ShotSpeech({ client, projectId, shotKey }: { client: StorybookClient; projectId: string; shotKey: string }) {
  const [data, setData] = useState<{ decision?: Decision; lines: Line[] } | null>(null);
  useEffect(() => {
    let current = true;
    setData(null);
    Promise.all([client.call<{ content: { decisions?: Decision[]; shot_intervals?: ShotInterval[] } }>("artifact_read", { project_id: projectId, key: "video_plan" }), speechGet<{ speech: { lines?: Line[] } | null }>(client, { project_id: projectId })]).then(([plan, speech]) => {
      const decision = plan.content?.decisions?.find((d) => d.shot_keys.includes(shotKey));
      if (current) setData({ decision, lines: linesForShot(plan.content?.shot_intervals ?? [], speech.speech?.lines ?? [], shotKey) });
    }).catch(() => { if (current) setData(null); });
    return () => { current = false; };
  }, [client, projectId, shotKey]);
  if (!data) return null;
  return <section><h3>Речь выбранного клипа</h3>
    {data.decision?.reason === "visible_speech" && <p>Видна речь персонажа: видео создаётся по образцам с исходной речью.</p>}
    {data.lines.map((line) => <p key={line.id}>{line.speaker_id}: {line.text} ({line.start}–{line.end} с)</p>)}
  </section>;
}
