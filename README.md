# ModelFailover —— 多 provider 灾备与任务语义路由网关

> 从内部提案池里挑出来的第一个**真正落地**的项目（2026-09-04 周度立项 Top 1）。

## 为什么是它

内部立项复盘写了句话：**「15 条提案全部停留在创意阶段，瓶颈不在创意质量，而在立项到开发的转化环节断了」**。
这条提案被选中的理由不是它最性感，而是：

1. **痛点是真的、而且正在发生** —— 本机一条每日流水线的分析步骤，曾因为主模型（MiniMax）超时直接失败，
   只能重试或临时换模型。这类「上游静默失效」的事件记录已经攒了十几条。
2. **纯软件、无 GPU、无外部付费依赖** —— 一个人一天能做出能跑的 MVP 并端到端验证。
3. **本机已经有多个真实 provider**（MiniMax / SiliconFlow / DeepSeek），故障转移可以**真刀真枪**地验，不用 mock。

而这次「立项 → 上线」不再停在文档：网关已经在 systemd user scope 里跑着，
并且**被每日 AI 洞察流水线真实消费**（见下面「接入」一节）。

## 它解决什么

一次 LLM 调用可能因为这些原因失败：主模型超时、模型被下线、key 配额耗尽（429）、鉴权失效、上游 5xx。
本机现状是：每条流水线各写一套 fallback 逻辑，且**大多数只处理「模型被禁用」这一种**，超时/限流直接抛异常。

ModelFailover 把这件事收敛成一个网关：

```
调用方（日报 / 任何脚本 / 任何 Agent）
        │  OpenAI 兼容 POST /v1/chat/completions
        ▼
   ┌──────────────────┐
   │  1. 任务分类      │  code / creative / analysis / general（规则可解释）
   │  2. 选路          │  强项匹配 → 通用 → 未知 → 已知挂掉（最后兜底）
   │  3. 依次尝试      │  任何错误都降级（每个 provider 的 key/配额独立）
   │  4. 审计落库      │  每次尝试一行：谁、哪个模型、多久、为什么挂
   └──────────────────┘
        │
        ├─▶ siliconflow（Kimi K2.6）   ← 主力
        ├─▶ minimax     （M3.1）      ← 灾备
        └─▶ deepseek    （chat）      ← 代码任务强项（2026-11-16 补上 key，现已 healthy）
```

## 快速开始

```bash
git clone https://github.com/OzzyLennon/modelfailover
cd modelfailover

# 1) 健康体检（真网络探针）
python3 -m mf check

# 2) 只看路由决策，不真调
python3 -m mf route "帮我写个 python 脚本，把这个函数重构一下"

# 3) 起服务
python3 -m mf serve                      # 127.0.0.1:8800
systemctl --user enable --now model-failover.service   # 生产形态

# 4) 调用（OpenAI 兼容，curl 就能用）
curl -s http://127.0.0.1:8800/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Pro/moonshotai/Kimi-K2.6","messages":[{"role":"user","content":"回复：收到"}]}'
```

响应里会带路由信息（便于排查）：

```json
"x_mf": {
  "provider": "siliconflow",
  "attempts": 1,
  "task_class": "general",
  "chain": ["#1 siliconflow[强项匹配/healthy] model=Pro/moonshotai/Kimi-K2.6", "..."],
  "failed_over": []
}
```

响应头也有：`x-mf-provider` / `x-mf-model` / `x-mf-attempts` / `x-mf-class` / `x-mf-request-id`。

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 兼容入口，自动分类 + 选路 + 降级 |
| GET | `/healthz` | 网关自身 + 各 provider 健康状态 |
| GET | `/providers` | provider 明细（协议、模型、强项、优先级、是否有 key） |
| GET | `/audit?limit=20` | 最近审计记录 |
| GET | `/stats` | 审计统计：失败率、按 provider 分布 |

## 配置（`config/providers.yaml`）

