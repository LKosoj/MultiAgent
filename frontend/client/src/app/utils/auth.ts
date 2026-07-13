type BearerTokenInput = string | null | undefined;

export function normalizeBearerToken(token: BearerTokenInput): string | null {
  return token?.trim() || null;
}

export function buildAuthHeaders(token: BearerTokenInput): Record<string, string> {
  const normalizedToken = normalizeBearerToken(token);
  return normalizedToken
    ? { Authorization: `Bearer ${normalizedToken}` }
    : {};
}
