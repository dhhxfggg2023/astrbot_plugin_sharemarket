# Mistakes

## 2026-10-11 · 读取工作区外的兄弟插件目录-..-astrbot_plugin_stockgame-..-astrbot_plugin_-irk6p
- symptom: 读取工作区外的兄弟插件目录（..\astrbot_plugin_stockgame、..\astrbot_plugin_pet_market）失败：Get-ChildItem PermissionDenied / UnauthorizedAccessException（访问被拒绝）
- cause: DSH 沙箱只给工作区根目录（astrbot_plugin_sharemarket）授予读写与低完整性标签，工作区外的路径（包括同级的其它插件目录）不可读
- fix: 只能在当前工作区内探索；如需参考现有插件（stockgame/pet_market 等），需请用户手动提供文件内容或放开沙箱权限
- do not repeat: 不要用 Get-ChildItem/read 访问工作区之外的路径，会稳定 PermissionDenied；先确认路径是否在工作区内

## 2026-10-11 · db.stats-里-int-row-0-抛-keyerror-0-而同一份代码若换成真-aiosqlite-反而正常
- symptom: db.stats() 里 `int(row[0])` 抛 `KeyError: 0`，而同一份代码若换成真 aiosqlite 反而正常
- cause: 离线测试用 sqlite3 伪造 aiosqlite，行对象是 dict 子类，只支持 row["col"] 键访问；真实 aiosqlite.Row（sqlite3.Row）同时支持下标与键访问。于是"生产能跑、测试挂"——测试 shim 比驱动更严格
- fix: SQL 一律写列别名（`SELECT COUNT(*) AS n`）并用 row["n"] 取值，键访问在两种驱动下都成立
- files: storage/db.py:300
- do not repeat: 业务代码取查询结果不要用 row[0] 下标；写别名 + 键访问，否则离线测试（dict shim）与生产（sqlite3.Row）结论会不一致

## 2026-10-11 · log.setup_file_logging-无声返回-none-文件日志没启用-插件照常运行-日志里没有任何提示
- symptom: log.setup_file_logging() 无声返回 None（文件日志没启用），插件照常运行、日志里没有任何提示
- cause: 函数体包在 `except Exception: return None` 里，把真实的 `AttributeError: 'str' object has no attribute 'mkdir'`（调用方传了 str 而函数假设 Path）吞掉了；静默失败让排查只能靠二分
- fix: 异常分支打印到 stderr（`print(..., file=sys.stderr)`）并容忍 str/Path 入参（`log_dir = Path(log_dir)`）
- files: log.py:96
- do not repeat: 日志初始化这类"失败不影响主流程"的辅助函数，except 里必须留下可诊断的痕迹（stderr/日志），不要静默 return

