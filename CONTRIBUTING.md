# 贡献

这个仓库是询盘背调 Relay 的本地演示。改动应保持两件事：写回前的身份校验仍然失败即停，仓库里仍然没有真实客户数据或密钥。

## 环境

需要 Python 3.11 或 3.12。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # 可选
```

## 检查

提交前在仓库根目录运行：

```bash
ruff check src tests
ruff format src tests
pytest -q
```

GitHub Actions 会在 pull request 上跑同样的 lint、格式检查和测试。

## 拉取请求

- 从一个最新的 `main` 拉分支
- 说明行为变化，并在 `CHANGELOG.md` 的 `Unreleased` 小节记下用户可见的改动（发布时再写版本号）
- 补或改测试，覆盖你动过的校验、暂停或写回路径
- 不要提交 `.env`、`data/`、虚拟环境或编辑器目录
- 种子数据和示例里只用虚构编号（当前是 `acme-motors`、`delta-pumps`）和 `example.com`
- Issue 和 PR 里不要贴真实询盘、邮箱、电话、表格 ID、令牌或本机绝对路径

界面和注释目前是中文，issue 用中文或英文都可以。

## 安全问题

不要开公开 issue。见 [SECURITY.md](SECURITY.md)。
