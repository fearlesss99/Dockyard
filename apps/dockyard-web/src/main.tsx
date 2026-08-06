import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { DockyardApp } from "./App";
import { createDockyardRuntimeFromConfig } from "./client/dockyardRuntime";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) throw new Error("Dockyard root element is missing");

let runtime;
try {
  runtime = createDockyardRuntimeFromConfig(window.__DOCKYARD_LOOPBACK__);
} catch {
  runtime = undefined;
}

createRoot(root).render(
  <StrictMode><DockyardApp client={runtime?.client} projectId={runtime?.projectId} /></StrictMode>,
);
