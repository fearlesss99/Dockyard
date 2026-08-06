import { describe, expect, it } from "vitest";
import { overviewFixture } from "../fixtures/overview";
import { DOCKYARD_API_NAMESPACE } from "./api";

describe("Dockyard API client boundary", () => {
  it("uses the frozen namespace", () => {
    expect(DOCKYARD_API_NAMESPACE).toBe("/api/dockyard/v1");
  });

  it("keeps overview fixtures free of forbidden raw evidence", () => {
    const serialized = JSON.stringify(overviewFixture);
    expect(serialized).not.toMatch(/api[_-]?key/i);
    expect(serialized).not.toMatch(/stdout|stderr|source_thread_id|route_id/i);
    expect(serialized).not.toMatch(/[A-Za-z]:[\\/]/);
  });

  it("does not mislabel missing live evidence as ready", () => {
    const codex = overviewFixture.providers.find((item) => item.provider_id === "codex");
    const reasonix = overviewFixture.providers.find((item) => item.provider_id === "reasonix");
    expect(codex?.health).toBe("NOT_READY");
    expect(reasonix?.health).toBe("NOT_READY");
  });
});
