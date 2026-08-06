import type { ReadViewState } from "../types/views";

const labels: Readonly<Record<ReadViewState, readonly [string, string]>> = Object.freeze({
  fresh: ["数据已校验", "当前内容来自同一快照。"],
  loading: ["正在加载", "正在读取本机 Runner 的安全投影。"],
  empty: ["暂无数据", "当前筛选条件下没有记录。"],
  stale: ["数据可能已过期", "快照已变化，请等待刷新后再做判断。"],
  reconnecting: ["正在重新连接", "保留最后一份已校验快照，连接恢复前标记为可能过期。"],
  offline: ["本机 Runner 离线", "网页仍显示最后一份安全快照，不推测当前状态。"],
  not_ready: ["功能未就绪", "缺少已冻结的运行证据，相关操作保持禁用。"],
  unknown: ["证据状态未知", "无法确认的状态按 fail-closed 处理。"],
  forbidden: ["无权读取", "当前设备令牌不具备所需范围。"],
  failed: ["读取失败", "只显示安全错误摘要与 correlation digest。"],
});

export function ReadStateBanner({ state }: { readonly state: ReadViewState }) {
  const [title, message] = labels[state];
  return (
    <div className={`read-state read-state--${state}`} role="status">
      <strong>{title}</strong>
      <span>{message}</span>
    </div>
  );
}

export const readStateLabels = labels;
