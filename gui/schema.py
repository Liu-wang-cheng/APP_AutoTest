"""动作 schema: GUI 表单生成 / YAML 序列化 / 参数校验的唯一数据源

新增动作只需: 1) ActionRunner 加 _do_xxx + 分发表一条  2) 此处加一条声明
"""

# 字段类型: text 文本 | int 整数 | float 小数 | bool 布尔 | int4 四个整数[x1,y1,x2,y2]
#          group 组合字段(值为字典,子字段全空时退化为标量 True)

# 所有步骤通用的附加字段(修改器)
GENERIC_FIELDS = [
    {"key": "desc", "label": "步骤说明", "type": "text", "hint": "显示在报告和结果面板"},
    {"key": "screenshot", "label": "自动截图", "type": "bool", "hint": "勾选则本步执行后自动截图, 文件名按用例/步骤自动生成"},
    {"key": "wait", "label": "本步后等待(秒)", "type": "int", "hint": "覆盖全局步骤间隔"},
    {"key": "timeout", "label": "超时(秒)", "type": "int", "hint": "0=无限等待;留空用全局默认"},
    {"key": "retry", "label": "失败重试次数", "type": "int", "hint": "默认 0 不重试"},
]

ACTIONS = [
    # ── 操作 ──
    {"key": "click", "label": "点击", "category": "操作", "fields": [
        {"key": "click", "label": "目标", "type": "text", "required": True,
         "hint": "按钮名 / 图片名(.png) / x,y 坐标"},
    ]},
    {"key": "click_template", "label": "点击模板", "category": "操作", "fields": [
        {"key": "click_template", "label": "模板", "type": "template", "required": True,
         "hint": "从当前 APP 组的模板中选择"},
    ]},
    {"key": "long_click", "label": "长按", "category": "操作", "fields": [
        {"key": "long_click", "label": "目标", "type": "text", "required": True,
         "hint": "文本 / 图片名 / [x,y,时长秒]"},
        {"key": "duration", "label": "长按秒数", "type": "int", "hint": "默认 2"},
    ]},
    {"key": "input", "label": "输入文本", "category": "操作", "fields": [
        {"key": "input", "label": "文本内容", "type": "text", "required": True,
         "hint": "需先 click 输入框获得焦点"},
    ]},
    {"key": "back", "label": "按返回键", "category": "操作", "fields": [], "fixed_bool": True},
    {"key": "swipe", "label": "滑动", "category": "操作", "fields": [
        {"key": "swipe", "label": "方向/坐标", "type": "text", "required": True,
         "hint": "left/right/up/down/fast-left/fast-right 或 [sx,sy,ex,ey]"},
    ]},
    {"key": "if_click", "label": "条件点击", "category": "操作", "fields": [
        {"key": "if_click", "label": "候选目标", "type": "text", "required": True,
         "hint": "多个用逗号/顿号/分号分隔(中英文逗号均可),存在哪个点哪个"},
    ]},
    {"key": "find_click", "label": "多选一点击", "category": "操作", "fields": [
        {"key": "find_click", "label": "候选(按优先级)", "type": "text", "required": True,
         "hint": "多个用逗号/顿号/分号分隔(中英文逗号均可);支持图片名;也可用 YAML 列表语法"},
    ]},

    # ── 断言 ──
    {"key": "assert", "label": "断言(文本/图片)", "category": "断言", "fields": [
        {"key": "assert", "label": "期望出现", "type": "text", "required": True,
         "hint": "文本(多个用逗号/顿号分隔, 中英文逗号均可, 任一匹配) / 图片名(.png)"},
    ]},
    {"key": "assert_switch", "label": "断言开关状态", "category": "断言", "fields": [
        {"key": "assert_switch", "label": "期望状态", "type": "text", "required": True,
         "hint": "on/off 或 打开/关闭"},
        {"key": "switch_area", "label": "开关区域", "type": "int4", "hint": "[x1,y1,x2,y2] 留空自动定位"},
    ]},
    {"key": "compare", "label": "图像相似度对比", "category": "断言", "fields": [
        {"key": "compare", "label": "基准图", "type": "stepshot",
         "hint": "选择前面开启了自动截图的步骤, 以其截图为基准"},
        {"key": "threshold", "label": "相似度阈值", "type": "float", "hint": "默认 0.6,低于即失败"},
    ]},
    {"key": "diff", "label": "图像变化检测", "category": "断言", "fields": [
        {"key": "diff", "label": "基准图", "type": "text", "required": True,
         "hint": "与基准图有差异才通过(反相对比)"},
        {"key": "threshold", "label": "相似度阈值", "type": "float", "hint": "默认 0.99,高于即'无变化'失败"},
    ]},

    # ── 数据 ──
    {"key": "grab", "label": "抓取页面数据", "category": "数据", "fields": [
        {"key": "grab", "label": "关键字", "type": "text", "required": True,
         "hint": "多个用逗号/顿号/分号分隔(中英文逗号均可),如 面积,时间"},
    ]},
    {"key": "match", "label": "比对抓取数据", "category": "数据", "fields": [
        {"key": "match", "label": "关键字", "type": "text", "required": True,
         "hint": "与 grab 相同关键字;数值允许 ±1 浮动"},
    ]},
    {"key": "latest_record", "label": "点击最新记录", "category": "数据", "fields": [], "fixed_bool": True},

    # ── 开关 ──
    {"key": "switch_to", "label": "切换开关", "category": "开关", "fields": [
        {"key": "switch_to", "label": "目标状态", "type": "text", "required": True,
         "hint": "on/off 或 打开/关闭;先判断颜色再点击验证"},
        {"key": "switch_tpl", "label": "开关模板图", "type": "text", "hint": "默认 勿扰打开.png"},
        {"key": "switch_label", "label": "同行文本标签", "type": "text", "hint": "如 定制模式,自动定位右侧开关"},
        {"key": "switch_area", "label": "开关区域", "type": "int4", "hint": "[x1,y1,x2,y2] 直接指定"},
    ]},

    # ── 时间 ──
    {"key": "set_time", "label": "设置时间滚轮", "category": "时间", "fields": [
        {"key": "set_time", "label": "偏移分钟数", "type": "int", "required": True,
         "hint": "相对设备当前时间,0=当前时间,可为负"},
        {"key": "circular", "label": "循环滚轮", "type": "bool", "hint": "勾选=最短路径环绕;不勾=纯数值不环绕"},
    ]},
    {"key": "wait_for", "label": "等待元素出现", "category": "时间", "fields": [
        {"key": "wait_for", "label": "目标", "type": "text", "required": True,
         "hint": "文本(多个用逗号/顿号分隔, 中英文逗号均可) / 图片名(.png)"},
    ]},
    {"key": "wait_loading", "label": "等待加载消失", "category": "时间", "fields": [], "fixed_bool": True},

    # ── 地图编辑 ──
    {"key": "room_zones", "label": "识别房间分区", "category": "地图编辑", "fields": [
        {"key": "room_zones", "label": "地图区域", "type": "int4",
         "hint": "[x1,y1,x2,y2] 留空=默认中间区域"},
    ]},
    {"key": "room_click", "label": "点击分区", "category": "地图编辑", "fields": [
        {"key": "room_click", "label": "分区序号", "type": "int", "required": True,
         "default": 1, "hint": "1=第1个;大于分区数=连点剩余全部"},
    ]},
    {"key": "merge_zones", "label": "合并分区", "category": "地图编辑", "fields": [], "fixed_bool": True},
    {"key": "split_zone", "label": "分割分区", "category": "地图编辑", "fields": [], "fixed_bool": True},

    # ── 清扫 ──
    {"key": "spot_clean", "label": "定点清扫", "category": "清扫", "fields": [
        {"key": "spot_clean", "label": "清扫参数", "type": "group",
         "hint": "全部留空=自动识别分区,面积=0 自动换分区重试", "fields": [
            {"key": "mode_btn", "label": "模式入口模板图", "type": "text",
             "hint": "默认 切换清扫模式.png"},
            {"key": "mode_text", "label": "模式选项文本", "type": "text",
             "hint": "默认 指哪扫哪"},
            {"key": "start_btn", "label": "开始按钮模板图", "type": "text",
             "hint": "默认 开始清扫.png"},
            {"key": "collapse_btn", "label": "收起面板模板图", "type": "text",
             "hint": "默认 收起清扫数据.png(箭头朝上)"},
            {"key": "max_rooms", "label": "最多尝试分区数", "type": "int",
             "hint": "默认 3"},
        ]},
    ]},

    # ── 定时 ──
    {"key": "add_timer", "label": "添加定时任务", "category": "定时", "fields": [
        {"key": "add_timer", "label": "定时参数", "type": "group", "hint": "全部留空=自动识别添加按钮", "fields": [
            {"key": "add_text", "label": "添加按钮文本", "type": "text", "hint": "如 添加"},
            {"key": "add_tpl", "label": "添加按钮模板图", "type": "text", "hint": "如 添加预约.png"},
            {"key": "max_tasks", "label": "任务数上限", "type": "int", "hint": "达到上限先删旧任务,默认 2"},
        ]},
    ]},

    # ── 流程控制 ──
    {"key": "if", "label": "条件满足则跳过", "category": "流程控制", "fields": [
        {"key": "if", "label": "条件", "type": "text", "required": True,
         "hint": "文本(多个用逗号/顿号分隔, 中英文逗号均可) / 电量>50 / 图片名 / resource-id;条件成立跳过本步"},
        {"key": "threshold", "label": "相似度阈值", "type": "float",
         "hint": "条件为图片时,填写则用图像对比判断"},
    ], "tip": "条件不成立时执行 else 子步骤;展开卡片后可在下方添加/编辑"},
    {"key": "if not", "label": "条件不满足则跳过", "category": "流程控制", "fields": [
        {"key": "if not", "label": "条件", "type": "text", "required": True,
         "hint": "同「条件满足则跳过」,判断结果取反"},
        {"key": "threshold", "label": "相似度阈值", "type": "float"},
    ], "tip": "条件成立时执行 else 子步骤;展开卡片后可在下方添加/编辑"},
]

