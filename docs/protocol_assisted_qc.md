# 协议规则复用与自动质量筛查

此补丁面向现有 **NIfTI-only** inventory。它保留原始体素切片、原有人工决定和独立
BIDS 目的地。不扫描 DICOM、不重新复制旧 BIDS、不修改源影像、不自动执行归档。
代码与合成测试不代表真实队列验证；当前未宣称达到 5% 错误放行率。

## 安装到当前环境

先保存正在编辑的病例，再用 Ctrl+C 停止阅片器。不要热替换运行中的代码。
在已有项目目录中执行以下命令；如有本地代码修改，先自行保存，不要强制覆盖。

```bash
git fetch origin
git switch --track origin/feat/protocol-assisted-qc
python -m pip install -e '.[qc-assist]'
python qc_assist.py --help
```

已有该本地分支时用 `git switch feat/protocol-assisted-qc` 和 `git pull --ff-only`。
无需新建 Conda 环境。可选依赖是 scikit-learn、SciPy 和 threadpoolctl。
不安装这些依赖也可以继续人工阅片与协议识别，但不能训练质量模型。

若使用独立 clone，请复制已有的本地配置，指向**同一个私有 audit 和 staging**，
不要运行初始化 inventory 的命令。同一份审计数据不要同时运行两个阅片器。

```bash
git clone --single-branch --branch feat/protocol-assisted-qc \
  https://github.com/zhenzonglin/gb-dicom2bids.git gb-dicom2bids-assisted-qc
```

下文假定配置为 `config/config.nifti.local.yaml`。它继续被 Git 忽略。
升级前保留私有 `visual_qc/` 的可恢复备份；不要将该备份放入公共仓库。

## 第一步：协议识别

当前流程严格分为**序列识别 → 质量检查 → 单独归档**。更新代码后，先停止旧阅片器，
在原目录、原环境执行下面两条命令。新版 `catalog` 只读取已有 inventory 和人工记录，
不重新读取 NIfTI header、不扫描源目录、不转换或复制图像。

```bash
python qc_assist.py catalog --config config/config.nifti.local.yaml
python qc_viewer.py --config config/config.nifti.local.yaml
```

启动后只在终端打印 `http://127.0.0.1:8765`，**不会自动打开浏览器**。请手动访问该地址；
只有显式追加 `--open-browser` 才调用浏览器。旧的 `--no-browser` 参数继续有效，但已无需追加。

启动先分块读取现有 `series_sources.json`，再准备患者索引、已有人工记录及识别分组。
终端每约 5 秒显示当前阶段；清单解析阶段显示读取 MiB 和已解析序列数。等出现
`Patient index ready` 和访问地址后，再手动打开或刷新网页。首次读盘仍需时间，
这不是重新扫描源影像，也不需要再次运行 `catalog`。

清单/患者列表加载不设 CPU 或磁盘占用门槛、不降优先级、没有固定等待时限。
分块读取只是减少同时驻留的清单副本，不限制读取总量或减少患者。网页初始化等待期间
持续显示耗时，不再因超过 60 秒取消请求；真正的接口错误仍显示错误和重试按钮。
源影像、已保存的 QC 决定、识别规则和归档保护不变。`Ctrl+C` 可停止本次启动后再重试。

1. 默认进入“序列待识别 · 每组一例”，T1、FLAIR **各自归组**，每组显示影响人数。
2. 第一阶段不显示质量通过/失败按钮。只确认序列归属、协议优先级，不认证图像质量。
3. 自动识别错误时，从“纠错备选序列”按原文件夹名选择正确影像，点击“将备选加入
   T1/FLAIR 识别”。可勾选“纠错：查看全部序列”逐一预览，但不要求分类其他序列。
4. 数字越小越优先。只有确属误识别时才移出目标候选；不要把非首选真 T1/FLAIR
   改成其他。同优先级不同协议需明确勾选“留待质量阶段逐幅比较”。
5. 无需填写识别依据。点击“预览同类影响”，核对人数与人工冲突，再“确认识别并应用同类”。
   已有人工决定不覆盖，不将代表病例的质量通过传播给其他患者。
