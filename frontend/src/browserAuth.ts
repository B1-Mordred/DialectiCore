export const browserAuthStorageKey = "dialecticore.browserAuth.v1";

export type BrowserAuthMode = "none" | "provider_session" | "api_key";

export type BrowserAuthSession = {
  mode: BrowserAuthMode;
  bearerToken?: string;
  providerTokenHeader?: string;
  apiKey?: string;
  apiKeyHeader?: string;
  role?: string;
  roleHeader?: string;
  userId?: string;
  userHeader?: string;
  updatedAt?: string;
};

export type BrowserAuthBuildEnv = {
  apiKey?: string;
  apiRole?: string;
  apiUser?: string;
};

export const emptyBrowserAuthSession: BrowserAuthSession = { mode: "none" };

export function readBrowserAuthSession(): BrowserAuthSession {
  if (typeof window === "undefined") {
    return emptyBrowserAuthSession;
  }
  const stored = window.localStorage.getItem(browserAuthStorageKey);
  if (!stored) {
    return emptyBrowserAuthSession;
  }
  try {
    const parsed = JSON.parse(stored) as BrowserAuthSession;
    if (parsed.mode === "provider_session" || parsed.mode === "api_key" || parsed.mode === "none") {
      return parsed;
    }
  } catch {
    return emptyBrowserAuthSession;
  }
  return emptyBrowserAuthSession;
}

export function writeBrowserAuthSession(session: BrowserAuthSession) {
  if (typeof window === "undefined") {
    return;
  }
  if (session.mode === "none") {
    window.localStorage.removeItem(browserAuthStorageKey);
    return;
  }
  window.localStorage.setItem(
    browserAuthStorageKey,
    JSON.stringify({ ...session, updatedAt: new Date().toISOString() })
  );
}

export function buildRequestHeaders(
  session: BrowserAuthSession,
  options: { includeJson?: boolean; env?: BrowserAuthBuildEnv } = {}
): Record<string, string> {
  const headers: Record<string, string> = {};
  if (options.includeJson) {
    headers["content-type"] = "application/json";
  }
  if (options.env?.apiKey) {
    headers["x-dialecticore-api-key"] = options.env.apiKey;
  }
  if (options.env?.apiRole) {
    headers["x-dialecticore-role"] = options.env.apiRole;
  }
  if (options.env?.apiUser) {
    headers["x-dialecticore-user"] = options.env.apiUser;
  }
  if (session.mode === "provider_session" && trimmedString(session.bearerToken)) {
    const tokenHeader = headerNameOrDefault(session.providerTokenHeader, "authorization");
    const token = trimmedString(session.bearerToken);
    headers[tokenHeader] =
      tokenHeader.toLowerCase() === "authorization"
        ? `Bearer ${token}`
        : token;
  }
  if (session.mode === "api_key" && trimmedString(session.apiKey)) {
    headers[headerNameOrDefault(session.apiKeyHeader, "x-dialecticore-api-key")] = trimmedString(
      session.apiKey
    );
    if (trimmedString(session.role)) {
      headers[headerNameOrDefault(session.roleHeader, "x-dialecticore-role")] = trimmedString(
        session.role
      );
    }
    if (trimmedString(session.userId)) {
      headers[headerNameOrDefault(session.userHeader, "x-dialecticore-user")] = trimmedString(
        session.userId
      );
    }
  }
  return headers;
}

function headerNameOrDefault(value: string | undefined, fallback: string): string {
  const headerName = trimmedString(value);
  return headerName || fallback;
}

function trimmedString(value: string | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

export function requestHeaders(includeJson = false): Record<string, string> {
  return buildRequestHeaders(readBrowserAuthSession(), {
    includeJson,
    env: {
      apiKey: import.meta.env.VITE_DIALECTICORE_API_KEY,
      apiRole: import.meta.env.VITE_DIALECTICORE_ROLE,
      apiUser: import.meta.env.VITE_DIALECTICORE_USER
    }
  });
}
