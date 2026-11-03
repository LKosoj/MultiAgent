import React from "react";

import { STORYBOOK_SCREENS, isScreenEnabled, type StorybookScreen } from "../../../lib/storybook/nav";
import { SCREEN_TITLES, SELECT_PROJECT_HINT, T } from "../../../lib/storybook/texts";

type Props = {
  screen: StorybookScreen;
  projectId: string | null;
  onGo: (screen: StorybookScreen) => void;
  // без проекта пункт нажимается, но только показывает подсказку
  onBlocked: (hint: string) => void;
};

export function StorybookNav({ screen, projectId, onGo, onBlocked }: Props) {
  return (
    <nav className="sb-nav" aria-label={T.navLabel}>
      {STORYBOOK_SCREENS.map((id, i) => {
        const enabled = isScreenEnabled(id, projectId);
        return (
          <button
            key={id}
            type="button"
            className="sb-nav-item"
            aria-current={screen === id ? "page" : undefined}
            aria-disabled={enabled ? undefined : true}
            title={enabled ? `Alt+${i + 1}` : SELECT_PROJECT_HINT}
            onClick={() => (enabled ? onGo(id) : onBlocked(SELECT_PROJECT_HINT))}
          >
            {SCREEN_TITLES[id]}
          </button>
        );
      })}
    </nav>
  );
}
