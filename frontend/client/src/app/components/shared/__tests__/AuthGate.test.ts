import { afterEach, describe, expect, it, vi } from "vitest";

import {
  authPrincipalIsAdmin,
  parseAuthPrincipal,
  probeAuth,
} from "../AuthGate";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("probeAuth", () => {
  it("probes with GET and the exact bearer header", async () => {
    const principal = {
      subject: "alice",
      tenant_id: "tenant-a",
      roles: ["user"],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue(principal),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      probeAuth("http://localhost:8000/v1/auth/me", "  deployment-token  "),
    ).resolves.toEqual({
      authenticated: true,
      rejected: false,
      status: 200,
      principal,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/v1/auth/me",
      {
        method: "GET",
        headers: { Authorization: "Bearer deployment-token" },
        signal: undefined,
      },
    );
  });

  it("allows an empty-token probe", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({
        subject: "system",
        tenant_id: "system",
        roles: ["admin", "memory_archivist", "user"],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await probeAuth("http://localhost:8000/v1/auth/me", "");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/v1/auth/me",
      expect.objectContaining({ headers: {} }),
    );
  });

  it.each([401, 403])("marks HTTP %s as rejected", async (status) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status }),
    );

    await expect(
      probeAuth("http://localhost:8000/v1/auth/me", "bad-token"),
    ).resolves.toEqual({
      authenticated: false,
      rejected: true,
      status,
      principal: null,
    });
  });
});

describe("authentication principal", () => {
  it("derives ordinary and admin access from validated roles", () => {
    const user = parseAuthPrincipal({
      subject: "alice",
      tenant_id: "tenant-a",
      roles: ["user"],
    });
    const disabledModeSystem = parseAuthPrincipal({
      subject: "system",
      tenant_id: "system",
      roles: ["admin", "memory_archivist", "user"],
    });

    expect(authPrincipalIsAdmin(user)).toBe(false);
    expect(authPrincipalIsAdmin(disabledModeSystem)).toBe(true);
  });

  it.each([
    null,
    {},
    { subject: "", tenant_id: "tenant-a", roles: ["user"] },
    { subject: "alice", tenant_id: "", roles: ["user"] },
    { subject: "alice", tenant_id: "tenant-a", roles: "user" },
    { subject: "alice", tenant_id: "tenant-a", roles: [] },
    { subject: "alice", tenant_id: "tenant-a", roles: [""] },
  ])("rejects malformed principal payload %#", (payload) => {
    expect(() => parseAuthPrincipal(payload)).toThrow(
      "Invalid authentication principal response",
    );
  });

  it("rejects an HTTP 200 with a malformed principal", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({
        subject: "alice",
        tenant_id: "tenant-a",
      }),
    }));

    await expect(
      probeAuth("http://localhost:8000/v1/auth/me", "deployment-token"),
    ).rejects.toThrow("Invalid authentication principal response");
  });
});
