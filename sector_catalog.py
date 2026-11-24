"""细分板块目录与论坛采集入口。

代表公司用于解释板块边界，并不是指数成份股或投资推荐。每家公司只在一个
细分板块中主归类，避免同一家公司反复抬高多个板块的社区热度。东方财富和
    新浪和淘股吧使用三家代表公司的个股社区；同花顺则使用明确配置的板块社
    区，而不是拿第一家代表公司的个股社区代替整个板块。每个渠道先在自己的
    三家公司内等权，避免某个平台因为配置入口更多而获得更多正式选票。
"""

from __future__ import annotations

from typing import Dict, List, Tuple


SECTOR_CATALOG_VERSION = 3
SECTOR_CATALOG_ID = "thirty_leaf_sectors_v3_ths_communities"

PARENT_SECTORS: Dict[str, str] = {
    "technology": "科技与数字经济",
    "advanced_manufacturing": "高端制造与军工",
    "consumer": "消费",
    "healthcare": "医药医疗",
    "new_energy_auto": "新能源与汽车",
    "finance_infrastructure": "金融地产基建",
    "resources_utilities": "资源化工与公用",
    "agriculture": "农业",
}


Company = Tuple[str, str, str]
Community = Tuple[str, str]


def _sector(
    name: str,
    parent_key: str,
    description: str,
    companies: List[Company],
    ths_communities: List[Community],
) -> Dict[str, object]:
    """生成统一目录项，并把展示公司与实际采集入口明确分开。"""

    representatives = [
        {"code": code, "name": company_name, "role": role}
        for code, company_name, role in companies
    ]
    source_stocks = [(code, company_name) for code, company_name, _ in companies]
    sina_stocks = [(code, company_name) for code, company_name, _ in companies]
    communities = [
        {"code": code, "name": community_name, "marketId": 48}
        for code, community_name in ths_communities
    ]
    return {
        "name": name,
        "parentKey": parent_key,
        "parentName": PARENT_SECTORS[parent_key],
        "description": description,
        "representativeCompanies": representatives,
        # 细分主题没有足够纯净的行业吧时宁可不用，避免宽泛帖子把板块拉平。
        "industry": None,
        "sourceStocks": source_stocks,
        # 兼容历史采集器仍读取 stocks 的旧接口。
        "stocks": list(source_stocks),
        "sinaStocks": sina_stocks,
        "thsCommunities": communities,
    }


