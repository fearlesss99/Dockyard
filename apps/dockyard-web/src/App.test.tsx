import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DockyardApp } from "./App";
import {
  isWorkbenchStreamConnected,
  WORKBENCH_READY_STREAM_LABELS,
  WORKBENCH_REFRESH_EVENT_TYPES,
} from "./pages/WorkbenchPage";

describe("Dockyard component smoke", () => {
  it("renders the workbench shell and safe summary", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/" />);
    expect(html).toContain("Dockyard");
    expect(html).toContain("运行状态总览");
    expect(html).toContain("Runner 未连接");
    expect(html).toContain("等待快照");
    expect(html).toContain("等待本机 Runner");
    expect(html).toContain("写命令");
    expect(html).toContain('src="/dockyard-hero.mp4"');
    expect(html).toContain("playsInline");
    expect(html).not.toContain("Attach");
    expect(html).not.toContain("Voice");
    expect(html).not.toContain("Prompts");
    expect(html).not.toMatch(/sk-[A-Za-z0-9_-]{12,}/);
  });

  it("renders top navigation and the history drawer trigger accessibly", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/reviews" />);
    expect(html).toContain('aria-label="Dockyard 主导航"');
    expect(html).toContain('aria-label="打开历史抽屉"');
    expect(html).toContain('class="brand__word-accent"');
    expect(html).toContain('class="brand__word-base"');
    expect(html).not.toContain('class="brand__mark"');
    expect(html).not.toContain("AgentDesk 控制台");
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).toContain('aria-controls="dockyard-more-menu"');
    expect(html).not.toContain("<details");
  });

  it("refreshes the workbench for every projected SSE resource class", () => {
    expect(WORKBENCH_REFRESH_EVENT_TYPES).toEqual([
      "snapshot.changed",
      "task.changed",
      "run.changed",
      "approval.changed",
      "review.changed",
      "provider.changed",
      "project.changed",
      "health.changed",
    ]);
    expect(WORKBENCH_READY_STREAM_LABELS.unavailable).toContain("SSE 不可用");
    expect(isWorkbenchStreamConnected("unavailable")).toBe(false);
    expect(isWorkbenchStreamConnected("connecting")).toBe(false);
    expect(isWorkbenchStreamConnected("connected")).toBe(true);
  });

  it("renders operational routes through the shared Dockyard shell", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/reviews" />);
    expect(html).toContain("审议与验收");
    expect(html).toContain("待验收交付");
    expect(html).toContain("集成边界");
  });

  it("keeps project commands not-ready without runtime evidence", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/projects" />);
    expect(html).toContain("not_ready");
    expect(html).toContain("不删除 Git 仓库");
    expect(html).not.toContain("删除仓库");
  });
});