ACTION_BY_KEY = {a["key"]: a for a in ACTIONS}
CATEGORY_ORDER = ["操作", "断言", "数据", "开关", "时间", "地图编辑", "定时", "流程控制"]

# 布尔型标量动作序列化时的键值
FIXED_BOOL = {a["key"]: True for a in ACTIONS if a.get("fixed_bool")}


def new_step(action_key):
    """按 schema 生成步骤 dict(必填项给默认值)"""
    action = ACTION_BY_KEY[action_key]
    step = {"desc": action["label"]}
    if action.get("fixed_bool"):
        step[action_key] = True   # ★ 固定布尔动作(back等): 新建即写入动作键
    for f in action["fields"]:
        if f["type"] == "group":
            step[f["key"]] = True  # 默认标量 True = 自动模式;填子参数后表单写成 dict
            continue
        if f.get("required") and f["type"] == "text":
            step[f["key"]] = ""
        elif f["type"] == "float":
            step[f["key"]] = f.get("default", 0.6)
        elif f["type"] == "int4":
            step[f["key"]] = []  # 序列化时空列表按动作语义处理(room_zones→True,其他省略)
        elif f["type"] == "int" and (f.get("required") or "default" in f):
            step[f["key"]] = f.get("default", 0)   # 必填/有默认值的 int 写入
        elif f["type"] == "stepshot":
            step[f["key"]] = ""   # 基准图引用: 空=用户尚未在下拉中选择
        # 可选 int/bool 默认缺省(序列化时省略)
    # ★ 动作键必须存在于步骤(否则卡片显示「未知」、引擎执行无动作):
    #   动作键与字段同名但未被上面分支写入时, 按字段类型给兜底默认
    if action["key"] not in step:
        for f in action["fields"]:
            if f["key"] == action["key"]:
                t = f["type"]
                if t == "text":
                    step[action["key"]] = ""
                elif t == "int":
                    step[action["key"]] = f.get("default", 0)
                elif t == "float":
                    step[action["key"]] = f.get("default", 0.6)
                elif t == "bool" or t == "group" or t == "stepshot":
                    step[action["key"]] = ""
                else:
                    step[action["key"]] = True
                break
        else:
            step[action["key"]] = True   # 无同名字段(理论不发生): 标量 True
    return step