SECTORS: Dict[str, Dict[str, object]] = {
    "optical_interconnect": _sector(
        "CPO/光模块",
        "technology",
        "观察高速光模块、光器件及数据中心光互连；不包含普通通信设备、服务器和PCB。CPO是其中的产业主题子集，不代表三家公司全部收入均来自CPO。",
        [
            ("300308", "中际旭创", "高速光模块龙头"),
            ("300502", "新易盛", "高性能光模块"),
            ("300394", "天孚通信", "光器件与光引擎平台"),
        ],
        [("886033", "CPO概念")],
    ),
    "pcb": _sector(
        "PCB",
        "technology",
        "观察高多层板、HDI和印制电路板制造；不包含芯片、服务器和普通电子零部件。",
        [
            ("002463", "沪电股份", "高速通信与服务器PCB"),
            ("300476", "胜宏科技", "高多层板与HDI"),
            ("002916", "深南电路", "PCB与封装基板综合平台"),
        ],
        [("885959", "PCB概念")],
    ),
    "ai_compute": _sector(
        "AI算力",
        "technology",
        "观察AI服务器、高性能计算整机、智算中心和算力基础设施；不包含芯片、光模块及PCB。",
        [
            ("000977", "浪潮信息", "AI服务器"),
            ("603019", "中科曙光", "高端计算与智算"),
            ("601138", "工业富联", "AI服务器综合制造"),
        ],
        [("885957", "AI算力")],
    ),
    "ai_apps": _sector(
        "AI应用",
        "technology",
        "观察直接面向办公、教育和内容创作等场景的AI软件与产品；不包含底层硬件和算力出租。",
        [
            ("002230", "科大讯飞", "语音与大模型应用"),
            ("688111", "金山办公", "AI办公软件"),
            ("300624", "万兴科技", "AIGC创意软件"),
        ],
        [("886108", "AI应用")],
    ),
    "chip_design": _sector(
        "芯片设计",
        "technology",
        "观察CPU、AI芯片、存储器和MCU等芯片设计；不包含制造设备、材料与晶圆代工。",
        [
            ("688041", "海光信息", "高端处理器设计"),
            ("688256", "寒武纪", "智能计算芯片"),
            ("603986", "兆易创新", "存储与MCU设计"),
        ],
        [("885756", "芯片设计")],
    ),
    "semi_equipment_materials": _sector(
        "半导体设备材料",
        "technology",
        "观察晶圆制造设备、刻蚀沉积设备和半导体硅片等上游环节；不包含芯片设计。",
        [
            ("002371", "北方华创", "半导体制造设备"),
            ("688012", "中微公司", "刻蚀与薄膜设备"),
            ("688126", "沪硅产业", "半导体硅片"),
        ],
        [("881121", "半导体设备材料")],
    ),
    "telecom_network": _sector(
        "通信设备与运营",
        "technology",
        "观察运营商、无线与有线网络设备及骨干传输；不包含数据中心光模块主题。",
        [
            ("600941", "中国移动", "综合通信运营商"),
            ("000063", "中兴通讯", "通信网络设备"),
            ("600498", "烽火通信", "光通信系统设备"),
        ],
        [("881129", "通信")],
    ),
    "media_games": _sector(
        "传媒游戏",
        "technology",
        "观察游戏、长视频和数字内容平台；不包含办公软件和纯硬件公司。",
        [
            ("002555", "三七互娱", "网络游戏发行运营"),
            ("002517", "恺英网络", "网络游戏研发运营"),
            ("300413", "芒果超媒", "长视频与数字内容"),
        ],
        [("881275", "游戏")],
    ),
    "industrial_robotics": _sector(
        "机器人与自动化",
        "advanced_manufacturing",
        "观察工业机器人、伺服运动控制和精密减速器；不包含仅因使用机器人而被贴概念的制造企业。",
        [
            ("002747", "埃斯顿", "工业机器人本体"),
            ("300124", "汇川技术", "工业自动化与伺服"),
            ("688017", "绿的谐波", "精密减速器"),
        ],
        [("885517", "机器人")],
    ),
    "aerospace_defense": _sector(
        "航空航天军工",
        "advanced_manufacturing",
        "观察军用飞机、航空发动机和核心航空装备；不包含只有少量军品业务的公司。",
        [
            ("600760", "中航沈飞", "军用飞机制造"),
            ("000768", "中航西飞", "大中型军民用飞机"),
            ("600893", "航发动力", "航空发动机"),
        ],
        [("885700", "军工")],
    ),
    "baijiu": _sector(
        "白酒",
        "consumer",
        "仅观察白酒生产与品牌经营，不包含啤酒、乳制品和一般食品。",
        [
            ("600519", "贵州茅台", "高端白酒"),
            ("000858", "五粮液", "浓香型白酒"),
            ("600809", "山西汾酒", "清香型白酒"),
        ],
        [("881273", "白酒")],
    ),
    "food_beverage": _sector(
        "食品饮料",
        "consumer",
        "观察乳制品、调味品、速冻食品等大众食品；不包含白酒和餐饮旅游。",
        [
            ("600887", "伊利股份", "乳制品"),
            ("603288", "海天味业", "调味品"),
            ("603345", "安井食品", "速冻食品"),
        ],
        [("881134", "食品")],
    ),
    "home_appliances": _sector(
        "家电",
        "consumer",
        "观察白电、厨电及综合家电制造；不包含家居零售和地产后周期材料。",
        [
            ("000333", "美的集团", "综合家电"),
            ("000651", "格力电器", "空调与白电"),
            ("600690", "海尔智家", "全球化白电"),
        ],
        [("881131", "家电")],
    ),
    "innovative_drugs": _sector(
        "创新药",
        "healthcare",
        "观察拥有自主新药研发和商业化管线的制药企业；不包含CXO及纯仿制药。",
        [
            ("600276", "恒瑞医药", "创新药研发商业化"),
            ("688235", "百济神州", "全球化创新药"),
            ("002422", "科伦药业", "创新药与传统制药综合型"),
        ],
        [("886015", "创新药")],
    ),
    "medical_devices": _sector(
        "医疗器械",
        "healthcare",
        "观察诊断监护、医学影像和家用医疗器械；不包含医院、眼科等医疗服务。",
        [
            ("300760", "迈瑞医疗", "诊断与监护设备"),
            ("688271", "联影医疗", "医学影像设备"),
            ("002223", "鱼跃医疗", "家用医疗器械"),
        ],
        [("881144", "医疗器械")],
    ),
    "battery_storage": _sector(
        "锂电池与储能",
        "new_energy_auto",
        "观察动力与储能电芯和电池系统；不包含锂矿、整车与光伏电站。",
        [
            ("300750", "宁德时代", "动力与储能电池"),
            ("300014", "亿纬锂能", "动力与储能电芯"),
            ("002074", "国轩高科", "动力电池系统"),
        ],
        [("881281", "电池储能")],
    ),
    "solar_pv": _sector(
        "光伏",
        "new_energy_auto",
        "观察硅片、电池片、组件和光伏逆变器；不包含电站运营和电网设备。",
        [
            ("601012", "隆基绿能", "硅片与组件"),
            ("688223", "晶科能源", "光伏组件"),
            ("300274", "阳光电源", "逆变器与储能综合型"),
        ],
        [("881279", "光伏")],
    ),
    "new_energy_vehicles": _sector(
        "新能源整车",
        "new_energy_auto",
        "观察新能源乘用车品牌和整车制造；不包含动力电池与零部件。",
        [
            ("002594", "比亚迪", "新能源汽车整车"),
            ("601127", "赛力斯", "智能新能源汽车"),
            ("601633", "长城汽车", "综合汽车制造"),
        ],
        [("885431", "新能源车")],
    ),
    "auto_parts_smart_drive": _sector(
        "汽车零部件与智能驾驶",
        "new_energy_auto",
        "观察智能座舱、辅助驾驶、底盘和综合汽车零部件；不包含整车企业。",
        [
            ("002920", "德赛西威", "智能座舱与辅助驾驶"),
            ("601689", "拓普集团", "底盘与汽车零部件"),
            ("600741", "华域汽车", "综合汽车零部件"),
        ],
        [("881126", "汽车零部件")],
    ),
    "banks": _sector(
        "银行",
        "finance_infrastructure",
        "仅观察商业银行，不包含券商、保险和互联网金融平台。",
        [
            ("600036", "招商银行", "股份制商业银行"),
            ("601398", "工商银行", "国有大型商业银行"),
            ("002142", "宁波银行", "区域性商业银行"),
        ],
        [("881155", "银行")],
    ),
    "nonbank_finance": _sector(
        "券商保险",
        "finance_infrastructure",
        "观察券商、保险和互联网证券服务；不包含商业银行。",
        [
            ("600030", "中信证券", "综合证券公司"),
            ("300059", "东方财富", "互联网证券综合平台"),
            ("601318", "中国平安", "综合保险金融"),
        ],
        [("881157", "证券"), ("881156", "保险")],
    ),
    "property_developers": _sector(
        "房地产开发",
        "finance_infrastructure",
        "观察住宅及商业地产开发；不包含建筑施工、建材和物业服务。",
        [
            ("600048", "保利发展", "综合地产开发"),
            ("000002", "万科A", "住宅与综合开发"),
            ("001979", "招商蛇口", "园区与地产开发"),
        ],
        [("881153", "房地产")],
    ),
    "infrastructure": _sector(
        "基建央企",
        "finance_infrastructure",
        "观察房建、铁路、公路和大型工程承包；不包含地产开发及单一建材。",
        [
            ("601668", "中国建筑", "房建与综合基建"),
            ("601390", "中国中铁", "铁路与综合工程"),
            ("601800", "中国交建", "交通基础设施"),
        ],
        [("881116", "基建")],
    ),
    "gold": _sector(
        "黄金",
        "resources_utilities",
        "观察以黄金采选冶炼为核心的企业；不包含仅有少量黄金副产品的综合矿企。",
        [
            ("600547", "山东黄金", "黄金采选冶炼"),
            ("600988", "赤峰黄金", "黄金矿业"),
            ("600489", "中金黄金", "黄金采选冶炼"),
        ],
        [("881169", "黄金")],
    ),
    "industrial_metals": _sector(
        "工业金属与稀土",
        "resources_utilities",
        "观察铜、钼、钴及稀土等工业金属；不包含纯黄金和钢铁。",
        [
            ("601899", "紫金矿业", "铜金等综合矿业，主归工业金属"),
            ("603993", "洛阳钼业", "铜钴钼综合矿业"),
            ("600111", "北方稀土", "稀土冶炼与材料"),
        ],
        [("881168", "工业金属"), ("885343", "稀土永磁")],
    ),
    "basic_chemicals": _sector(
        "基础化工",
        "resources_utilities",
        "观察聚氨酯、煤化工、烯烃及大宗化工材料；不包含锂电和半导体专用材料。",
        [
            ("600309", "万华化学", "聚氨酯与新材料"),
            ("600426", "华鲁恒升", "煤化工与基础化工"),
            ("002648", "卫星化学", "烯烃与化工新材料"),
        ],
        [("881108", "化学原料"), ("881109", "化学制品")],
    ),
    "power_operators": _sector(
        "电力运营",
        "resources_utilities",
        "观察水电、核电、火电及综合发电运营；不包含设备制造和光伏组件。",
        [
            ("600900", "长江电力", "水电运营"),
            ("601985", "中国核电", "核电运营"),
            ("600795", "国电电力", "综合电力运营"),
        ],
        [("881145", "电力")],
    ),
    "grid_equipment": _sector(
        "电网设备",
        "resources_utilities",
        "观察电网自动化、继电保护、输变电和高压开关设备；不包含发电运营。",
        [
            ("600406", "国电南瑞", "电网自动化"),
            ("000400", "许继电气", "继电保护与输变电"),
            ("600312", "平高电气", "高压开关设备"),
        ],
        [("881278", "电网")],
    ),
    "hog_farming": _sector(
        "生猪养殖",
        "agriculture",
        "观察生猪育种、养殖和屠宰产业链；不包含种业和以禽类为主的企业。",
        [
            ("002714", "牧原股份", "生猪养殖"),
            ("300498", "温氏股份", "畜禽养殖，主归生猪"),
            ("000876", "新希望", "饲料与养殖综合型"),
        ],
        [("885573", "生猪")],
    ),
    "seeds": _sector(
        "种业粮食",
        "agriculture",
        "观察水稻、玉米等农作物种子研发和销售；不包含养殖、农机和普通农产品加工。",
        [
            ("000998", "隆平高科", "水稻与玉米种业"),
            ("002041", "登海种业", "玉米种业"),
            ("300087", "荃银高科", "水稻等综合种业"),
        ],
        [("885812", "农业种植"), ("881101", "种植业与林业")],
    ),
}


