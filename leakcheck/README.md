# leakcheck：敏感内容包含检测（纯文本算法）

给"待上传的明文里有没有本地敏感内容"这个问题的一个算法实现。

**输入**：① 敏感内容（id + 文本）　② 一条待上传明文
**输出**：命中了哪一份、哪一段、多长、可以复核的片段

零第三方依赖，只要 Python 3.9+。

---

## 四层结构

| 层 | 文件 | 干什么 |
|---|---|---|
| ⓪ 入口 | `pathpolicy.py` `loader.py` | 按**路径**判断敏不敏感、属于哪类；合格的文件读成文本 |
| ① 决策 | `decision.py` | **路径为主判据，内容做二次确认**（或独立的内容模式） |
| ② 预处理 | `normalize.py` | 空白折叠 / 全角转半角 / 去零宽字符。**两侧必须共用**，否则指纹对不上 |
| ③ 索引 | `fingerprint.py` `ahocorasick.py` `index.py` | 长内容 → k-gram 指纹 + 倒排表；短内容 → AC 自动机 |
| ④ 比对 | `align.py` `verify.py` `score.py` `matcher.py` | 查表 → 对角线聚合 → 逐字符校验 → 两条规则判定 |
| ⑤ 评测 | `eval/` | 合成数据集 / P·R·F1 / 基线对比 |

`matcher.py` 是算法门面（`add()` / `match()`），`decision.py` 是**产品门面**（`by_path()` / `by_content()`）。

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

```bash
# A. 只判路径（最快，不需要索引）
python -m leakcheck judge --path "D:\公司法务\2024服务合同.docx"

# B. 路径 + 内容二次确认（需要索引）
python -m leakcheck judge --index company.idx --path "D:\公司法务\2024服务合同.docx" --file prompt.txt

# C. 批量：整个目录按路径判定（例如"这个目录要被上传"）
python -m leakcheck judge --dir "D:\公司资料"
```

## 路径策略（入口过滤）

"是否敏感可通过路径判断"——这一层把"全盘扫描"降级成"只看路径像敏感文件的那些"。

```bash
# 只判定、不读内容，用来调规则 / 排查"为什么没进索引"
python -m leakcheck paths --path "D:\公司法务\2024服务合同.docx" --path "C:\Windows\a.docx"

# 按策略扫描目录建索引
python -m leakcheck build --dir "D:\公司资料" -o company.idx

# 用关键词表（最省事的改法，纯文本，空格分隔）
python -m leakcheck paths --keywords 关键词.txt --path "D:\招标\2024标书.docx"
python -m leakcheck build --dir "D:\公司资料" --keywords 关键词.txt -o company.idx

# 用自己的规则（JSON）
python -m leakcheck build --dir "D:\公司资料" --policy my_rules.json -o company.idx
```

### 关键词表（推荐给业务人员）

`关键词.txt` 就是纯文本，一行一组，方括号里是类别，后面空格分隔关键词：

```
# 注释行
[contract] 合同 协议 法务 律师
[design] 图纸 施工图 招标 标书
财务 报表 流水 薪酬
```

- 同类别的多行会自动合并；
- 没有方括号的行归到默认类别 `keyword`；
- 关键词按"路径段包含"匹配（同内置规则的 segments）。

**优先级**：排除规则 → 关键词规则 → 内置包含规则。
也就是说，关键词能把内置的"合同/代码"等判定提前，但**顶不掉** `C:\Windows`、`node_modules` 这类排除。

**优先级**：`exclude` 规则最先（系统目录、node_modules、缓存），然后按规则顺序首个命中生效。
**排除用"整段相等"**（`template` 不会被 `temp` 误伤），**包含用子串**（`2024服务合同` 能被 `合同` 命中）。

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

**密钥文件的粒度**：`.env` 会按 `KEY=VALUE` 拆成多条（`prod.env#DB_PASSWORD`），
因为真实泄露通常是单独一个密码值，而不是整行——这就是"索引粒度 = 想保护的粒度"。

## 快速开始

### Windows 双击（推荐给非开发同学）

双击项目根目录的 `启动.bat`，会出现中文菜单：

```
[1] 演示            [2] 建索引·目录     [3] 建索引·清单
[4] 路径判定        [5] 判定           [6] 内容比对
[7] 评测            [8] 单元测试        [9] 管理关键词     [0] 退出
```

菜单里每一步都会告诉你「作用 / 示例」，只需要输路径或文件名，命令由菜单自动拼接。
关键词可以直接在 `[9]` 里增删，改完保存即可生效（记得重建索引）。

### 命令行

```bash
# 1. 内置演示（不用准备数据）
python -m leakcheck demo

# 2. 给敏感内容建索引（JSONL：每行 {id, text, tag}）
python -m leakcheck build --docs leakcheck/examples/docs.jsonl -o demo.idx

# 3. 检测一条明文
python -m leakcheck match --index demo.idx --file leakcheck/examples/prompt.txt
python -m leakcheck match --index demo.idx --text "第二条 合同金额：…"
python -m leakcheck match --index demo.idx --jsonl batch.jsonl --json

# 4. 跑评测（P/R/F1 + 基线对比）
python -m leakcheck eval

# 5. 测试
python -m unittest discover -s leakcheck/tests -t .
```

`examples/docs.jsonl` 里演示了三种粒度：整篇合同（长）、一段代码（长）、一个密码值（短）。

## 输入格式

**敏感内容（docs.jsonl）**，一行一条：

```json
{"id": "服务合同_001", "tag": "contract", "text": "第一条 合作内容：……"}
```

`id` 要唯一；`tag` 只用于展示。**索引粒度 = 你想保护的粒度**：
整篇入库能回答"他抄了哪一段"，密钥值单独入库才能回答"这个值泄露了"。

**待检测明文**：命令行字符串、一个 txt 文件、或批量 JSONL（每行 `{id, text}`）。

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
合计 7 条正样本全部命中，3 条负样本全部放行：P=R=F1=1.000
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
