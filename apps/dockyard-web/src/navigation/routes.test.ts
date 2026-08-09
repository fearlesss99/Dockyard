import { describe, expect, it } from "vitest";
import { dockyardRoutes, resolveDockyardRoute } from "./routes";

describe("Dockyard routes", () => {
  it("preserves the exact nine Chinese business routes across shell redesigns", () => {
    expect(dockyardRoutes.map((route) => route.label)).toEqual([
      "工作台",
      "需求与方案",
      "任务",
      "运行",
      "审议与验收",
      "Worker 与模型",
      "项目",
      "系统诊断",
      "设置",
    ]);
    expect(dockyardRoutes.map((route) => route.path)).toEqual([
      "/",
      "/requirements",
      "/tasks",
      "/runs",
      "/reviews",
      "/workers",
      "/projects",
      "/diagnostics",
      "/settings",
    ]);
  });

  it("uses unique stable paths", () => {
    expect(new Set(dockyardRoutes.map((route) => route.path)).size).toBe(9);
    expect(resolveDockyardRoute("/runs/").id).toBe("runs");
  });

  it("fails safe to the read-only workbench for an unknown path", () => {
    expect(resolveDockyardRoute("/arbitrary-terminal").id).toBe("workbench");
  });
});
