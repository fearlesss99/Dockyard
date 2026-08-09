import { describe, expect, it } from "vitest";
import { groupByTime } from "./CollapsibleCollection";

describe("groupByTime", () => {
  const now = Date.parse("2026-08-09T12:00:00Z");
  const items = [
    { id: "active", updatedAt: "2026-07-01T00:00:00Z", state: "in_progress" },
    { id: "today", updatedAt: "2026-08-09T08:00:00Z", state: "finalized" },
    { id: "recent", updatedAt: "2026-08-05T08:00:00Z", state: "finalized" },
    { id: "older", updatedAt: "2026-07-01T08:00:00Z", state: "finalized" },
  ];

  it("keeps active records open even when their timestamp is old", () => {
    const groups = groupByTime(items, (item) => item.updatedAt, (item) => item.state === "in_progress", now);
    expect(groups.map((group) => [group.key, group.items.map((item) => item.id)])).toEqual([
      ["active", ["active"]],
      ["today", ["today"]],
      ["recent", ["recent"]],
      ["older", ["older"]],
    ]);
    expect(groups[0]?.openByDefault).toBe(true);
    expect(groups[3]?.openByDefault).toBe(false);
  });

  it("puts invalid timestamps into the collapsed archive group", () => {
    const groups = groupByTime([{ id: "unknown", updatedAt: "not-a-date" }], (item) => item.updatedAt, undefined, now);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.key).toBe("older");
  });
});
