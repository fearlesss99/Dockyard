export const dockyardTokens = Object.freeze({
  color: Object.freeze({
    canvas: "#f3f5f2",
    canvasRaised: "#f8f9f7",
    surface: "#ffffff",
    surfaceStrong: "#e8ede8",
    border: "#d5ddd6",
    borderStrong: "#aebbb0",
    text: "#111713",
    textMuted: "#667169",
    accent: "#246b45",
    accentSoft: "#dcebe1",
    success: "#2f7d50",
    warning: "#a66a13",
    failure: "#b23a43",
    blocked: "#76539a",
    unknown: "#6d7470",
    focus: "#176f8f",
  }),
  radius: Object.freeze({
    small: "6px",
    medium: "8px",
    large: "8px",
  }),
  space: Object.freeze({
    xs: "4px",
    sm: "8px",
    md: "12px",
    lg: "18px",
    xl: "24px",
  }),
  shadow: Object.freeze({
    panel: "0 16px 42px rgba(19, 31, 22, 0.12)",
    focus: "0 0 0 3px rgba(23, 111, 143, 0.22)",
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
