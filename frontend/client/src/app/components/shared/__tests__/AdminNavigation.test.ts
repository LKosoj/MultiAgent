import { describe, expect, it } from "vitest";

import {
  authPrincipalIsAdmin,
  parseAuthPrincipal,
  shouldRenderAdminDbSection,
  visibleSectionsForAuth,
} from "../AuthGate";

const sections = [
  { id: "dashboard", label: "Dashboard" },
  { id: "db", label: "DB" },
  { id: "memory", label: "Memory" },
] as const;

describe("admin DB navigation", () => {
  it("hides the DB navigation and content from an ordinary user", () => {
    const principal = parseAuthPrincipal({
      subject: "alice",
      tenant_id: "tenant-a",
      roles: ["user"],
    });
    const isAdmin = authPrincipalIsAdmin(principal);

    expect(visibleSectionsForAuth(sections, isAdmin).map((section) => section.id))
      .not.toContain("db");
    expect(shouldRenderAdminDbSection("db", isAdmin)).toBe(false);
  });

  it("keeps the DB navigation and content for disabled-mode system admin", () => {
    const principal = parseAuthPrincipal({
      subject: "system",
      tenant_id: "system",
      roles: ["admin", "memory_archivist", "user"],
    });
    const isAdmin = authPrincipalIsAdmin(principal);

    expect(visibleSectionsForAuth(sections, isAdmin).map((section) => section.id))
      .toContain("db");
    expect(shouldRenderAdminDbSection("db", isAdmin)).toBe(true);
    expect(shouldRenderAdminDbSection("dashboard", isAdmin)).toBe(false);
  });
});
