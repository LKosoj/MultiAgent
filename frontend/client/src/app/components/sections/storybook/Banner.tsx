import React from "react";

type Props = {
  tone?: "error" | "warn" | "ok" | "info";
  children: React.ReactNode;
  actions?: React.ReactNode;
};

// Плашка с сообщением; ошибки объявляются скринридеру через role=alert
export function Banner({ tone = "info", children, actions }: Props) {
  return (
    <div className="sb-banner" data-tone={tone} role={tone === "error" ? "alert" : "status"}>
      <div>{children}</div>
      {actions ? <div className="sb-banner-actions">{actions}</div> : null}
    </div>
  );
}
