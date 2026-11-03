"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";

import { GEN_TEXTS, gfmt } from "../../../../lib/storybook/generationTexts";
import { logText, parseLine, type LogState } from "../../../../lib/storybook/logView";
import { LOG_LINES_CAP } from "../../../../lib/storybook/runs";

type Props = {
  log: LogState;
  onClear: () => void;
  onCopied: () => void;
  // журнал выбранного из истории прогона: кнопка возврата к текущему
  viewing: boolean;
  onBack: () => void;
};

const toneOf = (level: string | null): string => (level === "ERROR" || level === "CRITICAL" ? "danger" : level === "WARNING" ? "warn" : "muted");

// Журнал прогона: уровень записи читается словом, цвет только помогает
export function RunLog({ log, onClear, onCopied, viewing, onBack }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const entries = useMemo(() => log.lines.map(parseLine), [log.lines]);

  useEffect(() => {
    const el = boxRef.current;
    if (follow && el) el.scrollTop = el.scrollHeight;
  }, [entries, follow]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(logText(log.lines));
      onCopied();
    } catch {
      // буфер обмена недоступен: пользователь может сохранить файл
    }
  };
  const save = () => {
    const url = URL.createObjectURL(new Blob([logText(log.lines)], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `run-${log.runId ?? "log"}.log`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="sb-gen-card" aria-label={GEN_TEXTS.section_log}>
      <div className="sb-gen-log-head">
        <h3 className="sb-h3">{GEN_TEXTS.section_log}</h3>
        <label className="toggle">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          {GEN_TEXTS.log_follow}
        </label>
        {viewing ? (
          <button type="button" className="button ghost" onClick={onBack}>
            {GEN_TEXTS.log_back}
          </button>
        ) : null}
        <button type="button" className="button ghost" onClick={onClear} disabled={entries.length === 0}>
          {GEN_TEXTS.log_clear}
        </button>
        <button type="button" className="button ghost" onClick={() => void copy()} disabled={entries.length === 0}>
          {GEN_TEXTS.log_copy}
        </button>
        <button type="button" className="button ghost" onClick={save} disabled={entries.length === 0}>
          {GEN_TEXTS.log_save}
        </button>
      </div>
      {log.error ? <p className="sb-muted">{log.error}</p> : null}
      {log.truncated || entries.length >= LOG_LINES_CAP ? <p className="sb-muted">{gfmt("log_truncated", { n: LOG_LINES_CAP })}</p> : null}
      <div className="sb-gen-log" role="log" aria-live="off" aria-label={GEN_TEXTS.section_log} tabIndex={0} ref={boxRef} onScroll={(e) => {
        const el = e.currentTarget;
        if (el.scrollHeight - el.scrollTop - el.clientHeight > 24 && follow) setFollow(false);
      }}>
        {entries.length === 0 ? (
          <span className="sb-muted">{GEN_TEXTS.log_empty}</span>
        ) : (
          entries.map((e, i) => (
            <div key={i} className="sb-gen-log-line" data-tone={toneOf(e.level)}>
              {e.time ? <span className="sb-gen-log-time">{e.time}</span> : null}
              {e.word ? <span className="sb-gen-log-level">{e.word}</span> : null}
              <span>{e.text}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