6. 没有候选时可从备选纠错。人工确认本轮列出的序列均不是目标后，点击“本轮序列均不是
   T1/FLAIR”，核对名称和预览人数，再确认发布。同组相同模板自动跳过，下一位代表只显示
   尚未确认的新模板。自动识别为空不会直接触发排除；不删除文件、不记录质量失败。
7. 待识别组为零后，点击“序列识别完成，进入质量检查”。系统不会自动开始计算质量。

同类识别依据为中心和规范化协议名称。仅移除明确的日期/MR/序列号前缀，保留
T1、T2、3D、2mm 等内容；不能可靠规范化的前缀仍保留。只有 `image`、`series` 等
无协议含义的通用名称不跨患者传播。DWI/T2 等不进入分组签名，多一个 DWI 不会拆组。
T1 组不受 FLAIR 组合影响，反之亦然。矩阵、层数、体素变化不拆分**序列识别**组，
但仍保留在原始元数据、严格的质量模型域和后续技术检查中，不能据此宣称质量等价。
原 `protocol_id`、旧组合规则和人工记录保留；两阶段流程不直接继承旧整套组合规则。

名称高置信、只有一个协议族的候选默认视为已识别；可从“全部 T1/FLAIR 协议组”纠错。
同模板重复扫描不会制造新的序列识别任务，但仍保留多个影像，留到质量阶段比较。
代表病例优先采用已有人工记录，否则稳定排序；实际读取失败会在按需预览中显示，
不会为建立目录而预先读取全队列图像。

识别统计保存在私有 `visual_qc/assist/identification_catalogue.json`，包含每个模态的
`pending_groups`（代表组数）、`pending_subjects`（影响患者数）和重复扫描人数。
这些不是“待阅影像数”，两个模态患者数也不能直接相加当作独立患者数。

### 同组只看新序列

例如第一人拥有 A/B/C，人工确认三种模板均不是 T1 后，另一位只有 A/B/C 的患者自动
完成该模态的缺失确认；拥有 A/B/C/D 的下一位只需确认 D。若 D 是 T1，加入 T1 识别并
发布正向规则；若 D 也不是，继续排除。无需逐个患者点击“未找到”。其他模态仍独立处理。

“不是 T1”和“不是 FLAIR”分别保存，不把真 FLAIR 改成其他。负向规则范围固定为首次
确认时的本组成员；后来的新患者不自动加入。旧逐患者缺失决定保留，但不追溯扩大成整组规则。
新增模板会使原先自动跳过的患者重新待识别。源数据读取失败或勾选“待定”的项不会算作
否定证据；其他可识别病例优先展示，仍未解决的失败/待定项留在队列。重试成功后可取消待定。

“查看全部序列（含已排除）”恢复完整备选列表；“已排除模板 / 撤回排除”可勾选模板，
预览并发布撤回。相关缺失结论自动重新计算。已有人工正向分类受到保护，冲突模板不能
发布排除；先核对冲突或撤回错误规则。整个过程不复制代表病例的质量结果。

页面显示本轮新模板数、累计排除模板数、自动跳过人数及剩余人数。总计的新增字段为
`auto_skipped_subjects` 和 `excluded_templates`；撤回时这些派生计数可能减少。实际减少
的人工工作量以工作站统计为准，不把剩余患者数直接当作还要逐人阅片的次数。

### 更新和备份识别记录

先保存决定并用 Ctrl+C 停止阅片器，不要在运行中替换代码。在当前项目和当前环境中：

```bash
audit_root=$(python -c 'from gb_dicom2bids.config import load_config; from pathlib import Path; print(load_config(Path("config/config.nifti.local.yaml")).paths.audit_root)')
backup_dir=$(mktemp -d "$audit_root/visual_qc/identification-backup.XXXXXX")
cp -a "$audit_root/visual_qc/assist"/identification* "$backup_dir/"
printf '识别记录备份：%s\n' "$backup_dir"
git fetch origin
git switch feat/protocol-assisted-qc
git pull --ff-only origin feat/protocol-assisted-qc
python qc_assist.py catalog --config config/config.nifti.local.yaml
python qc_viewer.py --config config/config.nifti.local.yaml
```

