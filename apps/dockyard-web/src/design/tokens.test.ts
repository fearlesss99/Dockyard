import { describe, expect, it } from "vitest";
import { dockyardTokens, semanticStateColor } from "./tokens";

describe("Harbor Cyan design tokens", () => {
  it("is dark-only and Dockyard-owned", () => {
    expect(dockyardTokens.color.canvas).toBe("#071015");
    expect(dockyardTokens.color.accent).toBe("#37d4d0");
    expect(JSON.stringify(dockyardTokens)).not.toMatch(/hiveward/i);
  });

  it("distinguishes all semantic operational states", () => {
    expect(new Set(Object.values(semanticStateColor)).size).toBe(7);
    expect(semanticStateColor.unknown).not.toBe(semanticStateColor.success);
    expect(dockyardTokens.color.focus).not.toBe(dockyardTokens.color.failure);
  });

  it("freezes public token groups", () => {
    expect(Object.isFrozen(dockyardTokens)).toBe(true);
    expect(Object.isFrozen(dockyardTokens.color)).toBe(true);
  });
});