def _validate_catalog() -> None:
    if len(SECTORS) != 30:
        raise ValueError(f"细分板块目录应为30项，实际为{len(SECTORS)}项")
    company_codes: List[str] = []
    for key, definition in SECTORS.items():
        representatives = definition.get("representativeCompanies")
        if not isinstance(representatives, list) or len(representatives) != 3:
            raise ValueError(f"{key} 必须恰好配置3家典型公司")
        if definition.get("parentKey") not in PARENT_SECTORS:
            raise ValueError(f"{key} 的 parentKey 无效")
        codes = [str(item.get("code") or "") for item in representatives]
        if any(len(code) != 6 or not code.isdigit() for code in codes):
            raise ValueError(f"{key} 存在无效A股代码")
        company_codes.extend(codes)
        communities = definition.get("thsCommunities")
        if not isinstance(communities, list) or not communities:
            raise ValueError(f"{key} 必须至少配置1个同花顺板块社区")
        for community in communities:
            if not isinstance(community, dict):
                raise ValueError(f"{key} 的同花顺板块社区配置格式无效")
            community_code = str(community.get("code") or "")
            if len(community_code) != 6 or not community_code.isdigit():
                raise ValueError(f"{key} 存在无效同花顺板块社区代码")
            if not str(community.get("name") or "").strip():
                raise ValueError(f"{key} 的同花顺板块社区名称不能为空")
            if community.get("marketId") != 48:
                raise ValueError(f"{key} 的同花顺板块社区 marketId 必须为48")
    if len(company_codes) != len(set(company_codes)):
        raise ValueError("典型公司必须唯一主归类，不能跨细分板块重复")


_validate_catalog()