```yaml
providers:
  - name: siliconflow
    style: openai                 # openai | anthropic
    base_url: https://api.siliconflow.cn/v1
    api_key_env: [SILICONFLOW_LLM_KEY, SILICONFLOW_API_KEY]   # 按顺序找第一个能用的
    model: Pro/moonshotai/Kimi-K2.6
    strengths: [analysis, general, creative]   # 强项标签，决定语义路由
    priority: 20                  # 同层内顺序，越小越优先
```

**密钥永远不在代码里**，解析顺序：进程环境变量 → secrets 文件（默认 `~/.openclaw/secrets.env`，
可用环境变量 `MF_SECRETS_FILE` 或 `config/providers.yaml` 的 `secrets_file` 指定）→ OpenClaw 配置里的
provider 配置（`api_key_ref: openclaw:minimax_domestic`）。命令行里也不出现密钥。

## 接入（已经生效）

调用方（这里是一条每日 AI 洞察流水线）现在这样跑：

```bash
export LLM_API_URL="http://127.0.0.1:8800/v1/chat/completions"   # 走网关
export LLM_DIRECT_URL="https://api.siliconflow.cn/v1/chat/completions"  # 网关挂了直连兜底
```

调用方的 `call_llm()` 增加了「网关优先、连接失败自动直连」的兜底：
**网关是增强，不是新的单点** —— 网关进程挂了，日报照跑不误（已实测：停掉服务后调用自动走直连，2.6s 成功）。

## 审计

每次尝试都落一行 SQLite（`data/audit.db`）：

```bash
python3 -m mf audit -n 20     # 最近 20 条
python3 -m mf stats           # 失败率、按 provider 分布
```

字段：`ts / request_id / task_class / requested_model / provider / model / attempt / status / error_kind /
latency_ms / prompt_chars / response_chars / chain / usage`。

出事后要回答的三个问题都能答：**这次走了谁**、**为什么走它**（chain 里有每一步的状态与强项标签）、**耗时多少**。

## 测试与验证

```bash
python3 -m pytest tests/ -q          # 34 个测试（含一个真服务器的并发回归测试）
python3 scripts/smoke_live.py        # 真机冒烟：真 provider + 真故障转移
```

真机冒烟做三件事（**不 mock**）：

1. 健康体检 —— 真探针打所有启用的 provider；
2. **故障转移演练** —— 把首选指向 `127.0.0.1:9`（黑洞）且**假装它刚通过健康检查**（这是最常见的故障形态），
   验证网关确实自动切到备用并成功返回；
3. 单发真请求 —— 真 key、真模型。

最近一次真机结果：

```
minimax       MiniMax-M3.1              key=有 status=healthy  787ms
siliconflow   Pro/moonshotai/Kimi-K2.6  key=有 status=healthy   73ms
deepseek      deepseek-chat             key=有 status=healthy  152ms
--- 2A 故障转移演练 ---
HTTP 200  x-mf-provider=minimax  x-mf-attempts=2
失败记录: [{"provider":"blackhole","error":"network","msg":"[Errno 111] Connection refused"}]
--- 2B 健康感知路由 ---
HTTP 200  provider=minimax  attempts=1   （已知挂掉的 provider 被直接绕过）
--- 3 真实调用 ---
HTTP 200  provider=minimax  回答: 收到
```

## 部署

systemd **user scope**（`linger=yes` 开机自启）：

```bash
cp deploy/model-failover.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now model-failover.service
systemctl --user status model-failover.service
```

默认只监听 `127.0.0.1:8800`，不暴露公网。

## 设计取舍（都踩过坑才这么定）