def serialize_step(step):
    """清理步骤 dict: 空值/默认 False 的可选布尔省略,int4 空列表按动作语义处理"""
    out = {}
    for k, v in step.items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                # ★ 动作键即使为空也保留(否则卡片显示「未知」、引擎丢动作);
                #   非动作键的空串照旧省略
                if k in ACTION_BY_KEY:
                    out[k] = ""
                continue
            out[k] = v
            continue
        if k == "else" and isinstance(v, list):  # else 子步骤递归清理(必须在 int4 分支前)
            out[k] = [serialize_step(s) for s in v]
            continue
        if isinstance(v, list):  # int4
            if v:
                out[k] = v
            elif k == "room_zones":
                out[k] = True  # 空区域 = 用默认区域
            continue
        if isinstance(v, dict):  # 组字段: 清掉空子项,清完退化为 True(自动模式)
            cleaned = {dk: dv for dk, dv in v.items() if dv not in (None, "")}
            out[k] = cleaned if cleaned else True
            continue
        if v is False:
            continue  # 可选布尔 False 省略(engine 默认即 False)
        out[k] = v
    # fixed_bool 动作保证标量 True
    if not out:
        return out
    for key, val in FIXED_BOOL.items():
        if key in out and not isinstance(out[key], (bool, int, float, list, dict)):
            out[key] = True
    return out


