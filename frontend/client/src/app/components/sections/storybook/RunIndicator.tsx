import React from "react";

import { runTone } from "../../../lib/storybook/overviewView";
import type { ProjectStatusBrief } from "../../../lib/storybook/status";

// Текст состояния прогона цветом по тону (interrupted = предупреждение)
export function RunIndicator({ brief }: { brief: ProjectStatusBrief }) {
  const tone = runTone(brief.run.state);
  return (
    <span className={`sb-tone-${tone}`} data-run-state={brief.run.state}>
      {brief.run.text}
    </span>
  );
}
