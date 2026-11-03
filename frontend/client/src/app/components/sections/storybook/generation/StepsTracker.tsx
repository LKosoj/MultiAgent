"use client";

import React, { useEffect, useRef } from "react";

import { GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import { nextFollow, type FollowState, type StepViewRow } from "../../../../lib/storybook/stepsView";

type Props = {
  rows: StepViewRow[];
  // кнопки «С этого шага» и «Только этот шаг»; null - показывать не нужно (нет прав или идёт прогон)
  onPick: ((mode: "from_step" | "single_step", id: string) => void) | null;
};

const tag = (status: string): string => (status === "completed" ? "completed" : status === "failed" ? "failed" : status === "running" || status === "retrying" ? "running" : "pending");

// Шаги прогона: текущий шаг отмечен aria-current и прокручивается в вид, пока пользователь не прокрутил сам
export function StepsTracker({ rows, onPick }: Props) {
  const listRef = useRef<HTMLUListElement>(null);
  const state = useRef<FollowState>({ follow: true, lastCurrent: null });
  const scrolled = useRef(false);
  const currentId = rows.find((r) => r.current)?.id ?? null;

  useEffect(() => {
    state.current = nextFollow(state.current, currentId, scrolled.current);
    scrolled.current = false;
    if (!state.current.follow || !currentId) return;
    const el = listRef.current?.querySelector<HTMLElement>('[aria-current="step"]');
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, [currentId, rows]);

  return (
    <section className="sb-gen-card" aria-label={GEN_TEXTS.section_steps}>
      <h3 className="sb-h3">{GEN_TEXTS.section_steps}</h3>
      {rows.length === 0 ? (
        <p className="sb-muted">{GEN_TEXTS.progress_idle}</p>
      ) : (
        <ul className="sb-gen-steps" ref={listRef} onWheel={() => (scrolled.current = true)} onTouchMove={() => (scrolled.current = true)}>
          {rows.map((r) => (
            <li key={r.id} className="sb-gen-step" data-step-id={r.id} aria-current={r.current ? "step" : undefined}>
              <span>
                {r.index}. {r.title}
              </span>
              <span className="status-tag" data-status={tag(r.status)}>
                {r.word}
                {r.duration ? ` · ${r.duration}` : ""}
              </span>
              {onPick ? (
                <span className="sb-gen-step-actions">
                  <button type="button" className="button ghost" onClick={() => onPick("from_step", r.id)}>
                    {GEN_TEXTS.step_from_here}
                  </button>
                  <button type="button" className="button ghost" onClick={() => onPick("single_step", r.id)}>
                    {GEN_TEXTS.step_only_this}
                  </button>
                </span>
              ) : null}
              {r.error ? <div className="sb-step-error">{r.error}</div> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
