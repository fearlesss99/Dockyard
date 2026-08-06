# Dockyard Web

Dockyard 是 AgentDesk 的中文深色本地控制台。TC-13.29h 只建立设计系统、
九页路由、API 类型与契约 fixture；所有写命令保持不可用。

```powershell
npm.cmd install
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
```

本应用不会读取 API Key、原始 prompt、stdout/stderr、runtime route、会话 ID
或绝对 worktree 路径。
