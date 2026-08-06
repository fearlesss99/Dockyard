import type { CommandConfirmation } from "../types/commands";

export function canConfirm(
  confirmation: CommandConfirmation,
  acknowledged: boolean,
  phrase: string,
): boolean {
  return acknowledged && (
    confirmation.requiredPhrase === null || phrase === confirmation.requiredPhrase
  );
}

export function ConfirmationDialog({
  confirmation,
  acknowledged,
  phrase,
  busy,
  onAcknowledge,
  onPhrase,
  onCancel,
  onConfirm,
}: {
  readonly confirmation: CommandConfirmation;
  readonly acknowledged: boolean;
  readonly phrase: string;
  readonly busy: boolean;
  readonly onAcknowledge: (value: boolean) => void;
  readonly onPhrase: (value: string) => void;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}) {
  return (
    <div className="dialog-backdrop" role="presentation">
      <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <span className="eyebrow">明确确认</span>
        <h3 id="confirm-title">{confirmation.title}</h3>
        <p>{confirmation.consequence}</p>
        <dl className="detail-list">
          {confirmation.facts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
        </dl>
        <label className="confirm-check">
          <input
            type="checkbox"
            checked={acknowledged}
            onChange={(event) => onAcknowledge(event.currentTarget.checked)}
          />
          我已核对 revision、digest 和操作对象
        </label>
        {confirmation.requiredPhrase && (
          <label className="field-stack">
            <span>请输入：{confirmation.requiredPhrase}</span>
            <input value={phrase} onChange={(event) => onPhrase(event.currentTarget.value)} />
          </label>
        )}
        <footer className="dialog-actions">
          <button className="button button--quiet" type="button" onClick={onCancel}>取消</button>
          <button
            className={confirmation.destructive ? "button button--danger" : "button button--primary"}
            type="button"
            disabled={busy || !canConfirm(confirmation, acknowledged, phrase)}
            onClick={onConfirm}
          >
            {busy ? "提交中…" : "确认执行"}
          </button>
        </footer>
      </section>
    </div>
  );
}
