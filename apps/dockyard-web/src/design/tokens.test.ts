import { describe, expect, it } from "vitest";
import { dockyardTokens, semanticStateColor } from "./tokens";

function rgb(hex: string): readonly [number, number, number] {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!match) throw new Error(`Expected a six-digit hex color, received ${hex}`);
  return [Number.parseInt(match[1], 16), Number.parseInt(match[2], 16), Number.parseInt(match[3], 16)];
}

describe("Dockyard light hero design tokens", () => {
  it("uses a light canvas, forest accent, and Dockyard-owned vocabulary", () => {
    const canvas = rgb(dockyardTokens.color.canvas);
    const accent = rgb(dockyardTokens.color.accent);
    expect(Math.min(...canvas)).toBeGreaterThan(220);
    expect(accent[1] - accent[2]).toBeGreaterThan(20);
    expect(accent[1]).toBeGreaterThan(accent[0]);
    expect(JSON.stringify(dockyardTokens)).not.toMatch(/hiveward|logoipsum/i);
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
