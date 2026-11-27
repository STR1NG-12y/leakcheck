# leakcheck：敏感内容外发检测

判断**准备发出去的内容**里，有没有**本地敏感文件的原文或改写**。

- **输入**：① 敏感资料（本地目录 / 文件 / 已建索引）　② 待外发的内容（一段话、一个文件、一个目录、一批 jsonl）
- **输出**：命中了哪份文件、哪一段、有多长、能不能复核
- **环境**：Python 3.9+；核心算法**零第三方依赖**（只用标准库）

Windows 用户解压发布包双击 `leakcheck.exe` 即可，不需要命令行、不需要装 Python。

---

## 能力概览

| 能力 | 说明 |
|---|---|
| **一阶 · 逐字检测** | 连续 23 字相同**数学上必然检出**，给出精确字符区间 |
| **二阶 · 骨架检测** | 去虚词 + 数字归一（`三年`=`36个月`、`1.5万`=`15000`、`壹仟贰佰捌拾万`=`12800000`），能抓同义改写、语序调整 |
| **短内容检测** | API Key / 密码这类短值走 AC 自动机精确匹配 |
| **格式解析** | txt / md / csv / 代码 / **docx / xlsx / pptx / pdf** |
| **表格** | 按「整行 + 单元格」双表示入库，单行数据也能命中 |
| **图片与扫描件** | 图片 OCR；PDF 内嵌图片 OCR；扫描页整页识别 |
| **语义线索** | BGE-small-zh 向量检索，找出"意思相近"的块（**只给线索，不自动阻断**） |
| **收录规则** | 你选中的文件一律算敏感（不再二次筛选）；关键词只用来提示待检测文档的路径 |
| **图形界面** | 六页：建索引 / 路径判定 / 两阶扫描 / 语义筛选 / 关键词 / 设置 |

---

## 快速开始

### 方式一：发布包（推荐给非开发同事）

```
leakcheck\
├── leakcheck.exe         双击打开界面
├── _internal\            运行库，不要动
├── models\               语义模型（内置 bge-small-zh）
├── 我的资料\
│   ├── 敏感文件\         把要保护的资料放这里（放进来就一律收录）
│   ├── 待检测\           把准备发出去的内容放这里
│   └── 索引\             索引文件自动存这里
├── 关键词.txt
└── 使用说明.txt
```

三步走：**拷资料 → 双击 exe → 点两下按钮**（路径已默认填好）。

### 方式二：源码

```bash
git clone https://github.com/STR1NG-12y/leakcheck.git
cd leakcheck/leakcheck-tool
python -m leakcheck.gui          # 图形界面
python -m leakcheck demo         # 命令行演示
python -m unittest discover -s leakcheck/tests -t .   # 152 个单元测试
```

可选依赖（按需装，缺了会提示装哪个）：

```bash
pip install python-docx openpyxl python-pptx   # Office
pip install pdfplumber                          # PDF
pip install rapidocr-onnxruntime opencv-python-headless  # OCR
pip install onnxruntime tokenizers              # 语义筛选
```

---

## 图形界面

| 页 | 作用 |
|---|---|
| ① 建立索引 | 选目录/文件 → 生成 `.idx`（含逐字 + 骨架两层）；只填名字自动存到 `我的资料\索引\` |
| ② 路径判定 | 只看路径该不该保护，不读内容；排查"为什么这个文件没进索引" |
| ③ 两阶扫描 | 主力检测：逐字 → 骨架，支持单文件 / 目录 / `.jsonl` |
| ④ 语义筛选 | 向量找相似块，输出百分比 + 档位 + 片段，**不是证据** |
| ⑤ 关键词 | 直接编辑关键词表，用来标记待检测文档的路径是否敏感 |
| ⑥ 设置 | 程序目录、索引状态、删索引、复制路径 |

③④ 页都有「数据来源」二选一：**用索引**（快）或 **直接扫敏感资料**（不用先建库，慢）。

---

## 命令行

```bash
# 建索引（目录下文件全部收录；--all 保留兼容，早就是默认行为）
python -m leakcheck build --dir 我的资料/敏感文件 --all -o 我的资料/索引/leakcheck

# 两阶扫描：优先用索引
python -m leakcheck scan --index 我的资料/索引/leakcheck.idx --file 待检测.txt
python -m leakcheck scan --dir 我的资料/敏感文件 --file 待检测目录/

# 语义筛选（线索，不是证据）
python -m leakcheck screen --index 我的资料/索引/leakcheck.idx --file 待检测.txt

