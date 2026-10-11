# Decisions
## 2026-10-11 · 用 filter.command_group 实现 /炒股 子命令集，而非手动解析 message_str

AstrBot 的 command_group 装饰器原生支持子命令树、自动参数解析（a: int 自动转）、自动渲染帮助、指令别名（alias=）。手动解析 message_str 需要自己处理所有边界，还拿不到自动帮助树。改用 command_group。

Rejected: 手动 filter.command("炒股") + 解析 event.message_str 子命令：灵活但需自己处理参数类型/帮助/未知命令，且重复造 AstrBot 已有的轮子

- files: main.py

## 2026-10-11 · 挂单必须冻结资金/股份，并以"虚拟交易日"驱动日终清算

首版只在成交时扣钱，未成交挂单不冻结 → 玩家可无限挂单，对手成交时 cash 变负。改为：买单未成交部分冻结 freeze_estimate(price,qty)=price*qty*1.001+最低佣金*4（缓冲覆盖分笔最低佣金），卖单未成交部分从 sellable 预扣股份；成交消耗冻结、撤单/过期释放、全部成交时退回冻结余量（否则永久泄漏）。同时 247 演示模式必须有"日"，否则 T+1 永远不解锁：用 CFG.day_seconds=1200 触发 DayCloseService.run()（委托过期解冻 → T+1 解锁 → 昨收更新 → 可取资金结算）；realtime 模式按真实闭市+meta last_close_date 当日只清算一次。

Rejected: 只在成交时结算、不冻结：实现简单但可被无限挂单刷爆（cash 变负、超卖同一批股份），否决。用真实自然日做 T+1：247 演示模式下没人愿意等一天，游戏不可玩，否决。

- files: game/trade.py, game/dayclose.py, storage/db.py, config.py

## 2026-10-11 · E1 市场取向：刻意比现实更凶险 + 撤掉隐性保护（大部分完全随机）

用户要求"股市要和现实一样凶险，而且大部分完全随机"。落地：个股冲击改 t 分布(dof=3) 肥尾；大盘黑天鹅/狂欢跳空(crash_prob 2e-4/size 0.010–0.025；melt 1.5e-4/0.008–0.020，jump_decay 0.6 → 累积≈幅度/0.4)；个股暴雷/暴涨(stock_jump_prob 3e-5，65% 偏负)；均值回归压到 5e-5（撤掉"跌多了会回来"的保护）；虚拟主力(吸筹/拉升/派发/砸盘)制造个股级急跌急涨，状态不外露只给"主力净流入"；情绪指数 0–100 反作用于大盘但强度仅 8e-5（不做成稳赚信号）。实测(vol_probe，30 日 × 6 种子)：主板日波动 2.9–3.2%(真实 1.7%)、创业板 3.7%、科创板 4.2%，峰度 4.8，最差单日主板 −10.5%/创业板 −16.6%，30 日最大回撤均值 −13% 最深 −25%；市场因子零均值(engine.decomp 逐项分解验证)，6 种子 30 日累计 −11%~+10%。

Rejected: 保持与真实 A 股等波动(主板 1.7%)、不加跳空与暴雷：更"写实"但不够凶险，且玩家满仓死拿的期望收益接近 0，缺少破产压力，与用户明确要求不符，否决

- files: config.py, market/engine.py

## 2026-10-11 · K 线图片用 Pillow 手绘，不用 matplotlib / AstrBot 文转图

用户要求 K 线能渲染图片。三个候选：matplotlib（画得快但本机实测不可用，且是新增重依赖）、AstrBot 自带文转图 html_render（最漂亮但只能在 AstrBot 内跑，无法离线验证，且我对该 API 只有文档印象）、Pillow 手绘（本机 bundled python 实测有 Pillow 12.3.0，AstrBot 自身要处理图片几乎必有 Pillow）。选 Pillow：零新增重依赖、能离线生成并逐像素验证（已用 read_image 肉眼确认日 K 与分时两张图）、缺库时可回退文本 sparkline。取舍：图表样式不如 matplotlib 精致，K 线/均线/成交量/右轴刻度都要自己算坐标。

Rejected: matplotlib：本机没装、要拉重依赖，且 AstrBot 环境是否装不确定，否决。AstrBot html_render 文转图：样式最好但把"能不能出图"绑死在运行时，离线无法验证，作为后续可选增强而非主路径

- files: render/chart.py, main.py, requirements.txt

## 2026-10-11 · IPO 模块（game/ipo.py）实现方案：发行→申购冻结→摇号上市，供不应求自定中签率