已有本地分支时使用上述 `switch`；首次跟踪分支时使用
`git switch --track origin/feat/protocol-assisted-qc`。拉取存在冲突时停止，不强制覆盖本地修改。
本次无新增依赖，不重建环境，不运行 inventory 或重新复制 BIDS。`catalog` 只重建清单统计。

私有识别状态新增 `negative_scopes`，旧模板 ID 不变。预览、发布、撤回均检查版本，所有
确认和撤回保存在识别历史中。更新后的负向规则不被旧代码识别；回退必须先停止阅片器并
另外备份当前识别记录，再一起恢复升级前代码和上述识别备份。不要仅降级代码后继续归档。
手工质量记录、候选缓存和源影像不随本次更新重建。

```bash
python qc_assist.py status --config config/config.nifti.local.yaml
```

CLI 也可显式切换阶段；仍有待识别组时，完成命令会拒绝执行：

```bash
python qc_assist.py finish-identification --config config/config.nifti.local.yaml
```

需要改序列规则时，先在页面返回序列识别，或运行 `reopen-identification`。
已有人工质量记录保留；规则/阶段改变会使旧自动证据失效，后续模型需要重新校准验证。
原始切片仍可预览，但第一阶段的质量保存、质量计算和 BIDS 应用均被后端阻止。

## 第二步：原始切片特征与标签盘点

**仅在第一阶段完成后执行本节。** 也可以先在页面继续人工质量检查。已保存的人工
QC 可继续使用，无需从头逐幅复核；改分类应返回第一阶段。

```bash
python qc_assist.py features --config config/config.nifti.local.yaml --workers 8 --resume
python qc_assist.py status --config config/config.nifti.local.yaml --watch 5
```

读取已归类的 T1/FLAIR 候选，默认 8 个 CPU 进程，每任务一个计算线程。读取源 NIfTI
但不写源文件，不重采样、不把厚层变成薄层。沿用配置的临时盘、staging 和内存余量
门槛；资源不足时暂停提交新任务，正在运行的任务不强杀。
输出包含完成、失败、缓存命中、速度、ETA 和 worker 状态。Ctrl+C 后缓存保留；
恢复使用 `--resume`，失败任务需要 `--retry-failed`。缓存核对源 SHA256、文件状态和版本。

指标包括平面内梯度/清晰度、高频成分、背景异常信号、相邻层变化、局部低清晰度比例。
最可疑的原始层面可从阅片器按钮直接跳转。空图、非有限值、读取/几何/前景失败不会
自动通过。低清晰度称为“模糊/伪影风险”，**不能证明头动原因**。

```bash
python qc_assist.py calibrate --config config/config.nifti.local.yaml
python qc_assist.py propose --config config/config.nifti.local.yaml
```

`calibration_summary.json` 汇总标签数量、类别和中心/模板/几何分布。
仅人工通过和明确质量失败可成为标签；未选择、误分类、待核实、原因不清不作为失败。
旧标签继续读取，缺少可信身份/源校验和的记录可能需要重新保存。
新界面有结构化原因选择，如头动/模糊、重影、信号缺失、分类错误、仅未选中。

T1 和 FLAIR 分开训练逻辑回归。稳定哈希按患者分为约 60% 训练、20% 阈值校准、20%
独立抽查；同患者所有候选/重扫不能跨集合。最低门槛为 20 幅训练标签、两种质量类别和
10 幅校准标签，这只是可运行门槛，不是精度保证。样本不足或无合格阈值时显示
`needs_labels`，可先按原始指标定位可疑层并继续补充人工标签，**不会自动通过**。

## 第三步：独立抽查后才允许自动通过

训练后模型与阈值冻结。`propose` 从独立患者集合中预测合格、协议明确且最终候选唯一
的池中固定随机抽样，不按是否已有人工评分挑选。阅片器“自动通过抽查”包含：

