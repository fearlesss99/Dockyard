export const dockyardTokens = Object.freeze({
  color: Object.freeze({
    canvas: "#071015",
    canvasRaised: "#0a151c",
    surface: "#0e1b23",
    surfaceStrong: "#12242d",
    border: "#20343e",
    borderStrong: "#31505c",
    text: "#e8f1f3",
    textMuted: "#8fa5ad",
    accent: "#37d4d0",
    accentSoft: "#123c40",
    success: "#62d49a",
    warning: "#f2b84b",
    failure: "#f2767c",
    blocked: "#c99cff",
    unknown: "#7f9299",
    focus: "#7ce9e5",
  }),
  radius: Object.freeze({
    small: "8px",
    medium: "12px",
    large: "18px",
  }),
  space: Object.freeze({
    xs: "4px",
    sm: "8px",
    md: "12px",
    lg: "18px",
    xl: "24px",
  }),
  shadow: Object.freeze({
    panel: "0 18px 45px rgba(0, 0, 0, 0.22)",
    focus: "0 0 0 3px rgba(55, 212, 208, 0.2)",
  }),
});

export type DockyardSemanticState =
  | "neutral"
  | "selected"
  | "success"
  | "warning"
  | "failure"
  | "blocked"
  | "unknown";

export const semanticStateColor: Readonly<Record<DockyardSemanticState, string>> =
  Object.freeze({
    neutral: dockyardTokens.color.textMuted,
    selected: dockyardTokens.color.accent,
    success: dockyardTokens.color.success,
    warning: dockyardTokens.color.warning,
    failure: dockyardTokens.color.failure,
    blocked: dockyardTokens.color.blocked,
    unknown: dockyardTokens.color.unknown,
  });
