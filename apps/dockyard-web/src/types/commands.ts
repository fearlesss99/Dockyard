export type DockyardCommandType =
  | "plan.create"
  | "plan.update"
  | "plan.discard"
  | "plan.approve"
  | "task.retry"
  | "task.cancel"
  | "dispatch.terminate"
  | "delivery.accept"
  | "delivery.return"
  | "provider.binding.update"
  | "project.remove";

export interface DockyardCommandEnvelope {
  readonly schema_version: "dockyard.command/v1";
  readonly command_id: string;
  readonly project_id: string;
  readonly command_type: DockyardCommandType;
  readonly idempotency_key: string;
  readonly expected_snapshot_commit: string;
  readonly expected_revision: number;
  readonly confirmation_id: string | null;
  readonly payload_digest: string;
}

export interface DockyardCommandReceipt {
  readonly schema_version: "dockyard.command-receipt/v1";
  readonly command_id: string;
  readonly project_id: string;
  readonly command_type: DockyardCommandType;
  readonly outcome: string;
  readonly canonical_event_id: string | null;
  readonly snapshot_commit: string;
  readonly replayed: boolean;
  readonly completed_at: string;
  readonly content_digest: string;
}

export interface DockyardCommandDraft {
  readonly projectId: string;
  readonly commandType: DockyardCommandType;
  readonly endpoint: string;
  readonly method: "POST" | "PATCH" | "PUT";
  readonly expectedSnapshotCommit: string;
  readonly expectedRevision: number;
  readonly confirmationId: string | null;
  readonly payload: Readonly<Record<string, unknown>>;
}

export type CommandUiState = "idle" | "confirming" | "submitting" | "committed" | "stale" | "failed";

export interface CommandConfirmation {
  readonly title: string;
  readonly consequence: string;
  readonly facts: readonly Readonly<{ label: string; value: string }>[];
  readonly requiredPhrase: string | null;
  readonly destructive: boolean;
  readonly mobileAllowed: boolean;
}

export interface PlanEditorTask {
  readonly taskId: string;
  readonly title: string;
  readonly description: string;
  readonly executionMode: "parallel" | "serial";
  readonly dependencies: readonly string[];
  readonly agentId: string;
  readonly providerId: "claude" | "codex" | "reasonix";
  readonly modelId: string;
  readonly budgetTokens: number;
  readonly maxAttempts: 1 | 2 | 3;
}
