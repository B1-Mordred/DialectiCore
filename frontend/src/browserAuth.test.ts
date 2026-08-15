import { describe, expect, it, vi } from "vitest";
import {
  browserAuthStorageKey,
  buildRequestHeaders,
  emptyBrowserAuthSession,
  readBrowserAuthSession,
  writeBrowserAuthSession
} from "./browserAuth";

describe("browser auth helpers", () => {
  it("builds JSON and static non-secret build headers", () => {
    expect(
      buildRequestHeaders(emptyBrowserAuthSession, {
        includeJson: true,
        env: {
          apiRole: "producer",
          apiUser: "web-ui"
        }
      })
    ).toEqual({
      "content-type": "application/json",
      "x-dialecticore-role": "producer",
      "x-dialecticore-user": "web-ui"
    });
  });

  it("formats Authorization bearer sessions for provider-managed auth", () => {
    expect(
      buildRequestHeaders({
        mode: "provider_session",
        bearerToken: "provider-token",
        providerTokenHeader: "authorization"
      })
    ).toEqual({
      authorization: "Bearer provider-token"
    });
  });

  it("sends custom provider token headers without adding a bearer prefix", () => {
    expect(
      buildRequestHeaders({
        mode: "provider_session",
        bearerToken: "provider-token",
        providerTokenHeader: "x-provider-token"
      })
    ).toEqual({
      "x-provider-token": "provider-token"
    });
  });

  it("normalizes blank provider token headers and trims bearer tokens", () => {
    expect(
      buildRequestHeaders({
        mode: "provider_session",
        bearerToken: " provider-token ",
        providerTokenHeader: " "
      })
    ).toEqual({
      authorization: "Bearer provider-token"
    });
  });

  it("lets a browser API-key session override static build headers", () => {
    expect(
      buildRequestHeaders(
        {
          mode: "api_key",
          apiKey: "browser-key",
          apiKeyHeader: "x-dialecticore-api-key",
          role: "admin",
          roleHeader: "x-dialecticore-role",
          userId: "operator",
          userHeader: "x-dialecticore-user"
        },
        {
          env: {
            apiKey: "build-key",
            apiRole: "producer",
            apiUser: "web-ui"
          }
        }
      )
    ).toEqual({
      "x-dialecticore-api-key": "browser-key",
      "x-dialecticore-role": "admin",
      "x-dialecticore-user": "operator"
    });
  });

  it("normalizes API-key session headers and ignores blank optional identity values", () => {
    expect(
      buildRequestHeaders({
        mode: "api_key",
        apiKey: " browser-key ",
        apiKeyHeader: " ",
        role: " admin ",
        roleHeader: "",
        userId: " ",
        userHeader: " "
      })
    ).toEqual({
      "x-dialecticore-api-key": "browser-key",
      "x-dialecticore-role": "admin"
    });
  });

  it("does not attach blank stored bearer or API-key credentials", () => {
    expect(
      buildRequestHeaders({
        mode: "provider_session",
        bearerToken: " ",
        providerTokenHeader: "authorization"
      })
    ).toEqual({});
    expect(
      buildRequestHeaders({
        mode: "api_key",
        apiKey: " ",
        role: "admin",
        userId: "operator"
      })
    ).toEqual({});
  });

  it("ignores malformed stored browser auth sessions", () => {
    vi.stubGlobal("window", {
      localStorage: {
        getItem: () => "{",
        removeItem: vi.fn(),
        setItem: vi.fn()
      }
    });

    expect(readBrowserAuthSession()).toEqual(emptyBrowserAuthSession);

    vi.unstubAllGlobals();
  });

  it("persists non-empty sessions and removes the stored session on logout", () => {
    const storage = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        removeItem: (key: string) => storage.delete(key),
        setItem: (key: string, value: string) => storage.set(key, value)
      }
    });

    writeBrowserAuthSession({ mode: "api_key", apiKey: "browser-key" });
    expect(storage.has(browserAuthStorageKey)).toBe(true);
    expect(readBrowserAuthSession()).toMatchObject({
      mode: "api_key",
      apiKey: "browser-key"
    });

    writeBrowserAuthSession(emptyBrowserAuthSession);
    expect(storage.has(browserAuthStorageKey)).toBe(false);

    vi.unstubAllGlobals();
  });
});
