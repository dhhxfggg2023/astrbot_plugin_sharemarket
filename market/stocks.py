"""股票池定义（P1：约 78 只，10 个板块，虚构公司）。

板块（sector）是价格联动与板块指数的最小单位；industry 只用于展示。
"""

from dataclasses import dataclass

# 板块常量
BOARD_MAIN = "主板"
BOARD_GEM = "创业板"      # 300
BOARD_STAR = "科创板"     # 688
BOARD_BSE = "北交所"      # 8xx/43x

# 十大板块
SEC_FIN = "金融"
SEC_CONSUMER = "消费"
SEC_PHARMA = "医药"
SEC_TECH = "科技"
SEC_NEWENERGY = "新能源"
SEC_CYCLE = "周期"
SEC_INFRA = "基建地产"
SEC_UTILITY = "运输公用"
SEC_AUTO = "汽车军工"
SEC_AGRI = "农业传媒"

SECTORS = [SEC_FIN, SEC_CONSUMER, SEC_PHARMA, SEC_TECH, SEC_NEWENERGY,
           SEC_CYCLE, SEC_INFRA, SEC_UTILITY, SEC_AUTO, SEC_AGRI]


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    board: str
    sector: str          # 板块（联动单位）
    industry: str        # 细分行业（展示用）
    initial_price: float
    volatility: float    # 每 tick 波动率 σ（已按 tick 尺度折算）
    beta: float          # 对大盘敏感度
    sector_beta: float = 0.8   # 对所属板块的敏感度
    is_st: bool = False

    @property
    def limit_pct(self) -> float:
        from ..config import CFG
        if self.is_st:
            return CFG.limit_st
        if self.board in (BOARD_GEM, BOARD_STAR):
            return CFG.limit_growth
        if self.board == BOARD_BSE:
            return CFG.limit_bse
        return CFG.limit_main


