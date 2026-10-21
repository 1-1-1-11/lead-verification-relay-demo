## 摘要

<!-- 这个 PR 改变了什么行为？ -->

## 如何验证

```bash
ruff check src tests
ruff format --check src tests
pytest -q
```

## 清单

- [ ] 没有新增密钥、令牌、真实客户名、邮箱、电话或本机绝对路径
- [ ] 环境变量示例只写在 `.env.example`，值为占位符
- [ ] 用户可见的行为变化已写入 `CHANGELOG.md`
- [ ] 校验、暂停或写回路径有对应测试
