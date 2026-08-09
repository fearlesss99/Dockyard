# Dockyard 视觉决策

## 2026-08-09：Waypoint Light（采用）

用户批准完整参考图方向，同时确认品牌继续使用 `Dockyard`、首屏以运行状态总览为主、
采用顶部导航与历史抽屉，并且只重组现有业务能力。

- **构图**：全视口视频 Hero 承载首屏，顶部为 Dockyard 品牌与九条既有业务路由；
  最近查看内容进入可关闭的历史抽屉，不再使用固定左侧栏和常驻右检查器。
- **信息优先级**：首屏先显示 Runner、SSE、快照和活动运行证据，再显示任务、审批、
  需求与方案、交付和集成摘要。视频只提供氛围，不表达执行进度。
- **表面与色彩**：采用接近 `#f3f5f2` 的明亮画布、白色工作表面、接近
  `#111713` 的正文和接近 `#246b45` 的森林绿强调色。成功、警告、失败、阻塞、
  未知和焦点仍使用可区分的独立语义色。
- **排版**：Schibsted Grotesk、Inter、Noto Sans、Fustat 可变字体及其 OFL
  许可随前端本地打包；左上角文字字标独立使用 Quicksand SemiBold。系统字体继续
  作为缺字回退，中文操作文本不能因展示字体缺字而退化。
- **动效**：视频按合同使用 JavaScript `requestAnimationFrame` 淡入淡出；不使用
  CSS opacity transition，不把装饰动画映射成 Runner 或任务状态。
- **视频边界**：批准的 CloudFront 视频或其本地等同副本可以作为背景。加载、自动
  播放或网络失败时显示静态后备，且任何媒体请求都不能携带项目或运行数据。

参考提示词中的 `Logoipsum`、英文营销导航、注册、登录、credits、Upgrade、GPT-4o、
Attach、Voice、Prompts 和通用问答框都不是 Dockyard 功能，不实现也不显示。输入与
操作区只能调用现有 Dockyard 路由和冻结 Control API。

## 保留约束

- 九条中文路由的 ID、路径、顺序和业务能力保持不变。
- 简体中文、loopback-only、安全投影、CAS 二次确认和 fail-closed 规则保持不变。
- 移动端继续禁止任务删除、项目移除、返修派发和复杂计划/依赖编辑。
- 不展示 API Key、原始 prompt、stdout/stderr、会话标识或绝对路径。
- 不新增账户、计费、附件、语音、提示词库、云端中继或任意终端。

## 被替代的方向

TC-13.29h 的 **Harbor Cyan** 深黑蓝三栏布局已被本次用户批准的 Waypoint Light
视觉方向替代。它仍可作为历史设计记录，但不再是 AppShell 或 token 的测试基线。
