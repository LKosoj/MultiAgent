import React from "react";

import type { BlockoutAction } from "../../../../lib/storybook/blockout";
import { BLOCKOUT_TEXTS } from "../../../../lib/storybook/blockoutTexts";
import type { ActionButton } from "../../../../lib/storybook/blockoutView";

type Props = {
  buttons: ActionButton[];
  busy: boolean;
  canOpenShot: boolean;
  onAction: (action: BlockoutAction | "leave_as_is") => void;
  onOpenShot: () => void;
};

// Ряд действий переносится на следующую строку, подписи не обрезаются. Недоступные кнопки объясняют причину
export function BlockoutActions({ buttons, busy, canOpenShot, onAction, onOpenShot }: Props) {
  const hints = [...new Set(buttons.map((b) => b.hint).filter((h): h is string => !!h))].slice(0, 3);
  return (
    <div className="sb-blockout-actions" role="group" aria-label={BLOCKOUT_TEXTS.title}>
      {buttons.map((b) => {
        const off = !b.enabled || busy;
        return (
          <button
            key={b.action}
            type="button"
            className="button"
            data-action={b.action}
            aria-disabled={off ? "true" : undefined}
            title={b.hint ?? undefined}
            onClick={() => (off ? undefined : onAction(b.action as BlockoutAction | "leave_as_is"))}
          >
            {b.label}
          </button>
        );
      })}
      <button
        type="button"
        className="button ghost"
        aria-disabled={canOpenShot ? undefined : "true"}
        onClick={() => (canOpenShot ? onOpenShot() : undefined)}
      >
        {BLOCKOUT_TEXTS.open_in_shots}
      </button>
      {hints.length > 0 ? (
        <ul className="sb-muted sb-blockout-hint">
          {hints.map((h) => (
            <li key={h}>{h}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
