"""股票池定义（150 只，10 个板块，虚构公司）。

板块（sector）是价格联动与板块指数的最小单位；industry 只用于展示。
每只股票拥有 6 位原码 ``code`` 与 3 位短码 ``short_code``（板块拼音首
字母 + 2 位序号，如 JR01 / XF03 / KJ05 / XNY02）。``Stock.lookup`` 同时
接受两种码。``STOCK_POOL`` 以 6 位码为 key（向后兼容），``STOCK_BY_SHORT``
以短码为 key。
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

# 板块 -> 短码前缀（拼音首字母大写）
SECTOR_SHORT_PREFIX: dict[str, str] = {
    SEC_FIN: "JR",
    SEC_CONSUMER: "XF",
    SEC_PHARMA: "YY",
    SEC_TECH: "KJ",
    SEC_NEWENERGY: "XNY",
    SEC_CYCLE: "ZQ",
    SEC_INFRA: "JC",
    SEC_UTILITY: "YS",
    SEC_AUTO: "QC",
    SEC_AGRI: "NY",
}


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
    short_code: str = ""       # 3 位短码（板块前缀+序号）；IPO 临时股留空

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

    @classmethod
    def lookup(cls, code: str) -> "Stock | None":
        """按 6 位原码或 3 位短码查找股票。"""
        if not code:
            return None
        s = STOCK_POOL.get(code)
        if s is not None:
            return s
        return STOCK_BY_SHORT.get(code)


# (code, name, board, sector, industry, price, volatility, beta[, sector_beta][, is_st])
_ROWS: list[tuple] = [
    # ── 金融（15）JR ──────────────────────────────────────
    ("600003", "东方银行", BOARD_MAIN, SEC_FIN, "银行", 8.80, 0.003, 0.7),
    ("000001", "平安发展", BOARD_MAIN, SEC_FIN, "银行", 11.20, 0.004, 0.8),
    ("300003", "东方财富", BOARD_GEM, SEC_FIN, "证券", 15.80, 0.011, 1.4, 1.3),
    ("601003", "华泰人寿", BOARD_MAIN, SEC_FIN, "保险", 32.40, 0.005, 0.9),
    ("000004", "招商证券", BOARD_MAIN, SEC_FIN, "证券", 18.60, 0.006, 1.2, 1.2),
    ("601010", "深蓝银行", BOARD_MAIN, SEC_FIN, "银行", 7.35, 0.003, 0.7),
    ("300007", "东方证券", BOARD_GEM, SEC_FIN, "证券", 14.20, 0.008, 1.3, 1.2),
    ("601111", "建设工行", BOARD_MAIN, SEC_FIN, "银行", 6.20, 0.003, 0.6),
    ("601288", "农业工行", BOARD_MAIN, SEC_FIN, "银行", 4.10, 0.003, 0.6),
    ("601318", "新中国人寿", BOARD_MAIN, SEC_FIN, "保险", 45.30, 0.005, 0.9),
    ("601398", "工商银行", BOARD_MAIN, SEC_FIN, "银行", 5.40, 0.003, 0.6),
    ("300015", "同花顺投", BOARD_GEM, SEC_FIN, "证券", 120.00, 0.014, 1.5, 1.3),
    ("601628", "中国人寿", BOARD_MAIN, SEC_FIN, "保险", 30.10, 0.005, 0.8),
    ("600999", "光大证券", BOARD_MAIN, SEC_FIN, "证券", 16.40, 0.007, 1.2, 1.2),
    ("601601", "太平洋保险", BOARD_MAIN, SEC_FIN, "保险", 28.70, 0.005, 0.8),

    # ── 消费（15）XF ─────────────────────────────────────
    ("600004", "白云酒业", BOARD_MAIN, SEC_CONSUMER, "白酒", 168.00, 0.008, 1.0, 0.6),
    ("603001", "晨光乳业", BOARD_MAIN, SEC_CONSUMER, "食品饮料", 34.00, 0.006, 0.8),
    ("603002", "江南纺织", BOARD_MAIN, SEC_CONSUMER, "纺织", 3.90, 0.007, 0.8),
    ("605002", "华联超市", BOARD_MAIN, SEC_CONSUMER, "零售", 12.60, 0.005, 0.7),
    ("000003", "美的家电", BOARD_MAIN, SEC_CONSUMER, "家电", 58.00, 0.006, 0.9),
    ("600008", "老庙黄金", BOARD_MAIN, SEC_CONSUMER, "珠宝", 26.80, 0.005, 0.8),
    ("000005", "双汇食品", BOARD_MAIN, SEC_CONSUMER, "食品", 24.10, 0.004, 0.7),
    ("002006", "苏泊尔家电", BOARD_MAIN, SEC_CONSUMER, "家电", 45.60, 0.005, 0.9),
    ("603517", "绝味食品", BOARD_MAIN, SEC_CONSUMER, "食品", 38.20, 0.006, 0.9),
    ("603288", "海天味业", BOARD_MAIN, SEC_CONSUMER, "调味品", 42.50, 0.005, 0.8),
    ("002714", "牧原食品", BOARD_MAIN, SEC_CONSUMER, "食品加工", 22.30, 0.006, 0.8),
    ("603156", "养元饮品", BOARD_MAIN, SEC_CONSUMER, "饮料", 18.40, 0.005, 0.7),
    ("600887", "伊利乳业", BOARD_MAIN, SEC_CONSUMER, "乳品", 28.90, 0.005, 0.7),
    ("603027", "千禾味业", BOARD_MAIN, SEC_CONSUMER, "调味品", 15.70, 0.007, 1.0),
    ("002507", "涪陵榨菜", BOARD_MAIN, SEC_CONSUMER, "食品", 20.10, 0.006, 0.8),

    # ── 医药（15）YY ─────────────────────────────────────
    ("300002", "爱尔眼科", BOARD_GEM, SEC_PHARMA, "医疗服务", 26.00, 0.010, 1.0),
    ("300006", "迈瑞医疗", BOARD_GEM, SEC_PHARMA, "医疗器械", 260.00, 0.009, 0.9, 0.7),
    ("688004", "百济医药", BOARD_STAR, SEC_PHARMA, "创新药", 130.00, 0.017, 1.1, 0.9),
    ("600009", "云南白药", BOARD_MAIN, SEC_PHARMA, "中药", 55.20, 0.004, 0.7),
    ("000006", "华东医药", BOARD_MAIN, SEC_PHARMA, "医药", 38.90, 0.005, 0.9),
    ("300008", "康泰生物", BOARD_GEM, SEC_PHARMA, "疫苗", 62.40, 0.010, 1.1),
    ("688005", "君实生物", BOARD_STAR, SEC_PHARMA, "创新药", 48.60, 0.013, 1.2, 0.9),
    ("000538", "片仔癀药业", BOARD_MAIN, SEC_PHARMA, "中药", 240.00, 0.008, 1.0),
    ("600276", "恒瑞医药", BOARD_MAIN, SEC_PHARMA, "创新药", 45.20, 0.006, 0.9),
    ("300760", "迈瑞生物", BOARD_GEM, SEC_PHARMA, "医疗器械", 88.00, 0.009, 1.0),
    ("688180", "信达生物", BOARD_STAR, SEC_PHARMA, "创新药", 36.80, 0.015, 1.2, 0.9),
    ("002015", "华兰生物", BOARD_MAIN, SEC_PHARMA, "生物制品", 22.40, 0.007, 0.9),
    ("300016", "北陆药业", BOARD_GEM, SEC_PHARMA, "医药", 9.60, 0.009, 1.1),
    ("600196", "复星医药", BOARD_MAIN, SEC_PHARMA, "医药", 28.70, 0.006, 0.9),
    ("688235", "百利天恒", BOARD_STAR, SEC_PHARMA, "创新药", 62.00, 0.018, 1.3, 0.9),

    # ── 科技（15）KJ ─────────────────────────────────────
    ("002002", "海康安防", BOARD_MAIN, SEC_TECH, "电子", 32.00, 0.008, 1.0),
    ("002004", "立讯精密", BOARD_MAIN, SEC_TECH, "电子", 28.50, 0.009, 1.1),
    ("688002", "金山办公", BOARD_STAR, SEC_TECH, "软件", 240.00, 0.013, 1.2),
    ("300004", "汇川智造", BOARD_GEM, SEC_TECH, "自动化", 62.00, 0.012, 1.2),
    ("430001", "同辉信息", BOARD_BSE, SEC_TECH, "信息技术", 6.80, 0.024, 1.6, 1.1),
    ("300900", "ST 蓝盾", BOARD_GEM, SEC_TECH, "软件", 3.10, 0.015, 1.4, 0.8, True),
    ("000007", "中兴通讯", BOARD_MAIN, SEC_TECH, "通信", 33.50, 0.006, 1.1),
    ("688006", "中微公司", BOARD_STAR, SEC_TECH, "半导体", 128.00, 0.014, 1.4, 1.1),
    ("300009", "用友网络", BOARD_GEM, SEC_TECH, "软件", 21.40, 0.009, 1.2),
    ("688981", "中芯国际", BOARD_STAR, SEC_TECH, "半导体", 52.30, 0.012, 1.3, 1.1),
    ("002230", "科大讯飞", BOARD_MAIN, SEC_TECH, "软件", 38.60, 0.011, 1.3),
    ("300433", "蓝思科技", BOARD_GEM, SEC_TECH, "电子", 14.80, 0.010, 1.2),
    ("002415", "大华股份", BOARD_MAIN, SEC_TECH, "电子", 18.90, 0.008, 1.1),
    ("688111", "金山云网", BOARD_STAR, SEC_TECH, "云计算", 24.50, 0.013, 1.3, 1.1),
    ("300454", "深信服科技", BOARD_GEM, SEC_TECH, "网络安全", 86.00, 0.011, 1.3),

    # ── 新能源（15）XNY ──────────────────────────────────
    ("300001", "宁德能源", BOARD_GEM, SEC_NEWENERGY, "新能源", 180.00, 0.013, 1.3, 1.2),
    ("300005", "阳光光伏", BOARD_GEM, SEC_NEWENERGY, "新能源", 88.00, 0.014, 1.3, 1.2),
    ("600010", "隆基绿能", BOARD_MAIN, SEC_NEWENERGY, "光伏", 19.80, 0.009, 1.3, 1.1),
    ("000008", "亿纬锂能", BOARD_MAIN, SEC_NEWENERGY, "锂电", 42.30, 0.010, 1.3, 1.1),
    ("300010", "阳光电源", BOARD_GEM, SEC_NEWENERGY, "光伏", 78.50, 0.011, 1.3, 1.2),
    ("300011", "天合光能", BOARD_GEM, SEC_NEWENERGY, "光伏", 26.70, 0.012, 1.4, 1.2),
    ("688007", "派能科技", BOARD_STAR, SEC_NEWENERGY, "储能", 88.20, 0.014, 1.4, 1.2),
    ("002007", "金风科技", BOARD_MAIN, SEC_NEWENERGY, "风电", 11.30, 0.008, 1.1),
    ("601012", "隆基氢能", BOARD_MAIN, SEC_NEWENERGY, "氢能", 24.60, 0.010, 1.2, 1.1),
    ("300274", "阳光储能", BOARD_GEM, SEC_NEWENERGY, "储能", 92.00, 0.013, 1.4, 1.2),
    ("688390", "固德威电", BOARD_STAR, SEC_NEWENERGY, "光伏逆变器", 180.00, 0.015, 1.4, 1.2),
    ("002594", "比亚迪新能", BOARD_MAIN, SEC_NEWENERGY, "新能源车", 245.00, 0.012, 1.3, 1.1),
    ("300014", "亿纬动力", BOARD_GEM, SEC_NEWENERGY, "锂电", 38.40, 0.011, 1.3, 1.1),
    ("600438", "通威股份", BOARD_MAIN, SEC_NEWENERGY, "光伏", 28.70, 0.009, 1.2, 1.1),
    ("688599", "天合光伏", BOARD_STAR, SEC_NEWENERGY, "光伏", 32.10, 0.013, 1.3, 1.2),

    # ── 周期（15）ZQ ─────────────────────────────────────
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
    ("601857", "中国石油", BOARD_MAIN, SEC_CYCLE, "石油石化", 8.20, 0.005, 0.8),
    ("601088", "中国神华能源", BOARD_MAIN, SEC_CYCLE, "煤炭", 32.10, 0.004, 0.7),
    ("600362", "江西铜业", BOARD_MAIN, SEC_CYCLE, "有色", 18.70, 0.008, 1.1),
    ("000898", "鞍钢股份", BOARD_MAIN, SEC_CYCLE, "钢铁", 3.80, 0.006, 0.9),
    ("600309", "万华新材", BOARD_MAIN, SEC_CYCLE, "化工", 76.00, 0.007, 1.1),

    # ── 基建地产（15）JC ─────────────────────────────────
    ("601002", "神州建筑", BOARD_MAIN, SEC_INFRA, "建筑", 7.40, 0.006, 0.9),
    ("605001", "宏图地产", BOARD_MAIN, SEC_INFRA, "房地产", 2.80, 0.010, 1.2),
    ("000002", "万科兴业", BOARD_MAIN, SEC_INFRA, "房地产", 9.80, 0.009, 1.1),
    ("600013", "中国建筑", BOARD_MAIN, SEC_INFRA, "建筑", 5.60, 0.005, 0.8),
    ("000010", "海螺水泥", BOARD_MAIN, SEC_INFRA, "建材", 26.30, 0.005, 0.8),
    ("601005", "三一重工", BOARD_MAIN, SEC_INFRA, "工程机械", 16.80, 0.007, 1.1),
    ("002009", "东方雨虹", BOARD_MAIN, SEC_INFRA, "建材", 22.40, 0.008, 1.1),
    ("601668", "中国建筑工程", BOARD_MAIN, SEC_INFRA, "建筑", 6.10, 0.005, 0.8),
    ("600048", "保利发展", BOARD_MAIN, SEC_INFRA, "房地产", 11.20, 0.009, 1.1),
    ("001979", "招商蛇口", BOARD_MAIN, SEC_INFRA, "房地产", 14.60, 0.008, 1.0),
    ("600585", "海螺建材", BOARD_MAIN, SEC_INFRA, "建材", 24.80, 0.005, 0.8),
    ("601390", "中国中铁", BOARD_MAIN, SEC_INFRA, "建筑", 7.90, 0.005, 0.8),
    ("000651", "格力地产", BOARD_MAIN, SEC_INFRA, "房地产", 4.30, 0.009, 1.1),
    ("600031", "三一装备", BOARD_MAIN, SEC_INFRA, "工程机械", 18.50, 0.007, 1.1),
    ("601186", "中国铁建", BOARD_MAIN, SEC_INFRA, "建筑", 8.40, 0.005, 0.8),

    # ── 运输公用（15）YS ─────────────────────────────────
    ("600002", "长江电力", BOARD_MAIN, SEC_UTILITY, "电力", 22.50, 0.003, 0.6, 0.5),
    ("600005", "中原高速", BOARD_MAIN, SEC_UTILITY, "交通运输", 5.60, 0.004, 0.7),
    ("601001", "国航远洋", BOARD_MAIN, SEC_UTILITY, "航运", 6.10, 0.009, 1.1),
    ("002003", "顺丰物流", BOARD_MAIN, SEC_UTILITY, "物流", 41.00, 0.006, 0.8),
    ("600900", "ST 远洋", BOARD_MAIN, SEC_UTILITY, "航运", 1.80, 0.012, 1.3, 0.8, True),
    ("600014", "长江投资", BOARD_MAIN, SEC_UTILITY, "公用事业", 9.20, 0.004, 0.6),
    ("601006", "宁沪高速", BOARD_MAIN, SEC_UTILITY, "高速公路", 8.90, 0.003, 0.5),
    ("000011", "顺丰控股", BOARD_MAIN, SEC_UTILITY, "物流", 41.20, 0.006, 0.9),
    ("600025", "华能水电", BOARD_MAIN, SEC_UTILITY, "电力", 7.40, 0.004, 0.6),
    ("601021", "春秋航空", BOARD_MAIN, SEC_UTILITY, "航空", 52.30, 0.009, 1.2),
    ("600029", "南方航空", BOARD_MAIN, SEC_UTILITY, "航空", 8.60, 0.008, 1.1),
    ("601112", "中国国航", BOARD_MAIN, SEC_UTILITY, "航空", 9.40, 0.008, 1.1),
    ("002352", "顺丰快运", BOARD_MAIN, SEC_UTILITY, "物流", 38.70, 0.006, 0.8),
    ("600018", "上港集团", BOARD_MAIN, SEC_UTILITY, "港口", 5.90, 0.004, 0.6),
    ("601018", "宁波港", BOARD_MAIN, SEC_UTILITY, "港口", 4.30, 0.004, 0.6),

    # ── 汽车军工（15）QC ─────────────────────────────────
    ("002001", "比亚迪车业", BOARD_MAIN, SEC_AUTO, "汽车", 210.00, 0.010, 1.2, 1.1),
    ("600015", "长城汽车", BOARD_MAIN, SEC_AUTO, "汽车", 22.60, 0.008, 1.1),
    ("000012", "长安汽车", BOARD_MAIN, SEC_AUTO, "汽车", 12.80, 0.008, 1.1),
    ("002010", "中航光电", BOARD_MAIN, SEC_AUTO, "军工", 42.30, 0.008, 1.2),
    ("600016", "中国重工", BOARD_MAIN, SEC_AUTO, "船舶", 4.85, 0.006, 1.0),
    ("300012", "先导智能", BOARD_GEM, SEC_AUTO, "锂电设备", 33.60, 0.012, 1.3, 1.1),
    ("688008", "中航无人机", BOARD_STAR, SEC_AUTO, "军工", 45.20, 0.012, 1.3, 1.0),
    ("600104", "上汽集团", BOARD_MAIN, SEC_AUTO, "汽车", 14.50, 0.006, 0.9),
    ("000625", "长安汽车集团", BOARD_MAIN, SEC_AUTO, "汽车", 13.70, 0.008, 1.1),
    ("601633", "长城汽车股份", BOARD_MAIN, SEC_AUTO, "汽车", 24.30, 0.008, 1.1),
    ("600893", "航发动力", BOARD_MAIN, SEC_AUTO, "军工", 36.80, 0.010, 1.3),
    ("000768", "中航飞机", BOARD_MAIN, SEC_AUTO, "军工", 28.40, 0.009, 1.2),
    ("600760", "中航沈飞", BOARD_MAIN, SEC_AUTO, "军工", 56.20, 0.010, 1.3),
    ("300124", "汇川汽车", BOARD_GEM, SEC_AUTO, "汽车电子", 64.00, 0.011, 1.2),
    ("688320", "芯驰汽车", BOARD_STAR, SEC_AUTO, "汽车芯片", 78.50, 0.013, 1.3, 1.1),

    # ── 农业传媒（15）NY ─────────────────────────────────
    ("002005", "牧原农牧", BOARD_MAIN, SEC_AGRI, "农业", 44.00, 0.009, 0.9),
    ("002900", "ST 中昌", BOARD_MAIN, SEC_AGRI, "传媒", 2.30, 0.013, 1.2, 0.8, True),
    ("600017", "牧原股份", BOARD_MAIN, SEC_AGRI, "养殖", 44.30, 0.008, 0.9),
    ("000013", "海大集团", BOARD_MAIN, SEC_AGRI, "饲料", 52.60, 0.006, 0.8),
    ("002011", "分众传媒", BOARD_MAIN, SEC_AGRI, "传媒", 6.80, 0.007, 1.0),
    ("300013", "芒果超媒", BOARD_GEM, SEC_AGRI, "传媒", 25.40, 0.010, 1.2),
    ("600598", "北大荒", BOARD_MAIN, SEC_AGRI, "农业", 12.30, 0.007, 0.9),
    ("300498", "温氏股份", BOARD_GEM, SEC_AGRI, "养殖", 18.60, 0.008, 0.9),
    ("600439", "通威农业", BOARD_MAIN, SEC_AGRI, "饲料", 22.40, 0.007, 0.9),
    ("000876", "新希望", BOARD_MAIN, SEC_AGRI, "饲料", 10.80, 0.007, 0.9),
    ("002311", "海大饲料", BOARD_MAIN, SEC_AGRI, "饲料", 48.20, 0.006, 0.8),
    ("300017", "光线传媒", BOARD_GEM, SEC_AGRI, "传媒", 8.90, 0.011, 1.2),
    ("002607", "中公教育", BOARD_MAIN, SEC_AGRI, "传媒", 4.60, 0.012, 1.3),
    ("600977", "中国电影", BOARD_MAIN, SEC_AGRI, "传媒", 12.80, 0.008, 1.1),
    ("000713", "丰乐种业", BOARD_MAIN, SEC_AGRI, "种业", 9.40, 0.009, 1.0),
]


def _build_pool() -> tuple[dict[str, Stock], dict[str, Stock]]:
    pool: dict[str, Stock] = {}
    by_short: dict[str, Stock] = {}
    sector_seq: dict[str, int] = {}
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
        prefix = SECTOR_SHORT_PREFIX[sector]
        sector_seq[sector] = sector_seq.get(sector, 0) + 1
        short_code = f"{prefix}{sector_seq[sector]:02d}"
        if short_code in by_short:
            raise ValueError(f"短码重复：{short_code}")
        stock = Stock(code, name, board, sector, industry, price, vol, beta,
                      float(sector_beta), bool(is_st), short_code)
        pool[code] = stock
        by_short[short_code] = stock
    return pool, by_short


STOCK_POOL: dict[str, Stock]
STOCK_BY_SHORT: dict[str, Stock]
STOCK_POOL, STOCK_BY_SHORT = _build_pool()

# 行业列表（展示/搜索用）
INDUSTRIES = sorted({s.industry for s in STOCK_POOL.values()})

# 板块 -> 成分股（6 位原码列表，向后兼容）
SECTOR_MEMBERS: dict[str, list[str]] = {
    sec: sorted(c for c, s in STOCK_POOL.items() if s.sector == sec)
    for sec in SECTORS
}
