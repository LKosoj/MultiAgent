"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { SafeReportFrame } from "../shared/SafeReportFrame";
import { WorkflowResultView } from "../shared/WorkflowResultView";
import { extractFinalOutput } from "./useTextToSqlRun";

type Props = {
  open: boolean;
  runId: string | null;
  active: boolean;
  isBusy: boolean;
  runStatusValue: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  workflowResult: any;
  onClose: () => void;
  onGenerateReport: () => void;
};

export function TextToSqlResultModal({
  open,
  runId,
  active,
  isBusy,
  runStatusValue,
  result,
  workflowResult,
  onClose,
  onGenerateReport,
}: Props) {
  const [isMounted, setIsMounted] = useState(false);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  useEffect(() => {
    if (!active && open) {
      onClose();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, open]);

  useEffect(() => {
    if (!active) return;
    if (!isMounted) return;
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [active, isMounted, open]);

  if (!(active && isMounted && open && runId)) return null;

  return createPortal(
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <div className="section-header">
          <div>
            <div className="card-title">Результат запуска</div>
            <div className="card-description">{runId}</div>
          </div>
          <div className="button-row">
            {runStatusValue && runStatusValue !== "running" ? (
              <button
                className="button secondary"
                type="button"
                onClick={onGenerateReport}
                disabled={isBusy}
              >
                Сохранить отчёт
              </button>
            ) : null}
            <button
              className="modal-close"
              type="button"
              aria-label="Закрыть"
              onClick={onClose}
            >
              ×
            </button>
          </div>
        </div>

        {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
        {!(workflowResult as any)?.report ? (
          <div className="card-description">Статический отчёт пока не сформирован.</div>
        ) : null}
        <SafeReportFrame
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          report={(workflowResult as any)?.report}
          title={`report-${runId}`}
          structuredFallback={(
            <div className="run-result">
              <div className="label">Результат</div>
              <WorkflowResultView
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                finalOutput={extractFinalOutput((workflowResult as any)?.result) ?? result}
              />
            </div>
          )}
        />
      </div>
    </div>,
    document.body,
  );
}
