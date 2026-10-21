# 安全策略

## 支持范围

只处理 `main` 上的最新代码。这是一个没有认证的本地演示，用来说明领取、校验和暂停，不是可以接真实询盘的服务。

## 使用时不要

- 不要把进程暴露到公网。`python -m relay_demo` 默认只监听 `127.0.0.1`。
- 不要写入真实客户、邮箱、电话或表格链接。种子数据是虚构的。
- 不要把 `.env`、事件日志或含内部地址的截图提交进仓库。
- 不要在公开 issue、PR 或讨论里粘贴密钥、令牌、表格 ID 或个人数据。

## 报告漏洞

请使用 GitHub 的私下漏洞报告：

[打开 Security Advisory](https://github.com/1-1-1-11/lead-verification-relay-demo/security/advisories/new)

如果该入口不可用，仓库维护者需要先在 GitHub 仓库的 Settings → Advanced Security 中打开 Private vulnerability reporting。在维护者回复之前，不要开公开 issue，也不要把利用细节发到讨论区。

本仓库没有赏金计划。收到报告后会确认，并在修复发布后说明影响范围。
