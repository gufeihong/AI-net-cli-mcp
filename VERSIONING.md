# 版本说明

## 1.0.0 — 原始版本

原始多厂商 MCP 服务与 Web 控制台，保持兼容：

- MCP：`server.py`
- Web 控制台：`python console/app.py`
- 配置示例：`mcp-config.example.json`

## 2.0.0 — 安全增强版

在原始实现基础上增加独立的可选入口，不覆盖 1.0.0 的原有行为：

- MCP：`secure_server.py`
- Web 控制台：`python secure_console.py`
- 配置示例：`mcp-config.v2.example.json`
- 说明：`docs/security-hardening.md`

主分支保留两个版本；默认配置仍指向 1.0.0，升级时需明确切换到 2.0.0 的入口。

> 版本号是仓库内的版本说明。此提交没有创建 Git tag 或 GitHub Release。安全策略是应用层防护与启发式命令分类，不可代替设备 RBAC/AAA、人类审批和生产变更流程；部署前应在隔离实验环境充分验证。
