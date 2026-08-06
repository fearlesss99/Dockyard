export type DockyardRouteId =
  | "workbench"
  | "requirements"
  | "tasks"
  | "runs"
  | "reviews"
  | "workers"
  | "projects"
  | "diagnostics"
  | "settings";

export interface DockyardRoute {
  readonly id: DockyardRouteId;
  readonly path: string;
  readonly label: string;
  readonly eyebrow: string;
  readonly description: string;
}

export const dockyardRoutes: readonly DockyardRoute[] = Object.freeze([
  { id: "workbench", path: "/", label: "工作台", eyebrow: "总览", description: "需求、任务、审批、运行与交付状态" },
  { id: "requirements", path: "/requirements", label: "需求与方案", eyebrow: "计划", description: "输入需求并审阅 PM 生成的计划草案" },
  { id: "tasks", path: "/tasks", label: "任务", eyebrow: "执行", description: "按生命周期查看任务与不可变证据" },
  { id: "runs", path: "/runs", label: "运行", eyebrow: "监督", description: "查看调度、ACK、心跳、重试和恢复阶段" },
  { id: "reviews", path: "/reviews", label: "审议与验收", eyebrow: "把关", description: "查看 MAD 裁决、交付和集成结果" },
  { id: "workers", path: "/workers", label: "Worker 与模型", eyebrow: "资源", description: "查看 Worker、Provider 与模型证据状态" },
  { id: "projects", path: "/projects", label: "项目", eyebrow: "Registry", description: "管理 Dockyard 本地项目登记，不删除 Git 仓库" },
  { id: "diagnostics", path: "/diagnostics", label: "系统诊断", eyebrow: "健康", description: "查看 Runner、快照和连接健康" },
  { id: "settings", path: "/settings", label: "设置", eyebrow: "本机", description: "配置本机显示与安全边界" },
]);

export function resolveDockyardRoute(pathname: string): DockyardRoute {
  const normalized = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return dockyardRoutes.find((route) => route.path === normalized) ?? dockyardRoutes[0];
}
