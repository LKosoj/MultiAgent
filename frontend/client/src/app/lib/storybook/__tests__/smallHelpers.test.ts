import { describe, expect, it } from "vitest";

import { focusedCardId, gridBaseId } from "../gridFocus";
import { createLatestGate } from "../latestGate";
import { matchShortcut, type ShortcutEvent } from "../shortcuts";
import { workflowProjectId } from "../workflowLink";

describe("latestGate", () => {
  it("устаревший ответ не применяется", () => {
    const g = createLatestGate();
    const a = g.next();
    const b = g.next();
    expect(g.isCurrent(a)).toBe(false);
    expect(g.isCurrent(b)).toBe(true);
  });
});

describe("gridFocus", () => {
  it("в фокусе карточка - берём её, иначе выбранную", () => {
    expect(gridBaseId("a", "b")).toBe("a");
    expect(gridBaseId(null, "b")).toBe("b");
  });
  it("id ищется через closest", () => {
    const el = { closest: () => ({ getAttribute: () => "p1" }) };
    expect(focusedCardId(el as unknown as EventTarget)).toBe("p1");
    expect(focusedCardId({ closest: () => null } as unknown as EventTarget)).toBeNull();
    expect(focusedCardId(null)).toBeNull();
  });
});

describe("workflowProjectId", () => {
  it("свой id берётся из параметров, затем из входов", () => {
    expect(workflowProjectId({ project_id: "in" }, { project_id: " p1 " })).toBe("p1");
    expect(workflowProjectId({ project_id: "in" }, { project_id: "" })).toBe("in");
  });
  it("нет id или он небезопасный - null", () => {
    expect(workflowProjectId({}, {})).toBeNull();
    expect(workflowProjectId({}, { project_id: "../x" })).toBeNull();
  });
});

describe("Numpad с Alt в поле ввода", () => {
  const ctx = { active: true, modalOpen: false, focusInside: true, screen: "projects" as const, hasProject: true };
  const ev = (o: Partial<ShortcutEvent>): ShortcutEvent => ({
    key: "1", code: "Numpad1", ctrlKey: false, metaKey: false, altKey: true, shiftKey: false, inEditable: true, ...o,
  });
  it("в поле не перехватывается", () => {
    expect(matchShortcut(ev({}), ctx)).toBeNull();
  });
  it("вне поля и с обычной цифрой работает", () => {
    expect(matchShortcut(ev({ inEditable: false }), ctx)).not.toBeNull();
    expect(matchShortcut(ev({ code: "Digit1" }), ctx)).not.toBeNull();
  });
});
