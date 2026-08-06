import { DockyardClient, type DockyardClientCredentials } from "./dockyardClient";

export interface DockyardLoopbackRuntimeConfig {
  readonly apiBaseUrl: string;
  readonly projectId: string;
  readonly deviceId: string;
  readonly deviceToken: string;
  readonly csrfValue: string;
}

export interface DockyardBrowserRuntime {
  readonly client: DockyardClient;
  readonly projectId: string;
}

export class DockyardRuntimeConfigError extends Error {}

function text(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0 || value !== value.trim() || /[\u0000-\u001f]/.test(value)) {
    throw new DockyardRuntimeConfigError(`${field} is invalid`);
  }
  return value;
}

function loopbackUrl(value: unknown): string {
  const raw = text(value, "apiBaseUrl");
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new DockyardRuntimeConfigError("apiBaseUrl is invalid");
  }
  if (parsed.protocol !== "http:" || !["127.0.0.1", "[::1]", "localhost"].includes(parsed.hostname)) {
    throw new DockyardRuntimeConfigError("apiBaseUrl is not loopback");
  }
  if (parsed.pathname !== "" && parsed.pathname !== "/") throw new DockyardRuntimeConfigError("apiBaseUrl path is invalid");
  return raw.replace(/\/$/, "");
}

export function createDockyardRuntimeFromConfig(value: unknown): DockyardBrowserRuntime {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new DockyardRuntimeConfigError("Dockyard local runtime is unavailable");
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== 5 || !["apiBaseUrl", "projectId", "deviceId", "deviceToken", "csrfValue"].every((key) => key in record)) {
    throw new DockyardRuntimeConfigError("Dockyard local runtime is malformed");
  }
  const credentials: DockyardClientCredentials = {
    deviceId: text(record.deviceId, "deviceId"),
    bearerToken: text(record.deviceToken, "deviceToken"),
    csrfValue: text(record.csrfValue, "csrfValue"),
  };
  return {
    client: new DockyardClient(loopbackUrl(record.apiBaseUrl), credentials),
    projectId: text(record.projectId, "projectId"),
  };
}

export function createDockyardClientFromRuntime(value: unknown): DockyardClient {
  return createDockyardRuntimeFromConfig(value).client;
}

declare global {
  interface Window {
    __DOCKYARD_LOOPBACK__?: unknown;
  }
}
