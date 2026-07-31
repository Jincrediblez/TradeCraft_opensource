#!/usr/bin/env python3
"""Build the user-centered illustrated TradeCraft feature guide."""

from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parent.parent
IMAGE_DIR = ROOT / "docs" / "images" / "feature-guide"
OUTPUT = ROOT / "TradeCraft_系统功能手册_zh-CN.pdf"

INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#626262")
LINE = colors.HexColor("#D7D7D7")
PANEL = colors.HexColor("#F5F5F3")
GREEN = colors.HexColor("#089981")
RED = colors.HexColor("#F23645")
YELLOW = colors.HexColor("#FFF2B8")
WHITE = colors.white


def register_fonts() -> tuple[str, str]:
    candidates = [
        (
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
        ),
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ),
        (
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ),
    ]
    for regular, bold in candidates:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("TCRegular", regular, subfontIndex=0))
            pdfmetrics.registerFont(TTFont("TCBold", bold, subfontIndex=0))
            pdfmetrics.registerFontFamily(
                "TC",
                normal="TCRegular",
                bold="TCBold",
                italic="TCRegular",
                boldItalic="TCBold",
            )
            return "TCRegular", "TCBold"
    raise RuntimeError(
        "A Chinese font is required. Install Noto Sans CJK or WenQuanYi Zen Hei."
    )


REGULAR, BOLD = register_fonts()
BASE = getSampleStyleSheet()
STYLES = {
    "cover_title": ParagraphStyle(
        "CoverTitle",
        parent=BASE["Title"],
        fontName=BOLD,
        fontSize=34,
        leading=43,
        textColor=INK,
        alignment=TA_CENTER,
        spaceAfter=8,
    ),
    "cover_subtitle": ParagraphStyle(
        "CoverSubtitle",
        parent=BASE["Normal"],
        fontName=REGULAR,
        fontSize=14,
        leading=23,
        textColor=MUTED,
        alignment=TA_CENTER,
    ),
    "cover_tagline": ParagraphStyle(
        "CoverTagline",
        parent=BASE["Normal"],
        fontName=BOLD,
        fontSize=14,
        leading=22,
        textColor=INK,
        alignment=TA_CENTER,
    ),
    "cover_meta": ParagraphStyle(
        "CoverMeta",
        parent=BASE["Normal"],
        fontName=REGULAR,
        fontSize=9,
        leading=14,
        textColor=MUTED,
        alignment=TA_CENTER,
    ),
    "eyebrow": ParagraphStyle(
        "Eyebrow",
        parent=BASE["Normal"],
        fontName=BOLD,
        fontSize=8.5,
        leading=11,
        textColor=GREEN,
        spaceAfter=5,
    ),
    "h1": ParagraphStyle(
        "H1",
        parent=BASE["Heading1"],
        fontName=BOLD,
        fontSize=23,
        leading=30,
        textColor=INK,
        spaceAfter=8,
    ),
    "h2": ParagraphStyle(
        "H2",
        parent=BASE["Heading2"],
        fontName=BOLD,
        fontSize=15,
        leading=21,
        textColor=INK,
        spaceBefore=6,
        spaceAfter=6,
    ),
    "body": ParagraphStyle(
        "Body",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=9.5,
        leading=15.5,
        textColor=INK,
        spaceAfter=6,
    ),
    "body_large": ParagraphStyle(
        "BodyLarge",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=11,
        leading=18,
        textColor=INK,
        spaceAfter=7,
    ),
    "small": ParagraphStyle(
        "Small",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=7.7,
        leading=11.5,
        textColor=MUTED,
    ),
    "bullet": ParagraphStyle(
        "Bullet",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=9.3,
        leading=15,
        leftIndent=13,
        firstLineIndent=-9,
        bulletIndent=2,
        textColor=INK,
        spaceAfter=3,
    ),
    "value_label": ParagraphStyle(
        "ValueLabel",
        parent=BASE["BodyText"],
        fontName=BOLD,
        fontSize=8,
        leading=11,
        textColor=GREEN,
        spaceAfter=2,
    ),
    "value_text": ParagraphStyle(
        "ValueText",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=8.2,
        leading=12,
        textColor=INK,
    ),
    "caption": ParagraphStyle(
        "Caption",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=7.2,
        leading=10,
        textColor=MUTED,
        alignment=TA_CENTER,
        spaceBefore=4,
    ),
    "table_head": ParagraphStyle(
        "TableHead",
        parent=BASE["BodyText"],
        fontName=BOLD,
        fontSize=8.2,
        leading=11.5,
        textColor=WHITE,
    ),
    "table_cell": ParagraphStyle(
        "TableCell",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=8,
        leading=12,
        textColor=INK,
    ),
    "table_cell_small": ParagraphStyle(
        "TableCellSmall",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=7.3,
        leading=10.6,
        textColor=INK,
    ),
    "code": ParagraphStyle(
        "Code",
        parent=BASE["Code"],
        fontName="Courier",
        fontSize=8.3,
        leading=13,
        textColor=INK,
    ),
}


