# leakcheck：敏感内容外发检测

判断"待上传的明文里，有没有本地敏感文件的内容"。

- **输入**：① 敏感内容（路径 + 文本）　② 一条待上传明文
- **输出**：命中了哪一份、哪一段、有多长、可以复核的片段
- **环境**：Python 3.9+，**零第三方依赖**（只用标准库）

Windows 用户双击 `启动.bat` 就能用，不需要命令行。

---

## 项目结构

```
leakcheck-tool/
├── 启动.bat            双击入口（纯 ASCII，只做“切目录 + 调 Python”）
├── 关键词.txt          判定用的关键词表（可直接编辑，也可在菜单 [9] 里增删）
├── 我的规则.json       进阶规则示例（--policy 用；需要排除/分级时用）
├── README.md           本文档
├── LICENSE             MIT
└── leakcheck/          代码包（只放 .py）
    ├── menu.py         交互菜单（中文界面在这里）
    ├── cli.py          命令行 7 个子命令
    ├── pathpolicy.py   路径规则：9 条内置 + 关键词表
    ├── loader.py       目录 → 文本；密钥按值拆分
    ├── normalize.py    归一化（两侧共用）
    ├── fingerprint.py  滚动哈希 + winnowing
    ├── ahocorasick.py  短内容精确匹配
    ├── index.py        倒排索引 + 持久化
    ├── align.py        对角线聚合
    ├── verify.py       逐字符校验 + 边界精修
    ├── score.py        两条规则打分判定
    ├── matcher.py      算法门面（add / match）
    ├── decision.py     决策层（路径 + 内容 → 结论）
    ├── config.py       全部参数
    ├── types.py        数据结构
    ├── eval/           评测：数据集、指标、基线、规模实验
    ├── tests/          108 个单元测试
    └── examples/       可直接跑的示例输入
```

**为什么启动器和数据在包外面？**

`leakcheck/` 是 Python 包，`python -m leakcheck` 要求"当前目录是包的父目录"；而且
`pip install` 之后包会进 `site-packages`（通常只读），放里面的关键词表既改不了、升级时还会被覆盖。
所以：**代码放包里，入口和用户数据放包外**。

## Windows 一键启动

双击 `启动.bat`：

```
==========================================================
    leakcheck    敏感内容外发检测
    路径为主判据   +   内容二次确认
==========================================================

  目录     D:\工具\leakcheck-tool
  Python   3.13.14
  索引     未建立（先跑 2 或 3）
  关键词   关键词.txt（已找到）
```

| 选项 | 做什么 | 你要输入 |
|---|---|---|
| `[1]` 演示 | 用 6 条模拟的聊天输入跑一遍 | 无 |
| `[2]` 建索引·目录 | 扫目录，按规则筛出敏感文件建索引 | 目录路径 |
| `[3]` 建索引·清单 | 按清单强制收录（规则没命中的也收） | 清单文件 |
| `[4]` 路径判定 | 只判路径，不读内容 | 文件路径 |
| `[5]` 判定 | 路径 + 内容二次确认 | 文件路径（+ 可选明文文件） |
| `[6]` 内容比对 | 只给明文，不给路径 | 明文文件 |
| `[7]` 评测 | 出 P/R/F1 与基线对比 | 无 |
| `[8]` 单元测试 | 跑 108 个用例 | 无 |
| `[9]` 管理关键词 | 查看 / 添加 / 删除关键词 | 词 |
| `[0]` 退出 | | |

**你只需要输路径或文件名**，命令由菜单自动拼。例如选 `[2]` 只输 `D:\公司资料`，
菜单内部执行的是：

```
python -m leakcheck build --dir "D:\公司资料" -o leakcheck.idx
```

索引文件名固定为 `leakcheck.idx`（放在项目根目录），`[5] [6]` 读的也是它。

> `启动.bat` 里没有任何中文——cmd 是按字节解析批处理的，中文会因读块边界错位而
> 出现"半行被当成命令"。中文界面全部在 `leakcheck/menu.py` 里，由 Python 输出。

## 判定策略：路径为主，内容确认