# (code, name, board, sector, industry, price, volatility, beta[, sector_beta][, is_st])
_ROWS: list[tuple] = [
    # ── 金融（7）──────────────────────────────────────────
    ("600003", "东方银行", BOARD_MAIN, SEC_FIN, "银行", 8.80, 0.003, 0.7),
    ("000001", "平安发展", BOARD_MAIN, SEC_FIN, "银行", 11.20, 0.004, 0.8),
    ("300003", "东方财富", BOARD_GEM, SEC_FIN, "证券", 15.80, 0.011, 1.4, 1.3),
    ("601003", "华泰人寿", BOARD_MAIN, SEC_FIN, "保险", 32.40, 0.005, 0.9),
    ("000004", "招商证券", BOARD_MAIN, SEC_FIN, "证券", 18.60, 0.006, 1.2, 1.2),
    ("601010", "深蓝银行", BOARD_MAIN, SEC_FIN, "银行", 7.35, 0.003, 0.7),
    ("300007", "东方证券", BOARD_GEM, SEC_FIN, "证券", 14.20, 0.008, 1.3, 1.2),

    # ── 消费（8）─────────────────────────────────────────
    ("600004", "白云酒业", BOARD_MAIN, SEC_CONSUMER, "白酒", 168.00, 0.008, 1.0, 0.6),
    ("603001", "晨光乳业", BOARD_MAIN, SEC_CONSUMER, "食品饮料", 34.00, 0.006, 0.8),
    ("603002", "江南纺织", BOARD_MAIN, SEC_CONSUMER, "纺织", 3.90, 0.007, 0.8),
    ("605002", "华联超市", BOARD_MAIN, SEC_CONSUMER, "零售", 12.60, 0.005, 0.7),
    ("000003", "美的家电", BOARD_MAIN, SEC_CONSUMER, "家电", 58.00, 0.006, 0.9),
    ("600008", "老庙黄金", BOARD_MAIN, SEC_CONSUMER, "珠宝", 26.80, 0.005, 0.8),
    ("000005", "双汇食品", BOARD_MAIN, SEC_CONSUMER, "食品", 24.10, 0.004, 0.7),
    ("002006", "苏泊尔家电", BOARD_MAIN, SEC_CONSUMER, "家电", 45.60, 0.005, 0.9),

    # ── 医药（7）─────────────────────────────────────────
    ("300002", "爱尔眼科", BOARD_GEM, SEC_PHARMA, "医疗服务", 26.00, 0.010, 1.0),
    ("300006", "迈瑞医疗", BOARD_GEM, SEC_PHARMA, "医疗器械", 260.00, 0.009, 0.9, 0.7),
    ("688004", "百济医药", BOARD_STAR, SEC_PHARMA, "创新药", 130.00, 0.017, 1.1, 0.9),
    ("600009", "云南白药", BOARD_MAIN, SEC_PHARMA, "中药", 55.20, 0.004, 0.7),
    ("000006", "华东医药", BOARD_MAIN, SEC_PHARMA, "医药", 38.90, 0.005, 0.9),
    ("300008", "康泰生物", BOARD_GEM, SEC_PHARMA, "疫苗", 62.40, 0.010, 1.1),
    ("688005", "君实生物", BOARD_STAR, SEC_PHARMA, "创新药", 48.60, 0.013, 1.2, 0.9),

    # ── 科技（9）─────────────────────────────────────────
    ("002002", "海康安防", BOARD_MAIN, SEC_TECH, "电子", 32.00, 0.008, 1.0),
    ("002004", "立讯精密", BOARD_MAIN, SEC_TECH, "电子", 28.50, 0.009, 1.1),
    ("688002", "金山办公", BOARD_STAR, SEC_TECH, "软件", 240.00, 0.013, 1.2),
    ("300004", "汇川智造", BOARD_GEM, SEC_TECH, "自动化", 62.00, 0.012, 1.2),
    ("430001", "同辉信息", BOARD_BSE, SEC_TECH, "信息技术", 6.80, 0.024, 1.6, 1.1),
    ("300900", "ST 蓝盾", BOARD_GEM, SEC_TECH, "软件", 3.10, 0.015, 1.4, 0.8, True),
    ("000007", "中兴通讯", BOARD_MAIN, SEC_TECH, "通信", 33.50, 0.006, 1.1),
    ("688006", "中微公司", BOARD_STAR, SEC_TECH, "半导体", 128.00, 0.014, 1.4, 1.1),
    ("300009", "用友网络", BOARD_GEM, SEC_TECH, "软件", 21.40, 0.009, 1.2),

    # ── 新能源（8）───────────────────────────────────────
    ("300001", "宁德能源", BOARD_GEM, SEC_NEWENERGY, "新能源", 180.00, 0.013, 1.3, 1.2),
    ("300005", "阳光光伏", BOARD_GEM, SEC_NEWENERGY, "新能源", 88.00, 0.014, 1.3, 1.2),
    ("600010", "隆基绿能", BOARD_MAIN, SEC_NEWENERGY, "光伏", 19.80, 0.009, 1.3, 1.1),
    ("000008", "亿纬锂能", BOARD_MAIN, SEC_NEWENERGY, "锂电", 42.30, 0.010, 1.3, 1.1),
    ("300010", "阳光电源", BOARD_GEM, SEC_NEWENERGY, "光伏", 78.50, 0.011, 1.3, 1.2),
    ("300011", "天合光能", BOARD_GEM, SEC_NEWENERGY, "光伏", 26.70, 0.012, 1.4, 1.2),
    ("688007", "派能科技", BOARD_STAR, SEC_NEWENERGY, "储能", 88.20, 0.014, 1.4, 1.2),
    ("002007", "金风科技", BOARD_MAIN, SEC_NEWENERGY, "风电", 11.30, 0.008, 1.1),

    # ── 周期（10）────────────────────────────────────────
    ("600001", "钢铁联合", BOARD_MAIN, SEC_CYCLE, "钢铁", 4.20, 0.006, 0.9),
    ("600006", "恒安石化", BOARD_MAIN, SEC_CYCLE, "石油化工", 9.30, 0.007, 1.0),
    ("830001", "贝特新材", BOARD_BSE, SEC_CYCLE, "新材料", 18.00, 0.020, 1.5, 1.1),
    ("830002", "连城数控", BOARD_BSE, SEC_CYCLE, "专用设备", 9.50, 0.022, 1.5, 1.1),
    ("600011", "宝钢股份", BOARD_MAIN, SEC_CYCLE, "钢铁", 6.85, 0.005, 0.9),
    ("601004", "中国神华", BOARD_MAIN, SEC_CYCLE, "煤炭", 28.40, 0.004, 0.7),
    ("000009", "紫金矿业", BOARD_MAIN, SEC_CYCLE, "有色", 12.60, 0.007, 1.1),
    ("600012", "万华化学", BOARD_MAIN, SEC_CYCLE, "化工", 88.90, 0.007, 1.1),
    ("002008", "洛阳钼业", BOARD_MAIN, SEC_CYCLE, "有色", 5.40, 0.008, 1.2),
    ("830003", "连城新材", BOARD_BSE, SEC_CYCLE, "新材料", 12.40, 0.021, 1.5, 1.1),

    # ── 基建地产（7）─────────────────────────────────────
    ("601002", "神州建筑", BOARD_MAIN, SEC_INFRA, "建筑", 7.40, 0.006, 0.9),
    ("605001", "宏图地产", BOARD_MAIN, SEC_INFRA, "房地产", 2.80, 0.010, 1.2),
    ("000002", "万科兴业", BOARD_MAIN, SEC_INFRA, "房地产", 9.80, 0.009, 1.1),
    ("600013", "中国建筑", BOARD_MAIN, SEC_INFRA, "建筑", 5.60, 0.005, 0.8),
    ("000010", "海螺水泥", BOARD_MAIN, SEC_INFRA, "建材", 26.30, 0.005, 0.8),
    ("601005", "三一重工", BOARD_MAIN, SEC_INFRA, "工程机械", 16.80, 0.007, 1.1),
    ("002009", "东方雨虹", BOARD_MAIN, SEC_INFRA, "建材", 22.40, 0.008, 1.1),

    # ── 运输公用（8）─────────────────────────────────────
    ("600002", "长江电力", BOARD_MAIN, SEC_UTILITY, "电力", 22.50, 0.003, 0.6, 0.5),
    ("600005", "中原高速", BOARD_MAIN, SEC_UTILITY, "交通运输", 5.60, 0.004, 0.7),
    ("601001", "国航远洋", BOARD_MAIN, SEC_UTILITY, "航运", 6.10, 0.009, 1.1),
    ("002003", "顺丰物流", BOARD_MAIN, SEC_UTILITY, "物流", 41.00, 0.006, 0.8),
    ("600900", "ST 远洋", BOARD_MAIN, SEC_UTILITY, "航运", 1.80, 0.012, 1.3, 0.8, True),
    ("600014", "长江投资", BOARD_MAIN, SEC_UTILITY, "公用事业", 9.20, 0.004, 0.6),
    ("601006", "宁沪高速", BOARD_MAIN, SEC_UTILITY, "高速公路", 8.90, 0.003, 0.5),
    ("000011", "顺丰控股", BOARD_MAIN, SEC_UTILITY, "物流", 41.20, 0.006, 0.9),

    # ── 汽车军工（7）─────────────────────────────────────
    ("002001", "比亚迪车业", BOARD_MAIN, SEC_AUTO, "汽车", 210.00, 0.010, 1.2, 1.1),
    ("600015", "长城汽车", BOARD_MAIN, SEC_AUTO, "汽车", 22.60, 0.008, 1.1),
    ("000012", "长安汽车", BOARD_MAIN, SEC_AUTO, "汽车", 12.80, 0.008, 1.1),
    ("002010", "中航光电", BOARD_MAIN, SEC_AUTO, "军工", 42.30, 0.008, 1.2),
    ("600016", "中国重工", BOARD_MAIN, SEC_AUTO, "船舶", 4.85, 0.006, 1.0),
    ("300012", "先导智能", BOARD_GEM, SEC_AUTO, "锂电设备", 33.60, 0.012, 1.3, 1.1),
    ("688008", "中航无人机", BOARD_STAR, SEC_AUTO, "军工", 45.20, 0.012, 1.3, 1.0),

    # ── 农业传媒（7）─────────────────────────────────────
    ("002005", "牧原农牧", BOARD_MAIN, SEC_AGRI, "农业", 44.00, 0.009, 0.9),
    ("002900", "ST 中昌", BOARD_MAIN, SEC_AGRI, "传媒", 2.30, 0.013, 1.2, 0.8, True),
    ("600017", "牧原股份", BOARD_MAIN, SEC_AGRI, "养殖", 44.30, 0.008, 0.9),
    ("000013", "海大集团", BOARD_MAIN, SEC_AGRI, "饲料", 52.60, 0.006, 0.8),
    ("002011", "分众传媒", BOARD_MAIN, SEC_AGRI, "传媒", 6.80, 0.007, 1.0),
    ("300013", "芒果超媒", BOARD_GEM, SEC_AGRI, "传媒", 25.40, 0.010, 1.2),
    ("600018", "北大荒", BOARD_MAIN, SEC_AGRI, "农业", 12.30, 0.007, 0.9),
]


