import type { DockyardSemanticState } from "../design/tokens";

interface StatusPillProps {
  readonly state: DockyardSemanticState;
  readonly children: string;
}

export function StatusPill({ state, children }: StatusPillProps) {
  return <span className={`status-pill status-pill--${state}`}>{children}</span>;
}
