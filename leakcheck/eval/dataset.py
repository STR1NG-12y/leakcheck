"""④ 评测（一）：造可复现的文本数据集。

重要约定：**索引粒度 = 你想保护的粒度**。

    - 合同整篇入库  → 能回答"他抄了哪一段"
    - 密钥值单独入库 → 能回答"这个值泄露出去了"

所以这里把 .env 拆成"一个值一条"，而不是整篇入库——这也是真实集成时上游该做的事。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

SURNAMES = ["北京星辰", "上海云帆", "深圳鲸落", "杭州青梧", "广州南栀", "成都拾光"]
SERVICES = ["数据中台建设与运维", "客户画像系统开发", "智能客服平台实施", "供应链系统升级"]
CUSTOMERS = ["张伟", "李娜", "王强", "刘洋", "陈静", "杨帆", "赵敏", "周涛"]
CITIES = ["北京", "上海", "深圳", "杭州", "广州", "成都"]


def _token(rng: random.Random) -> str:
    return f"{rng.randint(0x100000, 0xFFFFFF):06X}"


def make_contract(rng: random.Random) -> list[str]:
    a, b = rng.sample(SURNAMES, 2)
    service = rng.choice(SERVICES)
    months = rng.choice([12, 24, 36, 48])
    amount = rng.randint(80, 3000)
    ratio = rng.choice([10, 15, 20, 30])
    token = _token(rng)
    return [
        f"甲方：{a}科技有限公司（以下简称甲方）",
        f"乙方：{b}信息技术有限公司（以下简称乙方）",
        f"第一条 合作内容：乙方为甲方提供{service}，服务期限为{months}个月，"
        f"自双方签字盖章之日起计算。",
        f"第二条 合同金额：本合同总金额为人民币{amount}万元整，含税价，分四期支付。",
        f"第三条 验收标准：以甲方业务部门出具的验收报告为准，"
        f"乙方应在收到验收通知后十个工作日内完成整改。",
        f"第四条 保密条款：双方对合作中知悉的商业秘密、技术资料、客户名单负有保密义务。",
        f"第五条 违约责任：任何一方违约，应向守约方支付合同总额百分之{ratio}的违约金。",
        f"第六条 本次合作的专属项目编号为{token}，双方后续一切函件均以此编号为准。",
    ]


def make_code(rng: random.Random) -> str:
    name = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6))
    return "\n".join([
        '"""内部风控引擎，请勿外传。"""',
        "import hashlib",
        "",
        f"def {name}(payload: dict) -> dict:",
        '    """核心评分入口。"""',
        "    score = int(hashlib.md5(str(payload).encode()).hexdigest()[:6], 16) % 100",
        f"    if score > {rng.randint(60, 90)}:",
        "        return {'risk': 'high', 'score': score}",
        "    return {'risk': 'low', 'score': score}",
    ])


def make_csv(rng: random.Random) -> str:
    rows = ["客户姓名,手机号,所在城市,年消费额"]
    for _ in range(rng.randint(12, 30)):
        rows.append(",".join([
            rng.choice(CUSTOMERS),
            f"1{rng.choice('3578')}{rng.randint(10 ** 8, 10 ** 9 - 1)}",
            rng.choice(CITIES),
            str(rng.randint(1000, 900000)),
        ]))
    return "\n".join(rows)


@dataclass
class Dataset:
    units: list[tuple[str, str, str]] = field(default_factory=list)   # (id, tag, text)
    payloads: list[dict] = field(default_factory=list)               # {id,kind,expect,label,text}


def _mangle(text: str) -> str:
    """只做格式级篡改：全角空格、全角标点、零宽字符。归一化应当吃掉这些。"""
    out = []
    for i, ch in enumerate(text):
        if ch == " ":
            out.append("\u3000")
        elif ch == ",":
            out.append("，")
        elif ch == ":":
            out.append("：")
        else:
            out.append(ch)
        if i % 7 == 6:
            out.append("\u200b")
    return "".join(out)


def build(docs: int = 20, seed: int = 0) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset()
    contracts: list[tuple[str, list[str]]] = []

    for i in range(max(2, docs // 4)):
        paras = make_contract(rng)
        uid = f"服务合同_{i:03d}"
        ds.units.append((uid, "contract", "\n".join(paras)))
        contracts.append((uid, paras))

    for i in range(max(1, docs // 6)):
        ds.units.append((f"module_{i:03d}.py", "code", make_code(rng)))

    for i in range(max(1, docs // 8)):
        ds.units.append((f"客户名单_{i:03d}.csv", "data", make_csv(rng)))

    # 密钥按"值"入库，而不是整篇 .env —— 索引粒度决定能保护到什么粒度
    secret_value = f"Zx{rng.randint(10 ** 5, 10 ** 6 - 1)}kQ7mLp"
    ds.units.append((f"prod.env/DB_PASSWORD", "secret", secret_value))
    api_key = f"sk-proj-{_token(rng).lower()}{_token(rng).lower()}"
    ds.units.append((f"prod.env/OPENAI_API_KEY", "secret", api_key))

    def add(kind: str, expect: str, label: str | None, text: str) -> None:
        ds.payloads.append({"id": f"{kind}-{len(ds.payloads) + 1:02d}", "kind": kind,
                            "expect": expect, "label": label, "text": text})

    cid, paras = contracts[0]
    add("full_text", "hit", cid, "\n".join(paras))
    add("paste_para", "hit", cid, "\n".join(paras[2:5]))
    add("paste_sentence", "hit", cid, paras[3])
    add("short", "miss", cid, paras[3][:16])          # 短于 k+w-1，预期漏检
    add("mangled", "hit", cid, _mangle("\n".join(paras[2:5])))
    add("secret_short", "hit", "prod.env/DB_PASSWORD", f"这个库密码是 {secret_value}，帮我连一下")
    add("secret_long", "hit", "prod.env/OPENAI_API_KEY", f"key 配好了：{api_key}")

    code_unit = next((u for u in ds.units if u[1] == "code"), None)
    if code_unit:
        add("code_snippet", "hit", code_unit[0],
            "\n".join(code_unit[2].splitlines()[3:8]))

    add("negative_chat", "clean", None, "帮我写一首关于秋天的七言绝句，要求押平声韵。")
    add("negative_topic", "clean", None,
        "起草一份软件采购合同的保密条款，要求包含保密期限、违约责任和争议解决方式。")
    add("negative_topic2", "clean", None,
        "我们公司想和供应商签合作协议，一般需要约定哪些关键条款？请按重要性排序。")
    return ds