## 2026-10-11 · 行情波动率比目标大-10-倍-主板日波动实测-6.31-应约-1.7-平均每-tick-有约-4-只股票被封在涨跌停-触-b5ut9
- symptom: 行情波动率比目标大 ~10 倍：主板日波动实测 6.31%（应约 1.7%），平均每 tick 有约 4 只股票被封在涨跌停（触及涨跌停的 tick 占比 191%），等权大盘日波动 4.98%（应约 1.2%）
- cause: 把 AR(1) 持续因子的 n 期累积方差按 iid 的 n·σ² 折算（即 σ_tick = 日目标/√n）。持续因子的真实累积方差是 σ_u²·[n/(1-φ)² − 2φ(1-φⁿ)/(1-φ)³]，φ=0.9、n=400 时约 38200·σ_u²（≈195²），而 n·σ_u² 只有 400 → 少除了 √(38200/400)≈9.8 倍
- fix: engine.PriceEngine._factor_sigmas(target_daily, phi, noise_share) 用精确累积因子 accum=sqrt(n/(1-φ)² − 2φ(1-φⁿ)/(1-φ)³)，σ_ar = 日目标·√(1-noise_share²)/accum、σ_noise = 日目标·noise_share/√n；并把该函数真正接进 tick()（第一次只写了函数没接线，实测无变化才发现）。修后实测：主板 1.70%、创业板 2.19%、科创板 2.53%、北交所 3.19%、等权大盘 1.32%、涨跌停触及占比 1.20%
- files: market/engine.py:74-88
- trigger: gauss\(0,\s*(sig|sigma)
- do not repeat: 给随机过程标定波动率时，先算该过程的"n 期累积方差"再折算每步 σ，别默认按 √n 线性累积；持续性越强（φ→1）放大越狠。改完必须重跑 .dsh-code/vol_probe.py 看实测数字

## 2026-10-11 · 低价股波动率被系统性抹平-5-元股票实测日波动-1.35-而-50-元股票-3.91-同类参数下应接近
- symptom: 低价股波动率被系统性抹平：≤5 元股票实测日波动 1.35%，而 ≥50 元股票 3.91%（同类参数下应接近）
- cause: 每 tick 把价格 round() 到 0.01 后再作为下一 tick 的基准：1.80 元股票的理论每 tick 波动约 0.002 元 &lt; 一个最小变动单位 0.01，取整后大部分 tick 变成"零变动"，长期累积使实现波动远低于模型目标
- fix: 引擎内部保留全精度价格 self._raw[code]（累乘收益、只做涨跌停钳制），展示价 Quote.price 才 round(raw,2)；修后 ≤5 元 1.95% vs ≥50 元 2.10%，量化失真消失
- files: market/engine.py:130-146
- do not repeat: 价格精度取整只用于展示与委托价，不要拿取整后的值继续做随机过程的状态变量；低价标的会被网格吃掉波动

## 2026-10-11 · 用单-tick-收益检验板块联动时-同板块相关性只有-0.298-跨板块-0.180-差-0.118-看不出-显著联动--8x9vq
- symptom: 用单 tick 收益检验板块联动时，同板块相关性只有 0.298、跨板块 0.180（差 0.118，看不出"显著联动"）；而同一份数据按 25 tick 聚合后是 0.778 vs 0.523
- cause: 每 tick 理论波动（约 1e-4 相对量）远小于 0.01 价格网格，单 tick 收益里绝大部分是取整量化噪声；量化噪声是个股独立的，会把真实的共同因子相关性稀释掉
- fix: 衡量联动/相关性一律用多 tick（此处 25 tick）聚合收益，别用单 tick 收益；.dsh-code/sim_test.py 第 14 节与 vol_probe.py 都按聚合口径
- files: .dsh-code/sim_test.py:355-380
- do not repeat: 在有最小价格变动单位（tick size）的模型里，不要用最小时间粒度的收益做相关性/波动率检验——先聚合成有意义的时间窗口再看

## 2026-10-11 · python-.dsh-code-sim_test.py-报-nameerror-name-math-is-not-de-1dokw
- symptom: `python .dsh-code/sim_test.py` 报 `NameError: name 'math' is not defined`（第 14 节新增的相关性计算用了 math.sqrt 但没 import math），测试脚本在第 63 项断言前直接崩掉
- cause: .dsh-code/ 下的测试脚本不在我平时那条 py_compile 文件清单里（且子进程不能往 .dsh-code 写 __pycache__），所以它们的语法/名字错误只有运行时才暴露
- fix: .dsh-code/doc_check.py 增加"源码语法检查"节：用 ast.parse 只读扫描插件内全部 .py（含 .dsh-code 脚本），一次跑出所有语法错误
- files: .dsh-code/sim_test.py
- do not repeat: 改完任何 .dsh-code 脚本后先跑 `python .dsh-code/doc_check.py`（ast.parse 全量语法检查），别只依赖 py_compile 的那份文件清单

## 2026-10-11 · 新增-k-线落库后-.dsh-code-sim_test.py-在日终清算处崩-attributeerror-_fake-m0694
- symptom: 新增 K 线落库后，`.dsh-code/sim_test.py` 在日终清算处崩：`AttributeError: '_FakeConn' object has no attribute 'executemany'`（21 个源文件语法检查全过，问题只在离线 shim）
- cause: 离线测试用 sqlite3 伪造 aiosqlite，shim 只实现了 execute/fetchone/fetchall/commit/close；业务层一旦用了真驱动有而 shim 没有的方法（executemany），测试就会以"像是自己的 bug"的方式失败。上一轮 row[0] 的 KeyError 是同一类问题的反面（shim 比驱动更严）
- fix: 给 _FakeConn 补 `async def executemany(self, sql, seq)`；新增任何 db 调用后先确认 shim 有同名方法
- files: .dsh-code/sim_test.py
- do not repeat: 往 storage/db.py 加新连接方法（executemany/executescript 等）时，同步检查 .dsh-code/sim_test.py 的 _FakeConn 是否实现；否则离线测试会误报为业务 bug

## 2026-10-11 · .dsh-code-kline_demo.py-末尾-typeerror-unsupported-format-stri-zdl63
- symptom: `.dsh-code/kline_demo.py` 末尾 `TypeError: unsupported format string passed to NoneType.__format__` —— `rsi(dc, 14)` 在只有 12 天样本时返回 None，而演示脚本直接 `:.1f` 格式化
- cause: 演示脚本为了"展示玩家看到的样子"重新实现了一遍 main.py 的渲染格式，但没照抄 handler 里的 None 安全处理（`self._fmt`）；指标在样本不足时返回 None 是正常契约
- fix: 演示脚本照抄 handler 的 `_fmt`（None → "—"）；生产 handler 本来就没这个问题
- files: .dsh-code/kline_demo.py
- do not repeat: 探针/演示脚本不要重新实现一遍展示层格式：能复用就复用，必须重写时把 None 分支一并抄过来（MA/RSI/MACD 在样本不足时都返回 None）

## 2026-10-11 · 集合竞价测试里-trade.buy-...-none-市价单-被拒-tradeerror-集合竞价阶段只能限价委托-请带-onzd6
- symptom: 集合竞价测试里 `trade.buy(..., None)`（市价单）被拒：`TradeError: 集合竞价阶段只能限价委托，请带上价格`，整个第 18 节跑不起来
- cause: 测试自己把全局阶段切到"开盘集合竞价"之后才去做建仓准备，而建仓用的是市价单——被产品规则正确拒绝（这其实是规则生效的证据）
- fix: 把"建仓/准备数据"放在切换阶段之前：先 `CFG.auction_enabled=False` 建仓，再开启并 `SESSION.reset()` 进入竞价
- files: .dsh-code/sim_test.py
- do not repeat: 测试里凡是要切换全局状态（阶段/开关）的，准备动作一律放在切换之前；否则会把产品规则的正确拒绝误读成 bug

## 2026-10-11 · 竞价成交后账户冻结资金变成负数-测试断言-冻结清零-失败-且日志里成交量翻倍-1-笔-200-股被记成-2-笔-400--79uha
- symptom: 竞价成交后账户冻结资金变成负数（测试断言"冻结清零"失败），且日志里成交量翻倍（1 笔 200 股被记成 2 笔 400 股）
- cause: 两处：① `settle_auction_fill` 里先用"账户级 frozen"扣了成交款，随后又按"订单级剩余需要多少"去重算并释放一次，同一个冻结被释放两遍 → frozen 变负；② `AuctionBook.match()` 返回买卖两侧各一条成交记录，`AuctionService.run` 对两侧都累加，成交量/笔数翻倍
- fix: ① 订单级冻结随成交递减：`left = max(order.frozen - consumed, 0)`，全部成交时再退回剩余缓冲（与连续竞价路径一致），绝不用账户级总计去重算；② 统计只累加买侧记录
- files: game/trade.py:330-360
- do not repeat: 资金冻结要区分"账户级合计"与"订单级占用"两个数：成交时按订单级递减、撤单/全部成交时释放订单级余额；把两者混成一个数重算必然双重释放

## 2026-10-11 · get-location-git-status---short-fatal-not-a-git-repository-o-spoy5
- symptom: Get-Location; git status --short: fatal: not a git repository (or any of the parent directories): .git
- cause: 本工作区是 AstrBot 插件目录，目前无 Git 仓库元数据。
- fix: 使用现有项目账本和 AgentTeams 任务记录；本次不初始化 Git。
- do not repeat: 不要假设该目录有 Git 历史，也不要为快照执行 reset/stash/commit；保留现有文件，通过任务与文件检查记录基线。

## 2026-10-11 · 净资产翻倍-借-30-万后净资产显示-200-万
- symptom: 净资产翻倍（借 30 万后净资产显示 200 万）
- cause: storage/db.py:138 create_account 把 cash_available 与 cash_withdrawable 同设为 initial_cash；withdrawable 是日终快照列（settle_withdrawable 拷贝 available），不是独立资金池。margin._assets 把三本账相加 → 初始资金双算。
- fix: _assets 现金口径改为 available + frozen（与 account.bankruptcy_level 一致）
- files: game/margin.py:89
- do not repeat: 现金口径只算 available+frozen，withdrawable 只是取现额度快照列，任何加总都不得计入

## 2026-10-11 · attributeerror-nonetype-object-has-no-attribute-execute-db.p-wakcm
- symptom: AttributeError: 'NoneType' object has no attribute 'execute'（db.py:134 get_account）
- cause: 重启测试段 db 换了新 Database 对象，但 acct = AccountService(db) 仍在函数早前绑定旧已 close 的 db（conn=None）
- fix: 重启段重建所有服务对象（acct = AccountService(db) 等）
- files: .dsh-code/margin_check.py:139
- do not repeat: db 重启测试里重建 db 后必须同时重建绑定 db 的服务对象；旧服务的 self.db.conn 是 None

## 2026-10-11 · 历史112断言全绿未覆盖已接入main.py的公司行为-game-corporate.py-调用不存在的-engine.-t15gp
- symptom: 历史112断言全绿未覆盖已接入main.py的公司行为：game/corporate.py 调用不存在的 engine.adjust_price，且 suspend(..., resume_gap=gap) 与 engine.suspend(code,ticks,reason) 签名不符。main.py定义_maybe_auction_clear但_tick_loop未调用。
- cause: 此前接线只复用了service模拟验证，未驱动真实ShareMarket入口及新公司行为分支。静态签名不一致已确认，真实运行故障尚未复现。
- fix: 交后续集成任务修复并补入口级确定性回归，当前未修复。
- files: main.py:155-227,game/corporate.py:53,game/corporate.py:91-112,market/engine.py:406
- do not repeat: 不得将sim_test.py通过升级为main.py/公司行为/真实AstrBot已通过；必须覆盖真实入口调用链与强制事件分支后再下结论。

## 2026-10-11 · 独立审查-t3-.dsh-code-margin_probe_t3.py-可复现-n1-n10-发现-game-marg-0vqar
- symptom: 独立审查（t3，.dsh-code/margin_probe_t3.py 可复现 N1-N10）发现 game/margin.py 融资融券实现 5 Critical：C1 现金更新与 meta 账本非同一事务(可凭空印钞/重复扣息)、C2 NaN 入账(-299900 负现金)、C3 坏账本结构缺键→setdefault 凭空销账、C4 强平绕过停牌/跌停、C5 强平不撤卖单→幽灵卖单空卖+凭空增发股票
- cause: db.update_cash_full 每方法独立 commit（db.py:154-161），与 meta ledger _save 分离，无回滚；_load 只捕 JSONDecodeError 不校验 schema；所有入口缺 math.isfinite；强平走 delete_position 不撤挂单；_settle_sell_maker pos=None 裸 return 仍由 _apply_buy_fill 发股
- fix: 交 t4 统一集成任务修复，并纳入 I1-I6：计息补日、榜单扣负债、L3/L4 归属、dayclose 接线+清账命令、config 键、README/DESIGN 阶段标签一致
- files: game/margin.py:204-238,game/margin.py:323-354,game/margin.py:406-447,game/margin.py:471-579,game/margin.py:76-91,game/leaderboard.py:22-28,game/account.py:54-83,storage/db.py:154-161,storage/db.py:174-184,game/trade.py:360-362,game/trade.py:410-420
- do not repeat: 融资账本与现金必须同 SQLite 事务提交；所有金额入口 math.isfinite+负值校验；meta ledger load 必须 schema 校验且绝不对已存在值默认建空账本；强平/清仓前先撤 DB+撮合簿卖单；持仓不足时使成交失败而非静默 return

## 2026-10-11 · ipo_check.py-第-7-节崩溃重放断言-5-个-fail-inflight-中断模拟没生效-走了正常抽签两人全-672zc
- symptom: ipo_check.py 第 7 节崩溃重放断言 5 个 FAIL：inflight 中断模拟没生效（走了正常抽签两人全中）
- cause: 测试按假设的发行顺序硬编码 IPO 数字 id（如 _subs_of(ledger, 5) 猜 IPO-D 是第 5 个发行），实际按代码段执行顺序发布 id 错位（D 实际 id=4/5 之争），模拟操作打到别的 IPO 上，重放分支静默失效
- fix: 一律用 ipo._ipo_of(ledger, code)["id"] 按股票代码查 id，绝不硬编码或按段落顺序推断
- files: .dsh-code/ipo_check.py
- do not repeat: ipo_check.py 第 7 节崩溃重放断言 5 个 FAIL：inflight 中断模拟没生效（走了正常抽签两人全中）

## 2026-10-11 · margin_check.py-margin_probe_t3.py-在事务回滚路径崩-attributeerror-_-848f5
- symptom: margin_check.py / margin_probe_t3.py 在事务回滚路径崩：AttributeError: '_FakeConn' object has no attribute 'rollback'，看起来像业务代码 bug
- cause: margin/ipo 现金与 meta 账本同事务写入后，失败回滚路径调 self.conn.rollback()，而离线测试的伪造连接只实现 commit/close，没实现 rollback
- fix: 在 .dsh-code/sim_test.py 的 _FakeConn 里补 async def rollback() -> self._c.rollback()，与 aiosqlite API 对齐
- trigger: rollback\(
- do not repeat: margin_check.py / margin_probe_t3.py 在事务回滚路径崩：AttributeError: '_FakeConn' object has no attribute 'rollback'，看起来像业务代码 bug

## 2026-10-11 · python-.dsh-code-sim_test.py-在-db.open-就崩-recursionerror-max-08jsb
- symptom: python .dsh-code/sim_test.py 在 db.open() 就崩：RecursionError: maximum recursion depth exceeded（_commit 递归自调用 987 层），112 个断言全未执行
- cause: taskboard 记录 t4 引入 db.transaction()；_commit() 的判断写成 if not self._in_txn: await self._commit()（自调用），正确应为 await self.conn.commit()
- fix: storage/db.py:155-158 改为 await self.conn.commit()；并在 .dsh-code/sim_test.py 的 _FakeConn 补 async def rollback()（失败回滚路径需要）
- trigger: _commit\(
- do not repeat: python .dsh-code/sim_test.py 在 db.open() 就崩：RecursionError: maximum recursion depth exceeded（_commit 递归自调用 987 层），112 个断言全未执行

## 2026-10-11 · margin_check.py-重启后负债仍-3300000-debt-0.0-重启后现金不丢-失败-所有-async--ukx0o
- symptom: margin_check.py「重启后负债仍=3300000」debt=0.0、「重启后现金不丢」失败——所有 async with db.transaction() 的写（融资借款、还款、还券）在失败即回滚/连接关闭即丢失之间从未 commit
- cause: _in_txn 为 True 时 _commit() 是 design 上的 no-op，transaction() 的提交退化成空操作；数据只在连接级可见未被 commit（aiosqlite Connection 未开 isolation_level=None，sqlite3 隐式事务），下次 rollback/close 即丢
- fix: transaction() 改为 try yield / except rollback+raise / else await self.conn.commit()；_commit() 保持原语义（只负责事务外的立即提交）
- files: storage/db.py:134-159
- trigger: _in_txn
- do not repeat: margin_check.py「重启后负债仍=3300000」debt=0.0、「重启后现金不丢」失败——所有 async with db.transaction() 的写（融资借款、还款、还券）在失败即回滚/连接关闭即丢失之间从未 commit

## 2026-10-11 · team-成员输出一条与代码实际不符的审计结论-声称-trade.py-_sell-有-engine.can_trade-6rmav
- symptom: team 成员输出一条与代码实际不符的审计结论（声称 trade.py:_sell 有 engine.can_trade() 拦截停牌，实际 game/trade.py:126-161 全文无该校验）
- cause: team 成员未亲自读代码验证，直接推测或复述了不准确的结论
- fix: team 成员的审计结论在被 captain 采纳前必须用 read 复核关键行号，不能直接当 output 记录；t3 已在 captain 指导下修正
- do not repeat: team 成员输出一条与代码实际不符的审计结论（声称 trade.py:_sell 有 engine.can_trade() 拦截停牌，实际 game/trade.py:126-161 全文无该校验）

## 2026-10-11 · git-status---short-报-fatal-not-a-git-repository
- symptom: git status --short 报 fatal: not a git repository
- cause: 本插件目录没有 git 仓库（不是 not-a-repo-with-parent 的问题，就是整个插件目录无 .git）
- fix: 不依赖 git，用文件内容比对 + team review 汇总 diff 来发现并行改动冲突
- do not repeat: git status --short 报 fatal: not a git repository
- recurred: 7
- last: 2026-10-11
