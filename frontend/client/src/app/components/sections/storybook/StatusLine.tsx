import React from "react";

import { formatTimeOfDay } from "../../../lib/storybook/format";
import { T } from "../../../lib/storybook/texts";

export type StatusMessage = { text: string; at: number };

type Props = {
  message: StatusMessage | null;
  onOpenJournal: () => void;
  // Журнал живёт в «Генерации», а она доступна только с проектом
  journalEnabled: boolean;
};

const HINT_ID = "sb-shortcuts-hint";

// Строка последнего сообщения. Озвучивает сообщения только тост (role="alert"), здесь aria-live нет.
export function StatusLine({ message, onOpenJournal, journalEnabled }: Props) {
  return (
    <div className="sb-status-line">
      <span className="sb-status-text">
        {T.statusLine}: {message ? `${formatTimeOfDay(message.at)} ${message.text}` : T.noMessages}
      </span>
      {journalEnabled ? (
        <button type="button" className="button ghost" onClick={onOpenJournal} title={T.linkGeneration}>
          {T.journal}
        </button>
      ) : null}
      <button type="button" className="button ghost" aria-label={T.shortcutsHelp} aria-describedby={HINT_ID} title={T.shortcutsList.join("\n")}>
        ?
      </button>
      <span id={HINT_ID} className="sb-sr-only">
        {T.shortcutsTitle}: {T.shortcutsList.join("; ")}
      </span>
    </div>
  );
}