- 启用前的独立验证病例。
- 随机样本未覆盖模板的额外范围检查。
- 启用后稳定抽取约 5% 的持续抽查病例；抽查完之前不自动授权。

在该队列逐幅作真实人工质量判断并保存，然后重新运行 `propose`。只有每个模态自己的
独立随机样本全部完成、单侧 95% Clopper–Pearson 不合格率上限不超过 5%，才启用自动
授权。零错误至少需要 59 名独立患者；59 人有 1 个错误不能通过。
目标针对该模态的已验证自动通过池，不宣称每个稀有中心/协议各自都满足 5%。
域还须同时有训练、校准、独立范围检查支持且无范围检查错误。未覆盖域仍需人工。

默认在训练前固定抽查 59 人。如计划更大独立样本，可在**查看抽查结果之前**指定：

```bash
python qc_assist.py calibrate --config config/config.nifti.local.yaml --audit-size 120
```

不要看到错误后不断增加样本直至通过。改变模型、阈值或规则应显式执行
`calibrate --new-model` 并重新 `propose`；旧模型授权随即失效，新模型不复用先前独立
抽查患者。新模型数据不足时保持无自动授权，不能退回旧模型悄悄继续放行。
当前规则使用全局修订号，任何发布/撤回都会保守地使已有自动证据失效。

自动通过需同时满足：有效唯一候选、明确协议、技术检查通过、达到冻结阈值、已验证
范围、无人工冲突、无范围暂停。质量待复核队列包含高风险、不确定、技术失败、真实
多候选和未验证范围。低优先级备选保留，但不会仅因未被选中而强制判为失败。
明确“无可用候选”只能人工决定。

持续抽查发现质量失败时，暂停该模型对应模板域的新自动通过，输出 `recheck.json`，
已有相关自动结果不再进入合格清单。更改独立验证病例的人工记录也会立即使验证报告
失效，需重新 `propose`。人工最终选择和人工质量决定始终优先。

## 第四步：核对后单独归档

```bash
python qc_viewer.py --config config/config.nifti.local.yaml --apply --dry-run
python qc_viewer.py --config config/config.nifti.local.yaml --apply
```

阅片和自动建议都不直接归档。应用时再次核对来源、版本、图像/JSON 校验和、目标及
写入互斥，复用现有实体复制、事务恢复和可恢复备份。无关模态和原始文件不变。
规则/模型撤销后，dry-run 可能列出 `quarantine`，表示撤回已经失去授权的旧结果，
正式 apply 才将其移到可恢复备份。人工重新通过并选定后可再次安装。

`accepted_manifest.tsv` 增加 `decision_source`、`model_version`、`rule_revision`。
自动记录签为 `automatic`，不冒充审核者；人工作出的新决定仍保存在原有 JSON/历史。
自动建议、特征、模型和报告在 `audit_root/visual_qc/assist/` 下，与人工记录分开。
模型为可审计 JSON 系数，不反序列化外部 pickle。

## 现场验收及回退

本次代码验证见 [交付验证记录](protocol_assisted_qc_verification.md)。

先完成少量代表病例的“发布规则—提取特征—补标签—独立抽查—dry-run—apply”流程。
检查重度 WMH、梗死、萎缩、厚层和低分辨率病例，不要将病变或协议差异直接当作模糊。
报告训练/校准/抽查人数、错误数及上限、中心/模板覆盖、自动通过数、人工待处理数。
自动通过数只是潜在减少的阅片量，持续抽查和协议识别仍消耗人工，不能报告为实际节省工时。

回退前先备份私有审计目录，停止阅片器和计算；撤回/停用模型后用本版 dry-run 查看
自动结果撤回计划并完成需要的隔离，再切回旧代码。不要删除原人工决定、inventory、
源影像或备份；旧版不应把自动结果当作人工已认证结果。

公共 Git 只含代码、说明和纯合成测试。真实规则、标签、模型、报告和影像不得提交。
本项目版权归 zhenzong，未授予复制、修改或再分发许可。
