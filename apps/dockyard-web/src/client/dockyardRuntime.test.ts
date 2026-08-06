import { describe, expect, it } from "vitest";
import { DockyardClient } from "./dockyardClient";
import {
  createDockyardClientFromRuntime,
  createDockyardRuntimeFromConfig,
  DockyardRuntimeConfigError,
} from "./dockyardRuntime";

describe("Dockyard loopback runtime bootstrap", () => {
  const config = {
    apiBaseUrl: "http://127.0.0.1:4312",
    projectId: "PRJ-1",
    deviceId: "DEV-1",
    deviceToken: "local-device-token",
    csrfValue: "CSRF-1",
  };

  it("constructs a typed client only from an exact loopback configuration", () => {
    expect(createDockyardClientFromRuntime(config)).toBeInstanceOf(DockyardClient);
    const runtime = createDockyardRuntimeFromConfig(config);
    expect(runtime.client).toBeInstanceOf(DockyardClient);
    expect(runtime.projectId).toBe("PRJ-1");
  });

  it("rejects missing, malformed, and non-loopback runtime configuration before fetch", () => {
    expect(() => createDockyardClientFromRuntime(undefined)).toThrow(DockyardRuntimeConfigError);
    expect(() => createDockyardClientFromRuntime({ ...config, extra: "no" })).toThrow(DockyardRuntimeConfigError);
    expect(() => createDockyardClientFromRuntime({ ...config, apiBaseUrl: "https://example.invalid" })).toThrow(DockyardRuntimeConfigError);
  });
});