| 决定 | 理由 |
|---|---|
| 任何上游错误都降级，包括 401/404 | 每个 provider 的 key 和配额是独立的，A 挂了不代表 B 挂了。代价最多多花 2 次调用 |
| 已知 `down` 的 provider 排最后而不是排除 | 排除会让「全部 down」时直接 503；排最后还能兜一把 |
| 模型名先做**全量精确匹配**再解析 `provider/model` | OpenAI 风格模型名自带斜杠（`Pro/moonshotai/Kimi-K2.6`），按斜杠切会把模型名截断 → 404（真机踩过） |
| 探针必须带鉴权头 | 少了会拿到 401，被误判成「provider 挂了」（真机踩过） |
| 上游调用必须丢进线程池 | 在 async 端点里调阻塞的 httpx → **一次 58 秒的长文分析期间，整个网关被冻住**，`/healthz`、`/audit` 全无响应（真机踩过）。现在用 `run_in_threadpool` |
| 回归测试必须用真 uvicorn 服务器 | Starlette 的 `TestClient` 每次请求会新起一个事件循环 → 阻塞调用冻不住第二个请求，测试会「假绿」。我把代码改回阻塞版它照样通过（真机踩过） |
| connect 与 read 超时分开设 | `connect=10s / read=300s` —— 连接要快失败（死上游立刻暴露），读要等够（长文分析要几十秒）。一刀切总超时会把「模型还在算」误判成「挂了」→ 白白降级到更差的模型 |
| 网关不可达时直连兜底 | 网关是增强，不能变成新的单点 |
| 密钥只从 env / secrets 文件 / openclaw.json 读 | 命令行里出现密钥会被工具脱敏成空值，验证结果全是假的 |

## 首跑实测（2026-11-15 23:30）

网关接进日报流水线的第一次生产运行，**一次通过**：

| 环节 | 实测 |
|---|---|
| 抓取 | 6/6 源正常（36kr 首次 ReadTimeout → 自动重试成功） |
| 增量去重 | 抓 63 条 → 新增 44 条 |
| 噪音过滤 | 丢 8 条，保留 36 条 |
| **LLM 分析** | **249s**，`url=http://127.0.0.1:8800/v1/chat/completions`，provider=siliconflow，attempts=1，3078 字符 |
| 推送 | 飞书卡片成功送达目标群 ✅ |

**249s 这件事逼出了三层超时链的对齐**（当时预算只有 300s，只剩 17% 余量）：

```
cron agent turn  1200s   ← 最外层
  └─ 调用方(日报)  450s
       └─ 网关上游读  420s
```

规则：**上层永远大于下层**，这样网关总是先回答，两边不会同时踩到 deadline 互相抢跑；
调用方超时时网关已经放弃，不会出现「调用方走了、网关还在烧钱算」的情况。

## 已知边界

- 不支持 `stream`（流式）—— 显式返回 400，不静默降级成非流式。
- 语义路由是**规则分类**（关键词打分），不是模型分类：可解释、零成本，但遇到刁钻 prompt 会分错
  （例：日报的产品分析 prompt 被判成 `code`，因为正文里全是技术名词）；
  分错的后果只是「走了一个没那么对口的 provider」，而日报那种**指定了模型**的调用会直接钉住 provider，不受影响。
- 3 个 provider 都有可用 key（MiniMax / SiliconFlow / DeepSeek）：DeepSeek 的 key 2026-11-16 从本机另一处
  已有配置收拢到统一 secrets 文件的 `DEEPSEEK_API_KEY`（密钥不进 git，值不经过命令行）；
  补之前它在 60s 一次的健康探测里连续失败 554 次（`没有 API key`）。
  备份通道：`tencent-coding`（`api_key_ref: openclaw:tencentcodingplan`）有 key 但配额已耗尽（429），默认 `enabled: false`。
- **独立 GitHub 仓库**：`OzzyLennon/modelfailover`（2026-11-16 建仓）。
  - 本机工作副本：跟着主仓做每日自动备份
  - 独立仓库副本：独立 git 历史，不受嵌套仓影响
  - 发布：同步 → 提交 → 推送 → **校验远端真的前进了**（不看 push 的退出码，看远端 SHA 有没有前进）

## 下一步（还没做）

1. 补 DeepSeek key，把「代码任务强项」这条路由真正跑起来；
2. 真实流量跑满 7 天后，用审计数据看实际失败率，再决定 `max_attempts` 和优先级；
3. 把其余几条流水线（周报 / 立项 / 宣发）也接进网关（目前只接了日报）；
4. 给 `/audit` 加一个只读页面（目前只有 JSON）。