class ColorBar(Flowable):
    def __init__(self, width: float, height: float = 6):
        super().__init__()
        self.width = width
        self.height = height

    def draw(self):
        parts = [
            (0.48, INK),
            (0.22, GREEN),
            (0.18, RED),
            (0.12, colors.HexColor("#E4C447")),
        ]
        cursor = 0
        for ratio, color in parts:
            segment = self.width * ratio
            self.canv.setFillColor(color)
            self.canv.rect(cursor, 0, segment, self.height, fill=1, stroke=0)
            cursor += segment


def p(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, STYLES[style])


def bullet(text: str) -> Paragraph:
    return Paragraph(f"- {text}", STYLES["bullet"])


def box(text: str, background=PANEL) -> Table:
    table = Table([[p(text, "body")]], colWidths=[177 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.7, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def pain_value(pain: str, value: str) -> Table:
    rows = [
        [
            p("主动交易者的痛点", "value_label"),
            p("TradeCraft 带来的价值", "value_label"),
        ],
        [p(pain, "value_text"), p(value, "value_text")],
    ]
    table = Table(rows, colWidths=[88.5 * mm, 88.5 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FFF7F7")),
                ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#F1FAF7")),
                ("BOX", (0, 0), (-1, -1), 0.7, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def fitted_image(path: Path, max_width=177 * mm, max_height=149 * mm) -> Image:
    if not path.exists():
        raise FileNotFoundError(path)
    with PILImage.open(path) as source:
        width, height = source.size
    scale = min(max_width / width, max_height / height)
    return Image(str(path), width=width * scale, height=height * scale)


def screenshot_page(
    story: list,
    section: str,
    title: str,
    pain: str,
    value: str,
    filename: str,
    caption: str,
):
    image_path = IMAGE_DIR / filename
    image = fitted_image(image_path)
    story.extend(
        [
            PageBreak(),
            p(section, "eyebrow"),
            p(title, "h1"),
            pain_value(pain, value),
            Spacer(1, 4 * mm),
            Table(
                [[image]],
                colWidths=[179 * mm],
                style=TableStyle(
                    [
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("BACKGROUND", (0, 0), (-1, -1), WHITE),
                        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
                        ("LEFTPADDING", (0, 0), (-1, -1), 2),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                ),
            ),
            p(caption, "caption"),
        ]
    )


def pain_table() -> Table:
    rows = [
        [
            p("常见痛点", "table_head"),
            p("为什么普通交易日志不够", "table_head"),
            p("TradeCraft 的处理方式", "table_head"),
        ],
        [
            p("信息分散", "table_cell"),
            p("成交、K 线、持仓、净值和笔记存在不同文件或软件中。", "table_cell"),
            p("把账户结果、单票复盘、数据排行和测评放进同一个本地系统。", "table_cell"),
        ],
        [
            p("结果偏见", "table_cell"),
            p("赚钱的交易容易被自动解释为正确，亏钱的交易容易被自动解释为错误。", "table_cell"),
            p("将结果质量、过程风险、近期行为和数据可信度分别呈现。", "table_cell"),
        ],
        [
            p("记忆失真", "table_cell"),
            p("复盘时容易用后来的行情重新编写当时的理由。", "table_cell"),
            p("把实际 fill、当日 K 线、交易计划和复盘补录关联起来。", "table_cell"),
        ],
        [
            p("过度交易", "table_cell"),
            p("日常只看到单笔盈亏，看不到换手、反复进入和交易密度。", "table_cell"),
            p("用排行、热力图和交易行为图暴露资金与注意力的真实分配。", "table_cell"),
        ],
        [
            p("优势幻觉", "table_cell"),
            p("少数盈利样本常被误认为已经证明的策略。", "table_cell"),
            p("区分已确认发现、疑似问题、数据不足和候选优势。", "table_cell"),
        ],
        [
            p("复盘不闭环", "table_cell"),
            p("写完总结后没有明确规则，也没有后续验证。", "table_cell"),
            p("把发现转成未来 20 个交易日的铁律周期并持续跟踪。", "table_cell"),
        ],
    ]
    table = Table(rows, colWidths=[30 * mm, 66 * mm, 81 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def weights_table() -> Table:
    rows = [
        [p("维度", "table_head"), p("权重", "table_head"), p("回答的问题", "table_head")],
        [p("交易摩擦", "table_cell"), p("20.45%", "table_cell"), p("是否反复进出、持有过短或制造了不必要换手？", "table_cell")],
        [p("选股质量", "table_cell"), p("20.45%", "table_cell"), p("选中的股票和主题是否真正创造了选择 alpha？", "table_cell")],
        [p("入场质量", "table_cell"), p("15.91%", "table_cell"), p("是否在延伸位置追入，入场后是否立即承受不利走势？", "table_cell")],
        [p("尾部风险", "table_cell"), p("15.91%", "table_cell"), p("仓位、集中度和大额损失暴露是否失控？", "table_cell")],
        [p("主题判断", "table_cell"), p("9.09%", "table_cell"), p("资本是否配置到了原本要参与的主线？", "table_cell")],
        [p("叙事热度", "table_cell"), p("9.09%", "table_cell"), p("是否为缺乏证据的 optionality 或 FOMO 支付过多风险？", "table_cell")],
        [p("退出质量", "table_cell"), p("9.09%", "table_cell"), p("退出是否过早，是否违反趋势或计划？", "table_cell")],
    ]
    table = Table(rows, colWidths=[35 * mm, 25 * mm, 117 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def quickstart_table() -> Table:
    rows = [
        [
            p("复盘动作", "table_head"),
            p("打开哪里", "table_head"),
            p("你要看清什么", "table_head"),
        ],
        [
            p("先选重点", "table_cell"),
            p("首页", "table_cell"),
            p("数据是否最新，今天哪几笔交易最值得复盘。", "table_cell"),
        ],
        [
            p("还原过程", "table_cell"),
            p("复盘 + 交易记录", "table_cell"),
            p("当时在哪里买卖、原计划是什么、执行是否走样。", "table_cell"),
        ],
        [
            p("看全局", "table_cell"),
            p("数据 + 市场 + 业绩", "table_cell"),
            p("盈亏来自哪里，交易是否过密，市场背景和净值路径怎样。", "table_cell"),
        ],
        [
            p("形成改进", "table_cell"),
            p("测评", "table_cell"),
            p("哪个问题有证据，下一阶段只跟踪哪一条改进规则。", "table_cell"),
        ],
    ]
    table = Table(rows, colWidths=[31 * mm, 49 * mm, 97 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def page_map_table() -> Table:
    rows = [
        [p("页面", "table_head"), p("什么时候打开", "table_head"), p("你会得到什么", "table_head")],
        [p("首页", "table_cell_small"), p("每天开始复盘时", "table_cell_small"), p("账户快照、数据新鲜度、回撤、敞口、复盘队列和盈亏贡献。", "table_cell_small")],
        [p("复盘", "table_cell_small"), p("要重建一只股票的决策时", "table_cell_small"), p("K 线、成交量、BUY/SELL 标记、fill、计划和复盘笔记。", "table_cell_small")],
        [p("数据", "table_cell_small"), p("要看整个账本的真实分布时", "table_cell_small"), p("成交额/笔数排行、盈亏贡献、热力图、交易强度和密度。", "table_cell_small")],
        [p("交易记录", "table_cell_small"), p("要核对底层成交时", "table_cell_small"), p("按股票分组的日期、方向、数量、价格、佣金、setup 和备注。", "table_cell_small")],
        [p("市场", "table_cell_small"), p("要补充大盘和行业背景时", "table_cell_small"), p("Nasdaq、S&amp;P 500、YTD 热力图和 Finviz 入口；需要联网。", "table_cell_small")],
        [p("自选", "table_cell_small"), p("要维护本地研究队列时", "table_cell_small"), p("分组、排序、颜色、搜索、批量操作、TradingView 文本导入和触发条件。", "table_cell_small")],
        [p("业绩", "table_cell_small"), p("要检查净值路径时", "table_cell_small"), p("月度净值、月收益、YTD/累计收益和本地业绩工作簿。", "table_cell_small")],
        [p("测评", "table_cell_small"), p("要判断结果、过程和行为时", "table_cell_small"), p("四类记分卡、七维风险、证据下钻、判断记录、铁律和可选 AI 总结。", "table_cell_small")],
        [p("设置", "table_cell_small"), p("要改变工作区口径或导入数据时", "table_cell_small"), p("语言、默认区间/代码、刷新间隔、模型、基准、数据更新和触发器。", "table_cell_small")],
    ]
    table = Table(rows, colWidths=[25 * mm, 55 * mm, 97 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def page_decor(canvas, _doc):
    page = canvas.getPageNumber()
    width, height = A4
    canvas.saveState()
    if page > 1:
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(16 * mm, height - 13 * mm, width - 16 * mm, height - 13 * mm)
        canvas.setFont(REGULAR, 7.2)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, height - 10 * mm, "TradeCraft 交易复盘系统")
        canvas.drawRightString(width - 16 * mm, 9 * mm, f"{page:02d}")
    canvas.restoreState()


def build_story() -> list:
    story = [
        Spacer(1, 19 * mm),
        Image(str(ROOT / "static" / "site-icon.png"), width=31 * mm, height=31 * mm),
        Spacer(1, 7 * mm),
        p("TradeCraft", "cover_title"),
        p("系统功能手册", "cover_title"),
        Spacer(1, 3 * mm),
        p("为严肃交易者打造的交易复盘系统。", "cover_tagline"),
        p("知己，才能善战。", "cover_subtitle"),
        Spacer(1, 2 * mm),
        p("用户版 · 从第一次启动到形成可验证的复盘规则", "cover_meta"),
        Spacer(1, 12 * mm),
        ColorBar(177 * mm, 6),
        Spacer(1, 11 * mm),
        box(
            "<b>文档数据说明：</b>本手册所有账户、交易、持仓、收益、股票组合和测评结论均来自随机合成 Demo，"
            "不包含真实个人数据。界面截图统一使用约 2000×1750 的高清素材，每张截图独立成页。",
            YELLOW,
        ),
        Spacer(1, 20 * mm),
        p("v0.1.0  |  Local-first  |  English + 简体中文", "cover_subtitle"),
        PageBreak(),
        p("01-03  交易者一页上手", "eyebrow"),
        p("一页看懂 TradeCraft", "h1"),
        p(
            "TradeCraft 不是一个告诉你明天买什么的工具。它把你的成交、行情、账户结果和交易计划放在一起，"
            "帮助你看清：这次赚亏从哪里来，当时的决定是否合理，同类问题是否反复发生，下一次具体要改什么。",
            "body_large",
        ),
        pain_value(
            "只看最后赚了还是亏了，容易把运气当能力，也容易把一次错误解释成偶然。",
            "把结果、过程和证据分开看，找到真正值得保留或改变的做法。",
        ),
        Spacer(1, 5 * mm),
        p("第一次打开", "h2"),
        bullet("顶部出现黄色“演示数据”横条时，所有账户、交易和收益都是虚构的；可以放心浏览。"),
        bullet("先从首页进入一只股票的复盘，再看数据、业绩和测评，体验一次完整复盘。"),
        p("换成自己的真实数据", "h2"),
        bullet("点击黄色横条里的“使用真实数据”，然后打开“设置 → 数据更新”。"),
        bullet("上传券商导出文件（当前支持 IBKR），点击“更新数据”，完成后回首页核对数据截止日期。"),
        p("每天怎么用", "h2"),
        quickstart_table(),
        Spacer(1, 6 * mm),
        box(
            "<b>记住边界：</b>TradeCraft 不登录券商、不替你下单、不预测市场，也不把测评分数当成买卖信号。"
            "它的作用是让你的复盘更有证据，并把一个确认的问题变成下一阶段可检查的规则。",
            YELLOW,
        ),
        PageBreak(),
        p("04  数据、联网与 AI 边界", "eyebrow"),
        p("本地优先，确定性计算优先", "h1"),
        p(
            "TradeCraft 只监听 127.0.0.1，真实工作区保存在本机，不登录券商、不下单、"
            "不提供交易信号，也不包含 TradeCraft 遥测。",
            "body_large",
        ),
        p("本地事实管线", "h2"),
        bullet("输入：IBKR 导出与可选业绩工作簿。"),
        bullet("处理：解析、校验、FIFO 匹配、持仓对账、主题归因、七维评分和证据索引。"),
        bullet("输出：按年度隔离的本地状态、行情缓存、复盘页面、业绩和测评工作台。"),
        p("明确的外部边界", "h2"),
        bullet("市场页的 TradingView 和 Finviz 是可见的第三方联网组件；它们不接收本地账户与成交数据。"),
        bullet("真实工作区只有在用户主动点击“生成 AI 总结”时才调用已配置的 Kimi；没有 API key 不影响核心功能。"),
        bullet("外部服务失败时，确定性测评工作台仍然可用。"),
        box(
            "<b>安全习惯：</b>不要提交 .env、data/、cache/、logs/、数据库、券商导出、业绩工作簿或本机路径。"
            "公开 Issue 和 PR 中也不要粘贴真实账户、交易或收益。",
            YELLOW,
        ),
    ]

    screenshots = [
        (
            "05  每日复盘",
            "首页：先决定今天最值得复盘什么",
            "账户信息、数据截止日、近期交易和盈亏贡献分散，打开系统后很难迅速确定优先级。",
            "在一个入口同时确认净值、现金、敞口、回撤、TWR、数据新鲜度和复盘队列，先处理最重要的问题。",
            "01-home.jpg",
            "首页：随机 Demo 的账户概览、数据状态、今日复盘队列和盈亏贡献。",
        ),
        (
            "05  每日复盘",
            "复盘：把市场走势与实际成交放在一起",
            "只看成交表无法重建当时的市场位置，只看 K 线又容易忽略真实的加减仓和执行细节。",
            "将复权 K 线、成交量、均线、BUY/SELL 标记、fill、计划和复盘笔记放在同一条证据链中。",
            "02-replay.jpg",
            "复盘：合成 AAPL.US 的 K 线、成交点、复盘摘要和交易计划。",
        ),
        (
            "05  每日复盘",
            "交易记录：回到底层成交事实",
            "当交易数量多、存在分批成交时，记忆中的“这笔交易”可能与真实 fill 不一致。",
            "按股票折叠查看日期、方向、数量、价格和 setup 标签，为 FIFO 回合和测评证据提供底层事实。",
            "03-trades.jpg",
            "交易记录：随机 Demo fill 按股票分组，可展开检查和标注买卖逻辑。",
        ),
        (
            "06  研究队列",
            "自选：维护本地研究队列",
            "候选标的散落在聊天、网页和笔记中，难以表达研究状态、优先级和等待条件。",
            "用本地 SQLite 分组、颜色、排序、搜索和图表维护研究队列，并与持仓和触发条件区分。",
            "04-watchlist.jpg",
            "自选主界面：分组列表、行情信息和本地缓存图表。",
        ),
        (
            "06  研究队列",
            "自选新增：批量建立候选池",
            "逐个录入股票耗时，外部 Watchlist 又容易把个人研究流留在第三方平台。",
            "支持批量代码、TradingView 文本格式、目标分组和颜色标记，快速建立本地候选池。",
            "05-watchlist-add.jpg",
            "自选新增面板：批量加入、选择分组、颜色和创建新分组。",
        ),
        (
            "07  业绩路径",
            "业绩：关注净值路径，而不是只看期末盈亏",
            "少数大额盈利可能掩盖长期回撤和不稳定的月度表现，期末金额无法说明过程。",
            "用月度净值、月收益、累计收益和 YTD 观察账户路径，识别回撤、恢复和波动结构。",
            "06-performance.jpg",
            "业绩追踪：随机 Demo 的月度净值曲线和收益表。",
        ),
        (
            "07  业绩路径",
            "业绩 YTD：聚焦当前年度",
            "跨年度数据会稀释当年策略变化，难以判断今年的真实进展。",
            "一键切换 YTD，将当前年度净值和月度回报单独检查。",
            "07-performance-ytd.jpg",
            "业绩 YTD 状态：当前年度月度净值与累计回报。",
        ),
        (
            "08  交易数据",
            "数据 - 排行：看清资金和注意力流向",
            "主观上觉得关注某些股票，不代表资金和成交真的集中在那里。",
            "同时查看成交额、fill 数量、盈利/亏损贡献和其他品种盈亏，识别实际资源分配。",
            "09-data-rank.jpg",
            "数据排行：成交额、成交笔数、盈亏贡献和其他品种盈亏。",
        ),
        (
            "08  交易数据",
            "数据 - 盈亏热力图：一眼识别贡献集中度",
            "表格很难快速显示哪些标的决定了整个期间的结果，也难看出亏损是否集中。",
            "用面积表示相对规模、颜色表示盈亏方向，快速定位组合结果的主要来源。",
            "10-data-pnl.jpg",
            "盈亏热力图：随机 Demo 标的按盈亏贡献进行面积和颜色编码。",
        ),
        (
            "08  交易数据",
            "数据 - 交易金额热力图：暴露资本与注意力集中",
            "高频关注和大额成交容易被单笔盈亏掩盖，交易者可能低估自己在某些标的上的投入。",
            "按成交金额显示真实的资金与注意力分配，辅助检查集中度和交易摩擦。",
            "11-data-amount.jpg",
            "交易金额热力图：不同标的的累计成交金额结构。",
        ),
        (
            "08  交易数据",
            "数据 - 交易行为图：识别过度交易和情绪化爆发",
            "逐笔成交无法直观看到某些日期是否出现异常频率或金额爆发。",
            "将每日 fill 数量和成交金额按时间展开，定位高频、密集和异常交易日。",
            "12-data-activity.jpg",
            "交易行为图：上方为每日成交笔数，下方为每日成交金额。",
        ),
        (
            "09  市场背景",
            "市场 - Nasdaq：查看当日科技成长股广度",
            "只盯持仓容易忽略同板块和指数成分股的整体强弱，误判个股走势。",
            "用 Nasdaq 100 当日热力图观察市场广度、领导者和拖累者，为复盘提供背景。",
            "13-market-nasdaq.jpg",
            "TradingView Nasdaq 100 当日热力图。该第三方组件需要联网。",
        ),
        (
            "09  市场背景",
            "市场 - S&amp;P 500：检查更广泛的大盘环境",
            "成长股视角可能与整个市场不同，只看 Nasdaq 容易忽略行业轮动和大盘广度。",
            "通过 S&amp;P 500 热力图比较科技、金融、医疗、消费和工业等板块表现。",
            "14-market-sp500.jpg",
            "TradingView S&amp;P 500 当日热力图。",
        ),
        (
            "09  市场背景",
            "市场 - Nasdaq YTD：区分短期波动与年度趋势",
            "单日涨跌容易放大噪声，无法说明年度领导结构是否已经改变。",
            "使用 YTD 热力图识别年度赢家、落后者和持续性的主题分化。",
            "15-market-nasdaq-ytd.jpg",
            "TradingView Nasdaq 100 YTD 热力图。",
        ),
        (
            "09  市场背景",
            "市场 - S&amp;P 500 YTD：观察年度行业轮动",
            "组合结果可能来自大盘风格和行业轮动，而不完全是个股选择能力。",
            "从 S&amp;P 500 年度表现检查行业背景，为选股 alpha 判断增加参照。",
            "16-market-sp500-ytd.jpg",
            "TradingView S&amp;P 500 YTD 热力图。",
        ),
        (
            "09  市场背景",
            "市场 - Finviz：打开外部市场地图补充观察",
            "单一热力图的数据维度有限，主动交易者有时需要更多指数、行业和筛选视角。",
            "从 TradeCraft 直接打开 Finviz Map，补充不同市场范围与数据类型的观察。",
            "17-market-finviz.jpg",
            "Finviz S&amp;P 500 Map。该页面属于第三方网站，截图仅用于说明入口。",
        ),
    ]
    for item in screenshots:
        screenshot_page(story, *item)

    story.extend(
        [
            PageBreak(),
            p("10  测评方法", "eyebrow"),
            p("七维风险：把“感觉不好”变成可检查的问题", "h1"),
            p(
                "测评分数代表观察到的过程风险：0 表示较低风险，100 表示较高风险。"
                "它不是人格评分，也不是收益预测。每个结论必须能回到当前数据和交易证据。",
                "body_large",
            ),
            weights_table(),
            Spacer(1, 6 * mm),
            p("证据等级", "h2"),
            bullet("<b>已确认：</b>当前证据足以支持问题存在。"),
            bullet("<b>疑似：</b>出现了模式，但样本或数据覆盖仍不足。"),
            bullet("<b>数据不足：</b>不能升级为结论，需要先补计划、初始风险或历史记录。"),
            bullet("<b>候选优势：</b>盈利为正，但样本、风险覆盖或稳定性尚未达到证明标准。"),
            box(
                "<b>对主动交易者的价值：</b>允许“暂时没有结论”可以减少优势幻觉。"
                "系统不会因为少数盈利样本就把一种做法包装成已经证明的策略。",
                YELLOW,
            ),
        ]
    )

    audit_screens = [
        (
            "11  交易测评",
            "测评 - 总览：先处理最重要的问题",
            "测评信息很多，如果没有优先级，用户容易停留在看分数而不采取行动。",
            "同时展示结果、过程、行为、可信度和前三个问题，并把证据、判断和铁律入口放在同一页。",
            "18-audit-overview.jpg",
            "测评总览：四类记分卡、优先发现、数据可信度和当前铁律。",
        ),
        (
            "11  交易测评",
            "测评 - 收益归因：区分市场 beta、选股和 setup",
            "账户赚钱或亏钱并不能说明来自市场环境、选股能力还是某类 setup。",
            "将账户 TWR、主/参考基准、相对表现、setup 结果和主要盈亏来源放在一起。",
            "19-audit-outcome.jpg",
            "收益归因：账户回报、基准、相对表现、setup 表现和主要盈亏来源。",
        ),
        (
            "11  交易测评",
            "测评 - 过程质量：检查七维风险",
            "只复盘亏损交易会忽略那些“结果赚钱但过程失控”的风险行为。",
            "从交易摩擦、选股、入场、尾部风险、主题、叙事和退出七个维度检查过程。",
            "20-audit-process.jpg",
            "过程质量：七维风险、退出质量和全部系统发现。",
        ),
        (
            "11  交易测评",
            "测评 - 行为模式：比较最近 20 日与此前 20 日",
            "行为变化通常是渐进的，交易者很难仅凭记忆发现自己最近更频繁、更短线或更激进。",
            "比较两个 20 个活跃交易日窗口，观察成交频率、持有期、仓位和胜率等变化。",
            "21-audit-behavior.jpg",
            "行为模式：最近窗口与前一窗口的指标变化和结果对比。",
        ),
        (
            "11  交易测评",
            "测评 - 交易证据：让每个发现可以被质疑",
            "如果系统只给结论不给证据，用户无法判断结论是否适用于真实语境。",
            "将发现下钻到完整 BUY/SELL 回合、关联 fill、持有天数和盈亏，并允许补录交易计划。",
            "22-audit-evidence.jpg",
            "交易证据：与当前发现相关的完整合成交易回合列表。",
        ),
        (
            "11  交易测评",
            "测评 - 改进记录：把复盘变成未来规则",
            "很多复盘止于一句“下次注意”，没有目标、时间窗口和后续验证。",
            "记录用户确认/驳回，并把问题转成未来 20 个交易日的铁律，显示基线、目标和进度。",
            "23-audit-improvement.jpg",
            "改进记录：Demo 铁律周期和用户判断记录。规则与数据均为合成演示。",
        ),
        (
            "11  交易测评",
            "测评 - AI 总结：压缩信息，不替代底层计算",
            "工作台信息密度高，用户需要一份能够快速回顾重点和下一步动作的摘要。",
            "AI 基于当前快照整理执行摘要、优先核查、候选优势、动作和数据边界；底层事实仍由确定性管线负责。",
            "24-audit-ai.jpg",
            "Demo AI：根据当前随机合成快照离线生成的中文总结。",
        ),
    ]
    for item in audit_screens:
        screenshot_page(story, *item)

    screenshot_page(
        story,
        "12  系统设置",
        "设置：让复盘标准保持一致",
        "默认区间、基准、语言和刷新方式不一致，会让不同时间的复盘口径发生漂移。",
        "集中管理语言、区间、默认股票、行情间隔、主题/测评基准、数据更新和 Watchlist 触发器。",
        "08-settings.jpg",
        "设置：语言、默认值、Kimi 模型、基准、数据导入和 Watchlist 触发器。",
    )

    story.extend(
        [
            PageBreak(),
            p("13  推荐使用方式", "eyebrow"),
            p("把 TradeCraft 变成稳定的复盘习惯", "h1"),
            p("每个交易日", "h2"),
            bullet("首页：确认数据截止日、净值、回撤、股票敞口和复盘优先级。"),
            bullet("复盘：选择最重要的 1-3 个交易，检查成交点、当时计划和实际执行。"),
            bullet("测评：处理一个高优先级发现，必要时补充证据或设为铁律。"),
            p("每周", "h2"),
            bullet("数据：查看成交金额、盈亏热力图和交易行为图，识别注意力与交易密度。"),
            bullet("自选：清理不再跟踪的候选，更新分组、颜色和等待条件。"),
            bullet("市场：比较当日广度与 YTD 结构，避免把 beta 误认为个股能力。"),
            p("每月", "h2"),
            bullet("业绩：检查净值路径、月收益、累计收益和回撤恢复。"),
            bullet("测评：复核七维风险、候选优势和已经完成的 20 日铁律周期。"),
            p("导入真实数据", "h2"),
            bullet("先退出 Demo，再在“设置 - 数据更新”上传 IBKR 文件。"),
            bullet("刷新完成后先检查首页数据新鲜度，再解释收益和风险。"),
            Spacer(1, 7 * mm),
            box(
                "<b>最终目标：</b>TradeCraft 不是让交易者每天产生更多观点，"
                "而是让重要决策有记录、重要结论有证据、重复错误有追踪、候选优势有验证。",
                YELLOW,
            ),
            PageBreak(),
            Spacer(1, 38 * mm),
            p("TradeCraft", "cover_title"),
            p("交易复盘系统", "cover_title"),
            p("为严肃交易者打造的交易复盘系统。", "cover_tagline"),
            p("知己，才能善战。", "cover_subtitle"),
            Spacer(1, 14 * mm),
            ColorBar(177 * mm, 6),
            Spacer(1, 14 * mm),
            box(
                "<b>产品边界：</b>TradeCraft 不登录券商、不下单、不卖信号、不预测市场。"
                "它帮助主动交易者复盘自己的数据与决策，不构成投资建议。"
            ),
            Spacer(1, 18 * mm),
            p("Apache-2.0  |  TradeCraft contributors", "cover_subtitle"),
        ]
    )
    return story


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=17 * mm,
        bottomMargin=15 * mm,
        title="TradeCraft 系统功能手册（用户版）",
        author="TradeCraft contributors",
        subject="基于用户任务与复盘闭环的 TradeCraft 图文功能手册",
    )
    doc.build(build_story(), onFirstPage=page_decor, onLaterPages=page_decor)
    print(OUTPUT)


if __name__ == "__main__":
    main()
