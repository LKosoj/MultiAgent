import { describe, expect, it, vi } from "vitest";

describe("authentication helpers", () => {
  it("normalizes a token and builds the exact bearer header", async () => {
    const { buildAuthHeaders, normalizeBearerToken } = await import("../auth");

    expect(normalizeBearerToken("  deployment-token\t")).toBe("deployment-token");
    expect(buildAuthHeaders("  deployment-token\t")).toEqual({
      Authorization: "Bearer deployment-token",
    });
  });

  it.each([undefined, null, "", " \t\n "])(
    "omits authorization for an empty token (%s)",
    async (token) => {
      const { buildAuthHeaders, normalizeBearerToken } = await import("../auth");

      expect(normalizeBearerToken(token)).toBeNull();
      expect(buildAuthHeaders(token)).toEqual({});
    },
  );

  it("does not access browser persistence while loading or using the helpers", async () => {
    const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    const originalSessionStorage = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
    const localStorageAccess = vi.fn();
    const sessionStorageAccess = vi.fn();

    Object.defineProperties(globalThis, {
      localStorage: { configurable: true, get: localStorageAccess },
      sessionStorage: { configurable: true, get: sessionStorageAccess },
    });

    try {
      vi.resetModules();
      const { buildAuthHeaders, normalizeBearerToken } = await import("../auth");

      expect(normalizeBearerToken("token")).toBe("token");
      expect(buildAuthHeaders("token")).toEqual({ Authorization: "Bearer token" });
      expect(localStorageAccess).not.toHaveBeenCalled();
      expect(sessionStorageAccess).not.toHaveBeenCalled();
    } finally {
      if (originalLocalStorage) {
        Object.defineProperty(globalThis, "localStorage", originalLocalStorage);
      } else {
        Reflect.deleteProperty(globalThis, "localStorage");
      }
      if (originalSessionStorage) {
        Object.defineProperty(globalThis, "sessionStorage", originalSessionStorage);
      } else {
        Reflect.deleteProperty(globalThis, "sessionStorage");
      }
    }
  });
});
