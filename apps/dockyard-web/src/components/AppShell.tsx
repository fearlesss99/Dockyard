import type { ReactNode } from "react";
import { dockyardRoutes, type DockyardRoute } from "../navigation/routes";
import { StatusPill } from "./StatusPill";

interface AppShellProps {
  readonly activeRoute: DockyardRoute;
  readonly children: ReactNode;
  readonly onNavigate: (path: string) => void;
  readonly writeEnabled: boolean;
}

export function AppShell({ activeRoute, children, onNavigate, writeEnabled }: AppShellProps) {
  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Dockyard 主导航">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">DY</span>
          <span>
            <strong>Dockyard</strong>
            <small>AgentDesk 控制台</small>
          </span>
        </div>

        <nav className="nav-list">
          {dockyardRoutes.map((route, index) => (
            <a
              href={route.path}
              key={route.id}
              className={route.id === activeRoute.id ? "nav-item nav-item--active" : "nav-item"}
              aria-current={route.id === activeRoute.id ? "page" : undefined}
              onClick={(event) => {
                event.preventDefault();
                onNavigate(route.path);
              }}
            >
              <span className="nav-item__index">{String(index + 1).padStart(2, "0")}</span>
              <span>{route.label}</span>
            </a>
          ))}
        </nav>

        <div className="sidebar__footer">
          <StatusPill state={writeEnabled ? "success" : "unknown"}>
            {writeEnabled ? "本机 API 已配置" : "Runner 未连接"}
          </StatusPill>
          <small>单用户 · 无登录 · loopback</small>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <span className="eyebrow">{activeRoute.eyebrow}</span>
            <h1>{activeRoute.label}</h1>
          </div>
          <div className="topbar__status">
            <span className="snapshot-dot" aria-hidden="true" />
            {writeEnabled ? "安全投影通道" : "等待快照"}
          </div>
        </header>
        <section className="workspace__content">{children}</section>
      </main>

      <aside className="inspector" aria-label="当前上下文">
        <span className="eyebrow">当前上下文</span>
        <h2>{activeRoute.label}</h2>
        <p>{activeRoute.description}</p>
        <dl className="inspector__facts">
          <div><dt>运行模式</dt><dd>Standard</dd></div>
          <div><dt>审议深度</dt><dd>balanced</dd></div>
          <div><dt>写命令</dt><dd><StatusPill state={writeEnabled ? "success" : "unknown"}>{writeEnabled ? "本机已连接" : "尚未启用"}</StatusPill></dd></div>
          <div><dt>数据来源</dt><dd>安全投影</dd></div>
        </dl>
        <div className="boundary-note">
          网页不接触 API Key、原始提示、stdout/stderr、会话标识或绝对路径。
        </div>
      </aside>
    </div>
  );
}
