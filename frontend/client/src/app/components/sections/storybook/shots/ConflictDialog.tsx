"use client";

import React from "react";

import { SHOT_TEXTS } from "../../../../lib/storybook/shotsTexts";
import { StudioModal } from "../StudioModal";

type Props = {
  busy?: boolean;
  onRefresh: () => void;
  onKeep: () => void;
};

const noop = () => undefined;

// Шот изменён в другом месте: взять свежее или оставить свою правку как черновик.
// Esc и крестика нет: закрытие окна не должно молча отбросить правки
export function ConflictDialog({ busy = false, onRefresh, onKeep }: Props) {
  return (
    <StudioModal title={SHOT_TEXTS.conflict_title} onClose={noop} closable={false}>
      <p>{SHOT_TEXTS.conflict_body}</p>
      <div className="button-row">
        <button type="button" className="button" data-autofocus="true" disabled={busy} onClick={onRefresh}>
          {SHOT_TEXTS.conflict_refresh}
        </button>
        <button type="button" className="button secondary" disabled={busy} onClick={onKeep}>
          {SHOT_TEXTS.conflict_keep}
        </button>
      </div>
    </StudioModal>
  );
}