```
                    内容二次确认通过          内容未比对 / 未命中
路径判定敏感        确认泄露 confirmed        疑似 suspected
路径判定不敏感      规则缺口 confirmed*       放行 clean
```

- **路径**决定"该不该保护"：便宜、可解释、可审计，是主证据。
- **内容**决定"到底传没传"：贵，但能给出"哪一段"的铁证。
- `confirmed*` 是策略漏洞：内容命中了，但路径规则没覆盖这个文件 → 结论仍是泄露，同时提示补规则。
- `suspected` 只有路径证据（文件被访问/上传，但索引里没有可比的文本）→ 交人工，**不要当放行**。

## 路径策略

"是否敏感可通过路径判断"——这一层把"全盘扫描"降级成"只看路径像敏感文件的那些"。

内置 9 条规则，按顺序匹配，**首个命中即生效**：

| 规则 | 类别 | 级别 | 命中方式 |
|---|---|---|---|
| 排除-系统目录 | excluded | – | 前缀 `c:/windows`、`c:/program files` |
| 排除-开发依赖 | excluded | – | 路径段 `node_modules`、`.git`、`__pycache__` |
| 排除-临时缓存 | excluded | – | 路径段 `temp`、`tmp`、`cache` |
| 密钥 | secret | high | 文件名 `.env`/`id_rsa`、扩展名 `.pem`/`.key`、目录 `密钥`/`.ssh` |
| 人事隐私 | hr | high | 目录 `人事`/`简历`/`招聘`、文件名 `*简历*` |
| 财务数据 | data | high | 扩展名 `.csv`/`.xlsx`、目录 `财务`/`客户`/`名单` |
| 合同 | contract | high | 扩展名 `.docx`/`.pdf`、文件名 `*合同*`/`*协议*`、目录 `法务` |
| 源代码 | code | medium | 扩展名 `.py`/`.java`/…、目录 `src`/`源码` |
| 设计文档 | design | medium | 扩展名 `.psd`/`.fig`、目录 `设计`/`原型` |

**优先级**：排除规则 → 关键词规则 → 内置包含规则。
关键词能提前内置判定，但**顶不掉** `C:\Windows`、`node_modules` 这类排除。

**两个匹配细节**：排除用"整段相等"（`template` 不会被 `temp` 误伤），
包含用"子串"（`2024服务合同` 能被 `合同` 命中）。

### 关键词表（日常改这个就够）

`关键词.txt` 是纯文本，一行一组，方括号里是类别：

```
# 注释行
[contract] 合同 协议 法务 律师
[design] 图纸 施工图 招标 标书
财务 报表 流水 薪酬
```

- 同类别的多行自动合并；没有方括号的归到 `keyword` 类别；
- 关键词按"路径段包含"匹配；
- 改完**记得重建索引**（`[2]`），因为进索引的文件集合变了。

三种改法：菜单 `[9]`（推荐）、记事本直接编辑、或 `--keywords 别的文件`。

### 进阶：JSON 规则（`我的规则.json`）

关键词表只能"加敏感词"，不能"加排除"、也不能给不同类别设不同级别。这些用 JSON：

```json
{
  "base": "default",
  "rules": [
    {"name": "排除-备份盘", "exclude": true, "prefixes": ["e:/backup"]},
    {"name": "公司法务", "tag": "contract", "severity": "high",
     "segments": ["公司法务", "法务部", "合同归档"]}
  ]
}
```

- `"base": "default"` = 在 9 条内置规则基础上**追加**；不写则是**完全替换**。
- 可用字段：`prefixes`（目录前缀）、`names`（精确文件名）、`globs`（通配）、
  `extensions`（扩展名）、`segments`（路径段关键词）、`exclude`、`tag`、`severity`。
- 用法：`--policy 我的规则.json`（可与 `--keywords` 同时用）。

## 命令行

