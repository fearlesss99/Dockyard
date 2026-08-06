import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DockyardApp } from "./App";

describe("Dockyard component smoke", () => {
  it("renders the workbench shell and safe summary", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/" />);
    expect(html).toContain("Dockyard");
    expect(html).toContain("AgentDesk 控制台");
    expect(html).toContain("Runner 未连接");
    expect(html).toContain("等待快照");
    expect(html).toContain("等待本机 Runner");
    expect(html).toContain("写命令");
    expect(html).not.toMatch(/sk-[A-Za-z0-9_-]{12,}/);
  });

  it("renders operational routes through the same three-column shell", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/reviews" />);
    expect(html).toContain("审议与验收");
    expect(html).toContain("待验收交付");
    expect(html).toContain("集成边界");
    expect(html).toContain("当前上下文");
  });

  it("keeps project commands not-ready without runtime evidence", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/projects" />);
    expect(html).toContain("not_ready");
    expect(html).toContain("不删除 Git 仓库");
    expect(html).not.toContain("删除仓库");
  });
});
