import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { dockyardRoutes, type DockyardRoute, type DockyardRouteId } from "../navigation/routes";
import { StatusPill } from "./StatusPill";
import { VideoBackground } from "./VideoBackground";

interface AppShellProps {
  readonly activeRoute: DockyardRoute;
  readonly children: ReactNode;
  readonly onNavigate: (path: string) => void;
  readonly writeEnabled: boolean;
}

const primaryRouteIds: readonly DockyardRouteId[] = [
  "workbench",
  "requirements",
  "tasks",
  "runs",
  "reviews",
];

const mobileRouteIds: readonly DockyardRouteId[] = [
  "workbench",
  "requirements",
  "tasks",
  "runs",
];

export function AppShell({ activeRoute, children, onNavigate, writeEnabled }: AppShellProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [recentRoutes, setRecentRoutes] = useState<readonly DockyardRoute[]>([activeRoute]);
  const historyTriggerRef = useRef<HTMLButtonElement>(null);
  const drawerTriggerRef = useRef<HTMLButtonElement | null>(null);
  const drawerCloseRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const moreMenuRef = useRef<HTMLDivElement>(null);
  const moreMenuTriggerRef = useRef<HTMLButtonElement>(null);
  const isWorkbench = activeRoute.id === "workbench";
  const primaryRoutes = dockyardRoutes.filter((route) => primaryRouteIds.includes(route.id));
  const secondaryRoutes = dockyardRoutes.filter((route) => !primaryRouteIds.includes(route.id));
  const mobileRoutes = dockyardRoutes.filter((route) => mobileRouteIds.includes(route.id));

  useEffect(() => {
    setRecentRoutes((current) => [
      activeRoute,
      ...current.filter((route) => route.id !== activeRoute.id),
    ].slice(0, 6));
    setMoreMenuOpen(false);
  }, [activeRoute]);

  useEffect(() => {
    if (!moreMenuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!moreMenuRef.current?.contains(event.target as Node)) setMoreMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setMoreMenuOpen(false);
      moreMenuTriggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [moreMenuOpen]);

  useEffect(() => {
    if (!drawerOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusFrame = window.requestAnimationFrame(() => drawerCloseRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = Array.from(drawerRef.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      (drawerTriggerRef.current ?? historyTriggerRef.current)?.focus();
      drawerTriggerRef.current = null;
    };
  }, [drawerOpen]);

  const navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => {
    event.preventDefault();
    setDrawerOpen(false);
    setMoreMenuOpen(false);
    onNavigate(path);
  };

  const openDrawer = (trigger: HTMLButtonElement) => {
    drawerTriggerRef.current = trigger;
    setMoreMenuOpen(false);
    setDrawerOpen(true);
  };

  return (
    <div className={isWorkbench ? "app-shell app-shell--workbench" : "app-shell"}>
      {isWorkbench ? <VideoBackground /> : null}

      <header className="app-header" inert={drawerOpen ? true : undefined}>
        <button
          ref={historyTriggerRef}
          className="icon-button history-trigger"
          type="button"
          aria-label="打开历史抽屉"
          aria-expanded={drawerOpen}
          aria-controls="dockyard-history-drawer"
          onClick={(event) => openDrawer(event.currentTarget)}
        >
          <span aria-hidden="true">☰</span>
        </button>

        <a className="brand" href="/" onClick={(event) => navigate(event, "/")}>
          <span className="brand__word">
            <span className="brand__word-accent">Dock</span>
            <span className="brand__word-base">yard</span>
          </span>
        </a>

        <nav className="top-nav" aria-label="Dockyard 主导航">
          {primaryRoutes.map((route) => (
            <a
              href={route.path}
              key={route.id}
              className={route.id === activeRoute.id ? "top-nav__item top-nav__item--active" : "top-nav__item"}
              aria-current={route.id === activeRoute.id ? "page" : undefined}
              onClick={(event) => navigate(event, route.path)}
            >
              {route.label}
            </a>
          ))}
          <div ref={moreMenuRef} className="top-nav__menu">
            <button
              ref={moreMenuTriggerRef}
              className={secondaryRoutes.some((route) => route.id === activeRoute.id) ? "top-nav__item top-nav__item--active" : "top-nav__item"}
              type="button"
              aria-expanded={moreMenuOpen}
              aria-haspopup="menu"
              aria-controls="dockyard-more-menu"
              onClick={() => setMoreMenuOpen((open) => !open)}
            >
              更多 <span aria-hidden="true">⌄</span>
            </button>
            {moreMenuOpen ? (
              <div id="dockyard-more-menu" className="top-nav__popover" role="menu">
                {secondaryRoutes.map((route) => (
                  <a
                    href={route.path}
                    key={route.id}
                    role="menuitem"
                    aria-current={route.id === activeRoute.id ? "page" : undefined}
                    onClick={(event) => navigate(event, route.path)}
                  >
                    <span>{route.label}</span>
                    <small>{route.eyebrow}</small>
                  </a>
                ))}
              </div>
            ) : null}
          </div>
        </nav>

        <div className="app-header__status" aria-label="本机运行状态">
          <span className={writeEnabled ? "snapshot-dot" : "snapshot-dot snapshot-dot--idle"} aria-hidden="true" />
          <span>
            <strong>{writeEnabled ? "Runner 已配置" : "Runner 未连接"}</strong>
            <small>{writeEnabled ? "写命令按证据开放" : "写命令未启用"}</small>
          </span>
        </div>
      </header>

      <main className="workspace" inert={drawerOpen ? true : undefined}>
        {!isWorkbench ? (
          <header className="page-heading">
            <div className="page-heading__title">
              <span className="eyebrow">{activeRoute.eyebrow}</span>
              <h1>{activeRoute.label}</h1>
              <p>{activeRoute.description}</p>
            </div>
            <div className="page-heading__facts" aria-label="当前上下文">
              <span>Standard</span>
              <span>balanced</span>
              <span>{writeEnabled ? "写命令按证据开放" : "写命令未启用"}</span>
              <StatusPill state={writeEnabled ? "success" : "unknown"}>
                {writeEnabled ? "本机通道已配置" : "等待快照"}
              </StatusPill>
            </div>
          </header>
        ) : null}
        <section className={isWorkbench ? "workspace__content workspace__content--hero" : "workspace__content"}>
          {children}
        </section>
      </main>

      {drawerOpen ? (
        <>
          <div className="drawer-backdrop" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
          <aside ref={drawerRef} id="dockyard-history-drawer" className="history-drawer" aria-label="最近查看" aria-modal="true" role="dialog">
            <header className="history-drawer__header">
              <div>
                <span className="eyebrow">本次浏览</span>
                <h2>最近查看</h2>
              </div>
              <button ref={drawerCloseRef} className="icon-button" type="button" aria-label="关闭历史抽屉" onClick={() => setDrawerOpen(false)}>
                <span aria-hidden="true">×</span>
              </button>
            </header>
            <nav className="history-list" aria-label="最近访问页面">
              {recentRoutes.map((route, index) => (
                <a href={route.path} key={route.id} onClick={(event) => navigate(event, route.path)}>
                  <span className="history-list__index">{String(index + 1).padStart(2, "0")}</span>
                  <span><strong>{route.label}</strong><small>{route.description}</small></span>
                </a>
              ))}
            </nav>
            <div className="history-drawer__all">
              <span className="eyebrow">全部页面</span>
              <nav aria-label="全部 Dockyard 页面">
                {dockyardRoutes.map((route) => (
                  <a href={route.path} key={route.id} onClick={(event) => navigate(event, route.path)}>{route.label}</a>
                ))}
              </nav>
            </div>
            <p className="history-drawer__note">仅记录本次浏览的安全页面访问，不写入项目证据或会话标识。</p>
          </aside>
        </>
      ) : null}

      <nav className="mobile-nav" aria-label="Dockyard 移动导航" inert={drawerOpen ? true : undefined}>
        {mobileRoutes.map((route) => (
          <a
            href={route.path}
            key={route.id}
            className={route.id === activeRoute.id ? "mobile-nav__item mobile-nav__item--active" : "mobile-nav__item"}
            aria-current={route.id === activeRoute.id ? "page" : undefined}
            onClick={(event) => navigate(event, route.path)}
          >
            {route.label === "需求与方案" ? "计划" : route.label}
          </a>
        ))}
        <button className="mobile-nav__item" type="button" onClick={(event) => openDrawer(event.currentTarget)}>更多</button>
      </nav>
    </div>
  );
}
