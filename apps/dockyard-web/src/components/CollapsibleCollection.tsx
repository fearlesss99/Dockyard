import { useState, type ReactNode } from "react";

export type CollectionGroupKey = "active" | "today" | "recent" | "older" | string;

export interface CollectionGroup<T> {
  readonly key: CollectionGroupKey;
  readonly label: string;
  readonly description: string;
  readonly items: readonly T[];
  readonly openByDefault?: boolean;
}

interface CollapsibleCollectionProps<T> {
  readonly groups: readonly CollectionGroup<T>[];
  readonly renderItem: (item: T) => ReactNode;
  readonly itemKey: (item: T) => string;
  readonly className?: string;
}

export function CollapsibleCollection<T>({
  groups,
  renderItem,
  itemKey,
  className,
}: CollapsibleCollectionProps<T>) {
  return (
    <div className={className ? `collection-list ${className}` : "collection-list"}>
      {groups.map((group) => (
        <CollapsibleCollectionGroup key={group.key} group={group} renderItem={renderItem} itemKey={itemKey} />
      ))}
    </div>
  );
}

function CollapsibleCollectionGroup<T>({
  group,
  renderItem,
  itemKey,
}: {
  readonly group: CollectionGroup<T>;
  readonly renderItem: (item: T) => ReactNode;
  readonly itemKey: (item: T) => string;
}) {
  const [open, setOpen] = useState(group.openByDefault ?? group.key !== "older");
  return (
    <details
      className="collection-group"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="collection-group__summary">
        <span className="collection-group__heading">
          <span className="eyebrow">{group.label}</span>
          <strong>{group.description}</strong>
        </span>
        <span className="collection-group__count">{group.items.length}</span>
      </summary>
      <div className="collection-group__items">
        {group.items.map((item) => <div key={itemKey(item)}>{renderItem(item)}</div>)}
      </div>
    </details>
  );
}

export function TimeGroupedCollection<T>({
  items,
  getUpdatedAt,
  isActive,
  renderItem,
  itemKey,
  className,
  now = Date.now(),
}: {
  readonly items: readonly T[];
  readonly getUpdatedAt: (item: T) => string | null | undefined;
  readonly isActive?: (item: T) => boolean;
  readonly renderItem: (item: T) => ReactNode;
  readonly itemKey: (item: T) => string;
  readonly className?: string;
  readonly now?: number;
}) {
  return (
    <CollapsibleCollection
      groups={groupByTime(items, getUpdatedAt, isActive, now)}
      renderItem={renderItem}
      itemKey={itemKey}
      className={className}
    />
  );
}

export function groupByTime<T>(
  items: readonly T[],
  getUpdatedAt: (item: T) => string | null | undefined,
  isActive: ((item: T) => boolean) | undefined = undefined,
  now = Date.now(),
): readonly CollectionGroup<T>[] {
  const buckets: Record<CollectionGroupKey, T[]> = {
    active: [],
    today: [],
    recent: [],
    older: [],
  };
  const todayStart = new Date(now);
  todayStart.setHours(0, 0, 0, 0);
  const recentStart = todayStart.getTime() - 6 * 24 * 60 * 60 * 1000;

  for (const item of items) {
    if (isActive?.(item)) {
      buckets.active.push(item);
      continue;
    }
    const timestamp = Date.parse(getUpdatedAt(item) ?? "");
    if (!Number.isFinite(timestamp) || timestamp < recentStart) {
      buckets.older.push(item);
    } else if (timestamp >= todayStart.getTime()) {
      buckets.today.push(item);
    } else {
      buckets.recent.push(item);
    }
  }

  const definitions: readonly Omit<CollectionGroup<T>, "items">[] = [
    { key: "active", label: "ACTIVE", description: "进行中与待处理", openByDefault: true },
    { key: "today", label: "TODAY", description: "今天更新", openByDefault: true },
    { key: "recent", label: "RECENT", description: "近 7 天更新", openByDefault: true },
    { key: "older", label: "ARCHIVE", description: "更早记录", openByDefault: false },
  ];
  return definitions
    .map((definition) => ({ ...definition, items: buckets[definition.key] }))
    .filter((group) => group.items.length > 0);
}
