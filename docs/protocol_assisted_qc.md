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

```bash
python qc_assist.py catalog --config config/config.nifti.local.yaml
python qc_viewer.py --config config/config.nifti.local.yaml
```

1. 左侧选择“协议待识别”，打开代表病例。
2. 展开“协议识别”，按原始文件夹名指定每个模板为 T1、FLAIR 或其他。
3. 同一模态中优先级数字越小越优先；真正的 T1/FLAIR 不要为了去重改成其他。
4. 填写规则依据，点击“预览批量影响”，检查影响人数、选择变化和冲突。
5. 无冲突时发布规则。只传播分类和优先级，不传播任何质量通过记录。

模板包含中心、规范化文件夹名、矩阵、层数、体素和已有方向信息。仅去除明确的
日期/MR/序列号前缀，保留 T1、T2、3D、2mm 等数字语义。不能识别的前缀不删除。
组签名保留**该患者所有候选模板及重复数量**，因此保守地要求完整组合匹配；
组合不同不会套用规则。当前 protocol_id 不变，新增模板 ID 独立保存。

已有一致人工记录的可读取病例优先作为代表。相同模板有相互矛盾的人工分类或最终
选择时拒绝发布，并列出冲突病例。仅有“质量通过”但没有“最终选择”不等于协议偏好。
同模板真实重扫和同优先级不同影像仍需人工比较。非首选协议保留为候选，不视为质量失败。
缺少 T1/FLAIR 的组合也会进入协议队列，可检查“其他序列”补充漏识别。

## 第二步：原始切片特征与标签盘点

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