```bash
# 演示（不用准备数据）
python -m leakcheck demo

# 建索引：目录 / 清单 / 已有 JSONL
python -m leakcheck build --dir "D:\公司资料" -o company.idx
python -m leakcheck build --from-list 清单.txt -o company.idx
python -m leakcheck build --docs leakcheck/examples/docs.jsonl -o demo.idx

# 判定（路径为主 + 内容确认）
python -m leakcheck judge --index company.idx --path "D:\a\合同.docx" --file prompt.txt
python -m leakcheck judge --path "D:\a\合同.docx"          # 只判路径，不用索引

# 纯内容比对（没有路径信息时）
python -m leakcheck match --index company.idx --text "帮我润色：……"
python -m leakcheck match --index company.idx --jsonl batch.jsonl --json

# 只判路径（调规则用）
python -m leakcheck paths --keywords 关键词.txt --path "D:\招标\2024标书.docx" --path "C:\Windows\a.docx"

# 评测与压测
python -m leakcheck eval
python -m leakcheck bench --sizes 300,1000,3000

# 测试
python -m unittest discover -s leakcheck/tests -t .
```

## 输入格式

**敏感内容（docs.jsonl）**，一行一条：

```json
{"id": "服务合同_001", "tag": "contract", "text": "第一条 合作内容：……"}
```

`id` 唯一；`tag` 只用于展示。**索引粒度 = 你想保护的粒度**：
整篇入库能回答"他抄了哪一段"，密钥值单独入库才能回答"这个值泄露了"。

**待检测明文**：命令行字符串、一个 txt 文件（UTF-8/GBK/UTF-16 均可），或批量 JSONL（每行 `{id, text}`）。

## Python API

```python
from leakcheck import Matcher, Config

m = Matcher(Config())
m.add("合同A", "第一条 合作内容：……", "contract")
m.add("密码", "Zx7Qm2Lp9Kw4", "secret")     # 12 字 → 自动走精确支路

for hit in m.match("帮我润色：第二条 合同金额：……"):
    print(hit.doc_id, hit.detector, hit.longest_run, hit.reason)

m.save("index.pkl"); m2 = Matcher.load("index.pkl")
```

## 输出字段

| 字段 | 含义 |
|---|---|
| `doc_id` / `tag` | 命中的敏感内容及其类别 |
| `detector` | `text`（指纹支路）/ `short`（精确支路） |
| `longest_run` | 最长连续命中字数，**最可信的证据** |
| `coverage_payload` | 命中占明文的比例 |
| `spans` | 文档区间 + 明文区间，用于截片段复核 |
| `score` | 0~1 排序分，**不是概率** |

## 参数

| 参数 | 默认 | 作用 | 调大 | 调小 |
|---|---|---|---|---|
| `k` | 16 | 指纹窗口 | 误报降、短片段漏 | 反之 |
| `w` | 8 | 抽稀窗口 | 索引小、灵敏度降 | 反之 |
| `min_run` | 32 | 告警最短连续长度 | 误报降 | 更敏感 |
| `coverage` | 0.30 | 覆盖率门槛 | 更严 | 更松 |

硬约束：`min_run >= k + w - 1`（默认 32 ≥ 23）。`k+w-1` 是算法**保证能检出**的最短公共子串长度。

## 实测（`python -m leakcheck eval`，默认规模）

```
语料：12 份 / 3806 字符 / 800 指纹 / 2 条短模式
7 条正样本全部命中，3 条负样本全部放行：P=R=F1=1.000
top-1 定位准确率 7/7
边界样本（16 字，短于 23）：0/1 被检出 —— 设计边界，不是 bug
基线：bruteforce R=1.000（但成本随语料线性增长）、simhash R=0.143
```

## 限制

1. **改写/翻译检不出**：精确匹配路线的本质边界。
2. **短于 `k+w-1` 的片段不检**（默认 23 字），除非它被单独登记成短内容。
3. **模板化文本会命中多份**：合同套话在多份文件里都有，top-1 通常仍正确。
4. **不做归一化以外的语义处理**：改词、换语序、翻译都会漏。
5. **索引保存原文**：为了命中后校验和展示片段，内存约等于语料大小。
6. **索引文件不要外发**：`*.idx` 里含敏感原文，`.gitignore` 已排除。

## 许可

MIT，见 [LICENSE](LICENSE)。
