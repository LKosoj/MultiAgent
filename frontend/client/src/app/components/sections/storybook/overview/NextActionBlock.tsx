import React from "react";

import type { NextActionView } from "../../../../lib/storybook/overviewView";
import { T } from "../../../../lib/storybook/texts";

type Props = { view: NextActionView; onGo: (target: NextActionView["target"]) => void };

export function NextActionBlock({ view, onGo }: Props) {
  return (
    <section className="sb-next" data-tone={view.severity} aria-label={T.nextAction}>
      <div>
        <h3 className="sb-h3">{view.title}</h3>
        <div className="sb-muted">{view.detail}</div>
      </div>
      <button type="button" className="button" onClick={() => onGo(view.target)}>
        {view.buttonLabel}
      </button>
    </section>
  );
}
