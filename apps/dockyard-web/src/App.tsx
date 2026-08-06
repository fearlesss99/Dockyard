import { useEffect, useState } from "react";
import { AppShell } from "./components/AppShell";
import { resolveDockyardRoute } from "./navigation/routes";
import { OperationalPage } from "./pages/OperationalPage";
import { WorkbenchPage } from "./pages/WorkbenchPage";
import type { DockyardClient } from "./client/dockyardClient";

export interface DockyardAppProps {
  readonly initialPath?: string;
  readonly client?: DockyardClient;
  readonly projectId?: string;
}

export function DockyardApp({ initialPath, client, projectId }: DockyardAppProps) {
  const browserPath = typeof window === "undefined" ? "/" : window.location.pathname;
  const [path, setPath] = useState(initialPath ?? browserPath);
  const route = resolveDockyardRoute(path);

  useEffect(() => {
    if (initialPath !== undefined || typeof window === "undefined") return;
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [initialPath]);

  const navigate = (nextPath: string) => {
    setPath(nextPath);
    if (initialPath === undefined && typeof window !== "undefined") {
      window.history.pushState(null, "", nextPath);
    }
  };

  return (
    <AppShell activeRoute={route} onNavigate={navigate} writeEnabled={client !== undefined}>
      {route.id === "workbench"
        ? <WorkbenchPage client={client} projectId={projectId} />
        : <OperationalPage route={route} client={client} projectId={projectId} />}
    </AppShell>
  );
}
