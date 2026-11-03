import React from "react";

import { GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import type { RunBanner } from "../../../../lib/storybook/generationModel";
import { Banner } from "../Banner";

type Props = {
  banners: RunBanner[];
  inflight: string | null;
  isAdmin: boolean;
  onMark: () => void;
  onResume: () => void;
  onRestart: () => void;
  onRefresh: () => void;
};

// Баннеры состояния прогона и предупреждение об оплаченных задачах видео
export function RunBanners({ banners, inflight, isAdmin, onMark, onResume, onRestart, onRefresh }: Props) {
  const action = (banner: RunBanner): React.ReactNode => {
    if (!isAdmin) return null;
    if (banner.action === "mark_interrupted") {
      return (
        <button type="button" className="button secondary" onClick={onMark}>
          {GEN_TEXTS.mark_interrupted}
        </button>
      );
    }
    if (banner.action === "resume") {
      return (
        <button type="button" className="button secondary" onClick={onResume}>
          {GEN_TEXTS.mode_resume}
        </button>
      );
    }
    if (banner.action === "restart") {
      return (
        <button type="button" className="button secondary" onClick={onRestart}>
          {GEN_TEXTS.rerun_start}
        </button>
      );
    }
    if (banner.action === "refresh") {
      return (
        <button type="button" className="button secondary" onClick={onRefresh}>
          {GEN_TEXTS.retry}
        </button>
      );
    }
    return null;
  };
  return (
    <div className="sb-gen-banners">
      {banners.map((b) => (
        <Banner key={b.kind} tone={b.tone} actions={action(b)}>
          {b.text}
        </Banner>
      ))}
      {inflight ? <Banner tone="warn">{inflight}</Banner> : null}
    </div>
  );
}
