"use client";

import React from "react";
import { KeyValueList } from "./KeyValueList";

type Props = {
  finalOutput: unknown;
};

const renderValue = (value: unknown) => {
  if (value === null || value === undefined) return <div className="card-description">Нет данных.</div>;
  if (typeof value === "string") {
    return <pre className="whitespace-pre-wrap text-sm">{value}</pre>;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return <div className="card-description">{String(value)}</div>;
  }
  return <KeyValueList data={value} />;
};

export function WorkflowResultView({ finalOutput }: Props) {
  if (!finalOutput) {
    return <div className="card-description">Результаты пока недоступны.</div>;
  }

  if (typeof finalOutput === "object") {
    const typed = finalOutput as Record<string, any>;
    const workflowType = typed.type;

    if (workflowType === "workflow_result") {
      const outputs = typed.outputs ?? {};
      const outputKeys = Object.keys(outputs);
      const lastKey = outputKeys[outputKeys.length - 1];
      const lastOutput = lastKey ? outputs[lastKey]?.output : null;
      const avgQuality = typed.quality_metrics?.average_quality ?? 0;
      const executionPath = Array.isArray(typed.execution_path) ? typed.execution_path : [];
      const successful = executionPath.filter((item: any) => item?.status === "completed").length;

      return (
        <div className="stack">
          <div className="profile-meta">
            <div>
              <span className="label">Шагов выполнено</span>
              <div className="meta-value">{outputKeys.length}</div>
            </div>
            <div>
              <span className="label">Средняя оценка</span>
              <div className="meta-value">{Number(avgQuality).toFixed(2)}</div>
            </div>
            <div>
              <span className="label">Успешных шагов</span>
              <div className="meta-value">{successful}</div>
            </div>
          </div>
          {lastKey ? (
            <div>
              <div className="card-title">Финальный результат ({lastKey})</div>
              {renderValue(lastOutput)}
            </div>
          ) : null}
          {outputKeys.length ? (
            <details className="details">
              <summary>Детализация по шагам</summary>
              <div className="stack">
                {outputKeys.map((key) => {
                  const step = outputs[key];
                  return (
                    <div key={key} className="card">
                      <div className="card-title">{key}</div>
                      {renderValue(step?.output)}
                      <div className="profile-meta">
                        <div>
                          <span className="label">Качество</span>
                          <div className="meta-value">{Number(step?.quality_score ?? 0).toFixed(2)}</div>
                        </div>
                        <div>
                          <span className="label">Длительность</span>
                          <div className="meta-value">{Number(step?.duration ?? 0).toFixed(1)}s</div>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </details>
          ) : null}
        </div>
      );
    }

    if (workflowType === "workflow_outputs") {
      const outputs = typed.outputs ?? {};
      const outputKeys = Object.keys(outputs);
      const finalValue = typed.final ?? (outputKeys.length ? outputs[outputKeys[0]] : null);
      return (
        <div className="stack">
          <div>
            <div className="card-title">Финальный результат</div>
            {renderValue(finalValue)}
          </div>
          {outputKeys.length ? (
            <details className="details">
              <summary>Детализация outputs</summary>
              <div className="stack">
                {outputKeys.map((key) => (
                  <div key={key} className="card">
                    <div className="card-title">{key}</div>
                    {renderValue(outputs[key])}
                  </div>
                ))}
              </div>
            </details>
          ) : null}
        </div>
      );
    }

    if (workflowType === "research_report" || workflowType === "analysis_report" || workflowType === "sql_generation") {
      const summary = typed.summary;
      const findings = Array.isArray(typed.key_findings) ? typed.key_findings : [];
      const recommendations = Array.isArray(typed.recommendations) ? typed.recommendations : [];
      return (
        <div className="stack">
          {summary ? (
            <div>
              <div className="card-title">Резюме</div>
              {renderValue(summary)}
            </div>
          ) : null}
          {findings.length ? (
            <div>
              <div className="card-title">Ключевые находки</div>
              <ul className="list-disc pl-5">
                {findings.map((item, idx) => (
                  <li key={idx}>{String(item)}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {recommendations.length ? (
            <div>
              <div className="card-title">Рекомендации</div>
              <ul className="list-disc pl-5">
                {recommendations.map((item, idx) => (
                  <li key={idx}>{String(item)}</li>
                ))}
              </ul>
            </div>
          ) : null}
          <details className="details">
            <summary>Полные данные</summary>
            <KeyValueList data={typed} />
          </details>
        </div>
      );
    }
  }

  return renderValue(finalOutput);
}