P2 打新模块按真实流程三阶段实现：publish 只登记发行信息（申购期不可交易）；subscribe 把 quantity×发行价 从 cash_available 扣进 cash_frozen（复用挂单冻结池，与 dayclose 的订单过期解冻互不干扰，因为申购单不在 orders 表）；draw 按"每签独立抽签"分配股票并退款——中签率不设配置，用 发行总量/总申购股数 自算（模拟真实供不应求；申购不足则全员中签）。抽签后即刻上市：股票登记进 STOCK_POOL + 行情引擎，首日涨跌幅按 DESIGN §2（主板 ±44% 对称简化、双创北交所前 5 日无限制，由 on_day_close 到期解除 pct_override）。中签股 sellable=0（沿用 T+1）。跨文件新增均为追加式：config.py 加 IPO 配置节、storage/db.py 加 ipos/ipo_subscriptions 两表与方法节、market/engine.py 加 register() 公开方法（行情状态登记的合法入口，避免 ipo.py 直接改引擎私有状态）。restore() 供 main.py 启动时重登记已上市股票（prices 表有价、STOCK_POOL 没有，否则 engine.limit_counts 会 KeyError）。

Rejected: 中签率固定配置值（可预测但不模拟真实供需，弃）；申购冻结资金另立账户字段/表（多一套账本，弃）；ipo.py 直接改 engine._raw/anchor/main_force 等私有状态（破坏封装，弃，改为 engine.register() 公开方法）；上市与摇号拆成两步（多一个中间态和触发点，弃，draw 原子完成摇号+缴款+上市）

- files: game/ipo.py, storage/db.py, market/engine.py, config.py

## 2026-10-11 · K 线图视觉质量缺陷定位与重构方向

用户看了 _demo_kline.png / _demo_intraday.png 后评价「感觉不太好看」。定位到 8 项具体缺陷，其中 7 项视觉、1 项功能：
1. 价格刻度画在 x1+6 而 PAD_R=74 留宽不够 → 刻度越界被裁
2. chart.py:144-145 成交量标题与最高量柱重叠（vtop-4）
3. chart.py:214-217 图例「价格/均价」与标题同水平带且未左对齐绘图区
4. chart.py:128 MA 图例按 62*index 硬编码横移，窗口 >3 即重叠
5. 无昨收虚线/最新价高亮/涨跌停参考线，缺视觉锚点
6. chart.py:106-107 价格刻度不取整，出现 12.9734
7. 蜡烛体 body_w=step*0.62 在 n 大时细到 1.5px 下 + width=1 → 图发虚
8. **功能性 bug**：chart.py:187 prev_close = closes[0] 把首笔价当昨收，分时涨跌着色与居中基准错误。修复需给 render_intraday 增加可选 prev_close 参数（默认 None 保持 main.py:480-482 兼容），并由 main.py 传入真实昨收（Bar dataclass 见 market/engine.py:64-72，无 prev_close 字段，需另找来源）
契约不变：available()、render_kline/render_intraday 对 main.py 暴露的签名与返回（Path|None、空 bars 返 None、无 Pillow 返 None）。



## 2026-10-11 · 改用 AgentTeams 插件编排，新建 p3 团队接手在跑工作

用户要求改用 AgentTeams 插件来编排（m00284「为什么不用子代理团队那个插件来做，我觉得很好啊」）。原因：旧 .agent-teams/p2-implementation/team.json 的 SSID 属于上一个 session，本 session 的 agent_teams_status 报「you do not lead or belong to any active team yet」，无法恢复；且该 team.json 现在位于仓库根 .agent-teams/ 而非 .dsh-code/.agent-teams/。已向用户确认：新建团队接手当前在跑的 3 个裸子代理工作，旧 p2 团队不恢复。



## 2026-10-11 · 用 AgentTeams 团队编排 K 线视觉 + IPO/margin 接线

AgentTeams 团队 p3-visual-and-wiring 已建并 running（8 任务 DAG：t1 req-chart、t2 req-wiring、t3 audit-dayclose、t4 wiring-check、t5 chart-fix、t6 wiring-fix、t7 test-write、t8 review-1）。t3 已完成。t1/t2/t4 进行中。captain 侧已把 chart 的 8 项缺陷验收依据发给 chart_impl，把 wiring_check 的断言清单与「测试先写、目标契约」要求发给 test_impl，并已把 dayclose 审计 5 条结论 + 统一接线顺序发给 wiring_impl。旧 3 个裸子代理已全部停掉（be7bc147 interrupt，其余无 closing message）。captain 后续只监控、解阻塞、转审查、合并。



