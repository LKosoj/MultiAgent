"use client";

import React from "react";

import type { ReportsForShotResult } from "../../../../lib/storybook/reports";
import { SHOT_TEXTS, fmt } from "../../../../lib/storybook/shotsTexts";
import type { ShotSecondaryApi } from "../../../../lib/storybook/useShotSecondary";
import { Skeleton } from "../Skeleton";

type Props = { api: ShotSecondaryApi };

const total = (r: ReportsForShotResult): number => r.groups.reduce((n, g) => n + g.findings.length, 0);

// Замечания к шоту, сгруппированные по источнику
export function ShotReportsTable({ api }: Props) {
  const { reports } = api;
  if (!reports) {
    return api.status === "error" ? (
      <p role="alert" className="sb-tone-danger">
        {api.error}
      </p>
    ) : (
      <Skeleton lines={2} label={SHOT_TEXTS.loading} />
    );
  }
  const n = total(reports);
  return (
    <section className="sb-shots-reports">
      <h3 className="sb-title">{fmt("reports_title", { n })}</h3>
      {n === 0 ? (
        <p className="sb-muted">{SHOT_TEXTS.reports_none}</p>
      ) : (
        <table className="sb-table">
          <thead>
            <tr>
              <th>{SHOT_TEXTS.reports_level}</th>
              <th>{SHOT_TEXTS.reports_source}</th>
              <th>{SHOT_TEXTS.reports_text}</th>
            </tr>
          </thead>
          <tbody>
            {reports.groups.flatMap((g) =>
              g.findings.map((f) => (
                <tr key={f.id}>
                  <td className={`sb-tone-${f.level === "error" ? "danger" : f.level === "warning" ? "warn" : "info"}`}>{f.level_title}</td>
                  <td>{f.source_title || g.title}</td>
                  <td>{f.message}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}