def _build_pool() -> dict[str, Stock]:
    pool: dict[str, Stock] = {}
    for row in _ROWS:
        if len(row) == 8:
            code, name, board, sector, industry, price, vol, beta = row
            sector_beta, is_st = 0.8, False
        elif len(row) == 9:
            code, name, board, sector, industry, price, vol, beta, last = row
            if isinstance(last, bool):
                sector_beta, is_st = 0.8, last
            else:
                sector_beta, is_st = float(last), False
        else:
            (code, name, board, sector, industry, price, vol, beta,
             sector_beta, is_st) = row
        if code in pool:
            raise ValueError(f"股票代码重复：{code}")
        if sector not in SECTORS:
            raise ValueError(f"{code} 的板块未登记：{sector}")
        pool[code] = Stock(code, name, board, sector, industry, price, vol, beta,
                           float(sector_beta), bool(is_st))
    return pool


STOCK_POOL: dict[str, Stock] = _build_pool()

# 行业列表（展示/搜索用）
INDUSTRIES = sorted({s.industry for s in STOCK_POOL.values()})

# 板块 -> 成分股
SECTOR_MEMBERS: dict[str, list[str]] = {
    sec: sorted(c for c, s in STOCK_POOL.items() if s.sector == sec)
    for sec in SECTORS
}
