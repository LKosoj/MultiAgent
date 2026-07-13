"use client";

import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { buildAuthHeaders, normalizeBearerToken } from "../../utils/auth";

export type AuthPrincipal = {
  subject: string;
  tenant_id: string;
  roles: readonly string[];
};

type AuthProbeResult =
  | {
      authenticated: true;
      rejected: false;
      status: number;
      principal: AuthPrincipal;
    }
  | {
      authenticated: false;
      rejected: boolean;
      status: number;
      principal: null;
    };

export function parseAuthPrincipal(value: unknown): AuthPrincipal {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid authentication principal response");
  }
  const record = value as Record<string, unknown>;
  if (
    typeof record.subject !== "string"
    || !record.subject.trim()
    || typeof record.tenant_id !== "string"
    || !record.tenant_id.trim()
    || !Array.isArray(record.roles)
    || record.roles.length === 0
    || record.roles.some((role) => typeof role !== "string" || !role.trim())
  ) {
    throw new Error("Invalid authentication principal response");
  }
  return {
    subject: record.subject.trim(),
    tenant_id: record.tenant_id.trim(),
    roles: record.roles.map((role) => (role as string).trim()),
  };
}

export function authPrincipalIsAdmin(principal: AuthPrincipal): boolean {
  return principal.roles.includes("admin");
}

export function visibleSectionsForAuth<T extends { id: string }>(
  sections: readonly T[],
  isAdmin: boolean,
): readonly T[] {
  return isAdmin ? sections : sections.filter((section) => section.id !== "db");
}

export function shouldRenderAdminDbSection(
  activeSection: string,
  isAdmin: boolean,
): boolean {
  return isAdmin && activeSection === "db";
}

export async function probeAuth(
  authUrl: string,
  token: string,
  signal?: AbortSignal,
): Promise<AuthProbeResult> {
  const response = await fetch(authUrl, {
    method: "GET",
    headers: buildAuthHeaders(token),
    signal,
  });
  if (response.ok) {
    return {
      authenticated: true,
      rejected: false,
      status: response.status,
      principal: parseAuthPrincipal(await response.json()),
    };
  }
  return {
    authenticated: false,
    rejected: response.status === 401 || response.status === 403,
    status: response.status,
    principal: null,
  };
}

type AuthSession = {
  headers: Record<string, string>;
  principal: AuthPrincipal;
  isAdmin: boolean;
  onForget: () => void;
};

type AuthGateProps = {
  authUrl: string;
  children: (session: AuthSession) => ReactNode;
};

type AuthStatus = "probing" | "required" | "authenticated";

export function AuthGate({ authUrl, children }: AuthGateProps) {
  const [token, setToken] = useState("");
  const [principal, setPrincipal] = useState<AuthPrincipal | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [status, setStatus] = useState<AuthStatus>("probing");
  const [error, setError] = useState<string | null>(null);
  const probeControllerRef = useRef<AbortController | null>(null);
  const headers = useMemo(() => buildAuthHeaders(token), [token]);

  const checkToken = useCallback(
    async (candidate: string) => {
      probeControllerRef.current?.abort();
      const controller = new AbortController();
      probeControllerRef.current = controller;
      setToken("");
      setPrincipal(null);
      setStatus("probing");
      setError(null);

      try {
        const result = await probeAuth(authUrl, candidate, controller.signal);
        if (probeControllerRef.current !== controller) return;
        if (result.authenticated) {
          setToken(normalizeBearerToken(candidate) ?? "");
          setPrincipal(result.principal);
          setTokenInput("");
          setStatus("authenticated");
          return;
        }

        setToken("");
        setPrincipal(null);
        setTokenInput("");
        setStatus("required");
        setError(
          result.rejected
            ? "Доступ отклонён. Проверьте токен и повторите попытку."
            : `Не удалось проверить доступ (HTTP ${result.status}).`,
        );
      } catch (probeError) {
        if (controller.signal.aborted) return;
        setToken("");
        setPrincipal(null);
        setTokenInput("");
        setStatus("required");
        setError(
          probeError instanceof Error
            ? probeError.message
            : "Не удалось проверить доступ.",
        );
      } finally {
        if (probeControllerRef.current === controller) {
          probeControllerRef.current = null;
        }
      }
    },
    [authUrl],
  );

  useEffect(() => {
    void checkToken("");
    return () => probeControllerRef.current?.abort();
  }, [checkToken]);

  const handleForget = useCallback(() => {
    probeControllerRef.current?.abort();
    probeControllerRef.current = null;
    setToken("");
    setPrincipal(null);
    setTokenInput("");
    setError(null);
    setStatus("required");
  }, []);

  const handleConnect = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void checkToken(tokenInput);
  };

  if (status === "authenticated" && principal) {
    return children({
      headers,
      principal,
      isAdmin: authPrincipalIsAdmin(principal),
      onForget: handleForget,
    });
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <div className="app-title">MultiAgent Studio</div>
          <div className="app-subtitle">Подключение к защищённому backend</div>
        </div>
        <span className="status-pill status-pill--offline">
          {status === "probing" ? "Checking" : "Disconnected"}
        </span>
      </header>
      <main className="app-grid">
        <section
          className="panel content-panel"
          style={{ gridColumn: "1 / -1" }}
        >
          <div className="section">
            <div className="section-header">
              <div className="section-title">Авторизация</div>
            </div>
            <form className="card" onSubmit={handleConnect}>
              <label className="field">
                <span className="label">Deployment token</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={tokenInput}
                  onChange={(event) => setTokenInput(event.target.value)}
                  placeholder="Введите bearer token"
                />
              </label>
              {error ? <div className="card-hint" role="alert">{error}</div> : null}
              <div className="button-row">
                <button className="button" type="submit" disabled={status === "probing"}>
                  Connect
                </button>
                <button className="button secondary" type="button" onClick={handleForget}>
                  Forget
                </button>
              </div>
            </form>
          </div>
        </section>
      </main>
    </div>
  );
}
