import { describe, expect, it } from "vitest";
import { dockyardRoutes, resolveDockyardRoute } from "./routes";

describe("Dockyard routes", () => {
  it("freezes the exact nine Chinese navigation items", () => {
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
  });

  it("uses unique stable paths", () => {
    expect(new Set(dockyardRoutes.map((route) => route.path)).size).toBe(9);
    expect(resolveDockyardRoute("/runs/").id).toBe("runs");
  });

  it("fails safe to the read-only workbench for an unknown path", () => {
    expect(resolveDockyardRoute("/arbitrary-terminal").id).toBe("workbench");
  });
});