# 其他
python -m leakcheck paths --path "D:\公司\合同.docx"     # 只看路径
python -m leakcheck judge --path ... --text ...          # 路径 + 内容
python -m leakcheck match --index ... --text ...         # 只做逐字比对
python -m leakcheck demo                                 # 内置 12 条样例
python -m leakcheck eval --docs 20                       # P/R/F1 与基线对比
python -m leakcheck bench --sizes 300,1000,3000          # 规模实验
```

---

## 检测原理

```
待检测内容
   │
   ├─ 归一化（全半角、零宽字符、换行、大小写）
   │
   ├─ 一阶：k=16 窗口 + winnowing(w=8) → 滚动哈希 → 倒排查表
   │        └ 按「位置差恒定」聚成对角线 → 合并成连续片段 → 逐字校验
   │
   └─ 二阶：骨架化（删虚词、数字/单位归一）→ 换个 k=8 再跑同一套
            └ 命中后回原文做 LCS 校验（防"加个不字"）
```

三个关键设计：

1. **23 字保证**：k=16、w=8 时，任意连续 `k+w−1 = 23` 字的相同片段必然被同一窗口覆盖。
2. **对角线聚合**：真抄袭的指纹满足 `文档位置 − 明文位置 = 常数`，零散碰撞不会落进同一条线。
3. **数字归一**：`1.5万` = `15000`、`三年` = `36个月`、`1,280` = `1280`、`-5` ≠ `5`；年份（1900–2100）不按时长换算。

判定规则（满足其一）：

```
最长连续 ≥ 32 字 且 命中指纹 ≥ 3
覆盖率 ≥ 30% 且 最长 ≥ 16 字 且 明文 ≥ 24 字
```

---

## 结果分级

| 级别 | 含义 | 处置 |
|---|---|---|
| **A** | 逐字命中（23 字有数学保证） | 自动阻断 |
| **B** | 骨架命中 / 语义召回后字面对齐成功 | 告警 + 人工确认 |
| **C** | 向量筛选给出的线索，无字面证据 | 仅供人工参考 |
| 放行 | 没发现重合 | — |

结果表每行右侧带**该文件的比对耗时**，状态栏给**总耗时**（含建库/加载索引时间）。批量检测时可据此判断慢在解析还是比对。

---

## 实测数据

| 场景 | 结果 |
|---|---|
| 单元测试 | 152 个全过 |
| 内置样例 | 12 条：9 命中 / 1 放行 / 2 边界 |
| 单条查询（260 字，已建索引） | 0.3 ms |
| 重复表格 5000 行 | 728 ms（限流后，无截断） |
| 图片 OCR | 1~3 秒/页 |
| 语义筛选 | 编码 10 ms/条 + 检索 ms 级 |

---

## 已知限制

- **翻译抓不到**：骨架与向量都基于中文，英文改写只能给 C 级线索。
- **OCR 精度**：扫描件识别有错字，"23 字保证"在 OCR 内容上不成立，只能靠骨架兜底。
- **大文件**：扫描件超过 40 页会截断 OCR 并提示。
- **索引安全**：`.idx` 里是敏感原文副本，**不要外发**。

---

## 项目结构

```
leakcheck-tool/
├── launcher.py          打包入口（双击进 GUI，带参数走命令行）
├── 启动.bat             源码方式启动菜单
├── 打包.bat             一键打包成 exe
├── 使用说明.txt          给最终用户的一页纸
├── 关键词.txt            关键词表（只用于待检测路径提示）
├── README.md
├── LICENSE
└── leakcheck/
    ├── gui.py           Tkinter 图形界面（六页）
    ├── menu.py          控制台菜单
    ├── cli.py           命令行子命令
    ├── cascade.py       两阶级联门面（逐字 → 骨架）
    ├── matcher.py       一阶门面（add / match）
    ├── skeleton.py      二阶骨架（虚词表、数字归一、LCS 校验）
    ├── vector_screen.py 语义筛选（onnx / sentence-transformers / 降级）
    ├── parse/           解析层：blocks / text / office / pdf / ocr / layout
    ├── pathpolicy.py    路径规则
    ├── loader.py        目录 → 文本
    ├── normalize.py     归一化
    ├── fingerprint.py   滚动哈希 + winnowing
    ├── ahocorasick.py   短内容精确匹配
    ├── index.py         倒排索引 + 持久化
    ├── align.py         对角线聚合（带限流）
    ├── verify.py        逐字符校验
    ├── score.py         打分判定
    ├── semantic/        BM25 与带状 SW（实验模块，默认不参与判定）
    ├── eval/            评测：数据集、指标、基线、规模实验
    └── tests/           152 个单元测试
```

---

## 许可证

MIT
