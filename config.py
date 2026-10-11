"""全局配置（P0）。所有数值集中在这一处，方便调平衡。"""

from dataclasses import dataclass, field


@dataclass
class Config:
    # ── 账户 ─────────────────────────────────────────────
    initial_cash: float = 1_000_000.0      # D4 初始资金 100 万
    currency: str = "元"

    # ── 交易规则 ─────────────────────────────────────────
    lot_size: int = 100                    # 1 手 = 100 股
    price_tick: float = 0.01               # 最小价格单位
    # 板块涨跌幅（相对昨收）
    limit_main: float = 0.10               # 主板 ±10%
    limit_growth: float = 0.20             # 创业/科创 ±20%
    limit_bse: float = 0.30                # 北交所 ±30%
    limit_st: float = 0.05                 # ST ±5%

    # ── 费用（对齐真实）───────────────────────────────────
    commission_rate: float = 0.00025       # 佣金 万2.5 双向
    commission_min: float = 5.0            # 佣金最低 5 元
    stamp_tax_rate: float = 0.0005         # 印花税 0.05% 仅卖出
    transfer_fee_rate: float = 0.00001     # 过户费 0.001% 双向

    # ── 行情引擎 ─────────────────────────────────────────
    tick_seconds: float = 3.0              # 行情推进间隔（秒）
    market_mode: str = "247"               # "realtime"=真实时段 / "247"=常开演示
    day_seconds: int = 1200                # 247 模式虚拟交易日长度（秒），到点日终清算
    mm_spread: float = 0.002               # 做市商基础价差（双边各 0.1%）
    mm_depth: int = 5000                   # 做市商每档基础挂量（股）
    impact_k: float = 0.05                 # 供需冲击系数：净买压占深度比例 → 价格漂移
    bars_per_day: int = 240                # 每虚拟交易日的 K 线条数（1 虚拟分钟 = 1 根）
    bars_keep: int = 240                   # 内存中保留的分时 K 线根数

    # ── 破产 / 重生（D6）──────────────────────────────────
    rebirth_cooldown_hours: int = 24       # 重生冷却
    rebirth_cash_factor: float = 0.5       # 重生资金打折
    rebirth_max_times: int = 3             # 终身重生上限

    # ── 治理 ─────────────────────────────────────────────
    trade_cooldown_seconds: float = 5.0    # D10 每玩家交易指令限流

    # ── 凶险度（E1：让市场像真实股市一样凶险，且大体不可预测）──────
    vol_scale: float = 1.15             # 全局波动放大系数（乘在大盘/板块因子上）
    idio_scale: float = 1.0             # 个股特质波动系数
    fat_tail_dof: int = 3               # 个股冲击用 t 分布，自由度越小尾越肥
    crash_prob: float = 0.0002          # 每 tick 大盘暴跌概率（黑天鹅）
    crash_size: tuple = (0.010, 0.025)  # 暴跌幅度区间
    melt_prob: float = 0.00015          # 每 tick 大盘暴涨概率
    melt_size: tuple = (0.008, 0.020)   # 暴涨幅度区间
    jump_decay: float = 0.6             # 跳空冲击每 tick 衰减（累积≈幅度/(1-衰减)）
    stock_jump_prob: float = 0.00003    # 单只股票每 tick 暴雷/暴涨概率
    stock_jump_neg: tuple = (0.05, 0.10)   # 暴雷幅度（偏负 → 更凶险）
    stock_jump_pos: tuple = (0.035, 0.08)  # 暴涨幅度
    mean_revert: float = 0.00005        # 均值回归强度（调低 → 更接近纯随机游走）
    sentiment_feedback: float = 0.00008  # 情绪对大盘的反作用强度
    main_force_enabled: bool = True     # 虚拟主力（吸筹/拉升/派发/砸盘）

    # ── 集合竞价 ─────────────────────────────────────────
    auction_enabled: bool = True        # 是否启用集合竞价阶段
    auction_seconds: int = 60           # 开盘集合竞价时长（虚拟交易日开头）
    closing_auction_seconds: int = 45   # 收盘集合竞价时长（日终前）
    auction_cancel_ratio: float = 0.5   # 竞价前段可撤单的比例（后段不可撤，对应真实 9:20）

    # ── 新闻与公司行为 ───────────────────────────────────
    news_market_prob: float = 0.0004    # 每 tick 大盘新闻概率
    news_sector_prob: float = 0.0008    # 每 tick 板块新闻概率
    news_stock_prob: float = 0.0025     # 每 tick 个股新闻概率
    news_max_per_day: int = 40          # 每虚拟交易日最多几条
    dividend_prob: float = 0.02         # 每只股票每日发生分红/送转的概率
    suspend_prob: float = 0.004         # 每只股票每日停牌概率
    suspend_ticks: tuple = (60, 300)    # 停牌时长（tick）
    st_prob: float = 0.01               # 弱势股被 ST 的每日概率
    un_st_prob: float = 0.02            # ST 摘帽的每日概率
    delist_price: float = 1.00          # 面值退市阈值（元）
    delist_prob: float = 0.05           # 已 ST 且低于面值时每日退市概率

    # ── 日志 ─────────────────────────────────────────────
    log_level: str = "INFO"                # DEBUG / INFO / WARNING / ERROR
    log_trades: bool = True                # 每笔成交写一行日志 + 落库（成交流水表）
    heartbeat_ticks: int = 200             # 每 N 个 tick 打一条心跳日志


CFG = Config()
