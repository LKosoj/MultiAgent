import React from "react";

import { T } from "../../../../lib/storybook/texts";

type Props = { count: number | null; blockRef: React.Ref<HTMLElement> };

// Число предупреждений по отчётам; сами отчёты появятся в других разделах
export const WarningsBlock = ({ count, blockRef }: Props) => (
  <section ref={blockRef} tabIndex={-1} aria-label={T.warnings}>
    <h3 className="sb-h3">{T.warnings}</h3>
    <p className="sb-muted">{count === null ? T.noReports : `${T.warnings}: ${count}`}</p>
  </section>
);
