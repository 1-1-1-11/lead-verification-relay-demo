# lead-verification-relay-demo

询盘从表格领取，背调结果写回原行，写回前做身份核对。核对不一致时本批不写入，相关行标为异常，服务进入暂停，等人工看过再恢复。

生产系统的脱敏版：校验与暂停逻辑保留，Google Sheets / ChatGPT 网页换成本地假数据，不含客户信息和密钥。

## 怎么跑

```bash
git clone https://github.com/1-1-1-11/lead-verification-relay-demo.git
cd lead-verification-relay-demo
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn relay_demo.app:app --port 8017
```

浏览器开 <http://127.0.0.1:8017/> 看工作台。

走一遍：

```bash
# 领取一批
curl -X POST "http://127.0.0.1:8017/api/batches/claim?batch_size=3"

# 合法写回
python -m relay_demo.demo_flow

# 故意改指纹 → 409，整批拒，进入全局暂停
python -m relay_demo.demo_flow --tamper

# 人工恢复
curl -X POST http://127.0.0.1:8017/api/ops/resume
```

![询盘背调工作台](assets/dashboard.png)

## 为什么要停

领取时给每行打指纹：`customer_id + 行号 + 源行内容`。写回时核对客户、任务、行号、指纹，任一不对：

- 整批不写
- 行标成异常
- 服务进全局人工暂停，恢复前不再领新批次

能写回去的列也白名单限制，其它列代码碰不到。事件日志只记阶段和结果，不记正文、URL、PII。

## 和正式版差在哪

正式版连真实表格和 ChatGPT 网页会话，还打成 Windows 桌面端；这里用内存假表 + Mock 分析，方便你本地把校验和暂停跑通。

```text
src/relay_demo/
  models.py  store.py  verify.py  events.py  app.py  demo_flow.py
tests/test_relay.py
```