def validate_step(step):
    """返回错误信息列表(空列表=通过);else 子步骤递归校验"""
    errors = []
    action = None
    for key in step:
        if key in ACTION_BY_KEY:
            action = ACTION_BY_KEY[key]
            break
    if action is None:
        return [f"未识别的动作: {list(step)}"]
    for f in action["fields"]:
        if f["type"] == "group":
            continue
        if f.get("required") and not str(step.get(f["key"], "")).strip():
            errors.append(f"「{f['label']}」必填")
    if errors and action.get("tip"):
        errors.append(action["tip"])
    for i, sub in enumerate(step.get("else") or []):
        if isinstance(sub, dict):
            for err in validate_step(sub):
                errors.append(f"else子步骤{i + 1}: {err}")
    return errors


def step_summary(step):
    """生成步骤卡片的一行摘要(优先显示步骤说明 desc)"""
    action_key = None
    for key in step:
        if key in ACTION_BY_KEY:
            action_key = key
            break
    if action_key is None:
        if "wait" in step and len(step) <= 2:
            return f"延时等待 {step['wait']} 秒"
        return str(step)
    action = ACTION_BY_KEY[action_key]
    val = step[action_key]
    if isinstance(val, dict):
        val = ",".join(f"{k}={v}" for k, v in val.items() if v not in (None, ""))
    elif isinstance(val, list):
        val = f"[{','.join(map(str, val))}]"
    # ★ chip(彩色标签)已经显示动作名, summary 只显示「具体参数」, 不再重复
    detail = "" if val in (True, "") else str(val)
    desc = str(step.get("desc", "")).strip()
    if desc and desc != action["label"]:
        main = f"{desc} · {detail}" if detail else desc
    else:
        main = detail
    badges = []
    else_items = step.get("else")
    if isinstance(else_items, list) and else_items:
        badges.append(f"▸else {len(else_items)}步")
    if step.get("screenshot"):
        badges.append("📷")
    if step.get("wait"):
        badges.append(f"+{step['wait']}s")
    if step.get("retry"):
        badges.append(f"↻{step['retry']}")
    if step.get("timeout"):
        badges.append(f"⏱{step['timeout']}s")
    if badges:
        main += "  " + " ".join(badges)
    return main
