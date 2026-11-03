import React from "react";

import { NO_RIGHTS_TEXT, T } from "../../../lib/storybook/texts";

export function NoAccess() {
  return (
    <div className="sb-empty" role="alert">
      <h2 className="sb-title">{T.noRightsTitle}</h2>
      <p>{NO_RIGHTS_TEXT}</p>
    </div>
  );
}
