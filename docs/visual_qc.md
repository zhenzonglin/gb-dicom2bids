# v0.2.0 可视质控补丁 1

作者：zhenzong。合成测试不等于真实队列验收；本工具用于研究影像整理，不用于临床诊断。

## 从 GitHub 轻量分支使用

分支为 `feat/visual-qc`；不改动 `main` 或 `v0.2.0` 标签。建议浅克隆到现有项目旁边，
保留原克隆。先等待 pipeline 完成或安全停止全部子进程，再激活原有 Conda 环境。
从当前正在使用的旧项目目录执行：

```bash
OLD_PROJECT="$PWD"
git clone --depth 1 --single-branch --branch feat/visual-qc \
  https://github.com/zhenzonglin/gb-dicom2bids.git \
  ../gb-dicom2bids-visual-qc && cd ../gb-dicom2bids-visual-qc
cp "$OLD_PROJECT/config/config.local.yaml" config/config.local.yaml
python -m pip install --no-deps --no-build-isolation -e .
python -c 'import gb_dicom2bids; print(gb_dicom2bids.__file__)'
python qc_viewer.py
```

如果克隆失败，请停止，不要继续后续命令。导入路径应位于新克隆的 `src/gb_dicom2bids/`。
这一步只把原环境中的本项目切换到新代码，不下载运行依赖、不新建环境，也不复制数据集。
以后在新项目目录使用该环境执行转换和 QC，避免使用旧版代码绕过人工认证门槛。
原配置指向相同 audit/staging，因此清单、复制进度和候选缓存继续沿用。
若需要原监控器，可将旧项目的 `monitor.py` 复制到新项目；它不随本分支提交。

直接克隆本分支无需再运行离线安装器，也无需在浅克隆中运行 `build_qc_patch.py`。
首次启动质控器会检查活动任务并启用人工认证门槛；浏览器和后续应用方式见下文。

## 安装到现有克隆

不重建环境、不重扫 DICOM、不重复制 BIDS。先等待当前 pipeline 完成，或使用原有
`scripts/stop_run.sh` 安全停止，并确认子进程已经退出。关闭已有质控网页服务。
监测脚本可以保持运行。不要对运行中的项目热替换代码。

将 ZIP 和旁边的 `.sha256` 一起传到工作站。在传输目录验证、解压：

```bash
sha256sum -c gb-dicom2bids-v0.2.0-visual-qc-1.zip.sha256
unzip gb-dicom2bids-v0.2.0-visual-qc-1.zip -d gb-visual-qc-patch
cd gb-visual-qc-patch
sha256sum -c CHECKSUMS.sha256
```

激活正在使用的项目环境，进入现有 `gb-dicom2bids` 项目目录。将下面安装器路径替换为
刚解压的绝对路径，不要填写数据集路径作为 `--project`：

```bash
python /path/to/gb-visual-qc-patch/install_qc_patch.py --project "$PWD" --check
python /path/to/gb-visual-qc-patch/install_qc_patch.py --project "$PWD"
python qc_viewer.py
```

安装器针对原始 v0.2.0 提交逐文件检查基线。代码被自行改过时会拒绝覆盖，显示冲突文件。
不要使用强制覆盖。`monitor.py`、本地配置和环境文件不在补丁内。
原代码逐文件备份到 `work/patch_backups/<时间戳>/`，安装后显示回滚编号。
环境必须是此目录原有的 editable 安装；若导入的是另一份克隆，先激活正确环境。
不会执行 `conda env create` 或下载任何运行依赖。

浏览器服务只绑定 `127.0.0.1:8765`。在工作站桌面浏览器打开该地址；若用 SSH 登录，
浏览器里的 `127.0.0.1` 是当前电脑，需要显式 SSH 本地端口转发，不要绑定公网地址。
页面没有外部字体、脚本、遥测或影像上传。关闭浏览器不会丢失已保存决定，终端 Ctrl+C
退出服务。预览中的当前转换可能需要完成后退出；日志和候选缓存保留。

## 阅片顺序

1. 从左侧选择患者；可按中心、协议、自动状态、原因或 pilot 过滤。
2. 两个下拉框选择竞争候选。自动入选、待复核、排除均可查看。
3. 轴、冠、矢三视图滚轮切层；Ctrl+滚轮缩放，拖动平移。窗低/窗高控制亮度范围。
   显示采用神经学约定：左侧 L、右侧 R；矢状面左 P、右 A。
4. 逐候选记录“通过 / 不通过 / 待核实 / 未评”。多个候选可以同时通过。
5. 每个模态选择一个通过的最终候选，或选择“无可用”并填写原因。
   未被最终选中不等于质量失败。不通过、无可用、改分类必须填原因。
6. 点击“保存决定”或“保存并下一例”。这里只更新审计数据，staging 不变。

“包括其他序列”可以检索漏识别协议、按需预览、人工改为 T1 或 FLAIR。失败候选仍显示，
日志在候选下方，可点击重试。默认最多同时转换 2 个预览，pigz 每个 2 线程，复用现有资源
硬门槛。不会预先转换全队列。4D 其他序列只显示第一卷并禁止通过；多输出或不支持的转换
可能失败，不能强行解释为结构像。非 MR 不能作为最终结构像。

检查脑覆盖、运动/重影、信号缺失、严重畸变和模态是否正确；不要仅凭中央一层判断。
可看右侧候选元数据和各方向边缘层面。元数据不足时保留“待核实”，不要以主观判断代替
无效的空间信息。这个补丁不自动诊断病灶，也不替代后续配准测试。

Study UID 或日期差异不再作为保存门槛，也不要求确认“属于同一次检查重扫”。本版仍是
单 session，不自动创建或判断纵向随访会话。
T1 最终选择必须由原始方向证实为轴位（20°阈值）的 original 或 derived_mpr。
把矢状采集显示成轴位切面不改变其资格。人工仍应优先原始轴位；轴位 MPR 用作质量合格的替补。

## 显式应用

先用少量代表病例走通以下过程。应用处理所有已保存且未完整应用的模态组。
不必退出浏览器，但不能存在 pipeline 转换或其他已登记的写入进程。

```bash
python qc_viewer.py --apply --dry-run
python qc_viewer.py --apply
```

dry-run 列出安装/隔离的患者与模态，不修改 staging。应用仍不会扫描或重复制。
已完成的旧 BIDS 复制记录是前提；如果安全停止在复制中间，先让原任务完成复制再应用。
可用 `gb-dicom2bids pilot --config config/config.local.yaml` 继续既有清单上的 pilot 和文件级
复制断点；不要重跑 `run --mode inventory-pilot`，因为补丁启用后禁止覆盖已冻结的 inventory。
每组替换前备份旧文件；无可用或撤回已应用候选时移到可恢复隔离区。几何、候选身份、影像与
JSON 校验和必须同时通过。空间/存储不足时停止提交应用，不杀死其他任务；解决后重复执行。

每组记录事务，所有相关清单写入后才签发认证。中断或 NFS 错误后用同一条 `--apply` 重试。
中断期间 staging 可能只有部分文件更新，所以绝不能让下游直接扫描整个 staging 作为合格队列。
只使用当前 `accepted_manifest.tsv`，并在集中应用结束后再启动下游。
相同决定与文件已完整应用时跳过；损坏输出用已审核缓存恢复。

启用补丁后，普通 `convert --resume` 只能继续准备未认证候选，不能代替 `--apply` 安装。
旧 BIDS 复制来的影像保留，但未审核者都是“未认证”，不进入合格清单。
原始 DICOM、正式 BIDS 和非目标模态不改动。新患者即使旧 BIDS 不存在，也可在审核后安装。

## 私有输出

所有以下文件位于配置的 `audit_root/visual_qc/`，不得上传 GitHub：

| 路径 | 用途 |
| --- | --- |
| `subjects/`、`history/` | 当前决定和不可变版本记录；多窗口冲突要求重新载入 |
| `baseline_selection.tsv` | 安装时的自动推荐快照，保留原始状态和原因 |
| `artifacts/`、`jobs/`、`frozen/` | 候选身份/校验和、隔离转换和固定来源缓存 |
| `errors/` | 候选转换失败记录 |
| `timings/` | 按需读取的检查/采集时间，仅保存在私有审计区 |
| `transactions/`、`certifications/` | 每患者模态应用事务及认证 |
| `backups/<患者>/<版本_模态>/` | 更换或隔离前的 staging 文件；只读保留 |
| `accepted_manifest.tsv` | 当前人工通过、最终选择且完整安装的影像；正式合格名单 |
| `coverage.tsv` | DICOM 候选数、旧/新文件、认证、缺失原因、新补入患者 |

保存决定会撤销旧版本认证；应用后重新生成覆盖表。覆盖表是最近一次应用/核对的快照。
常规 `selection_manifest.tsv`、`manual_review.tsv`、`conversion_status.tsv`、scans 和差异表
在应用时更新。既有自动 QC 报告仍是技术检查，不应替代上述人工认证名单。
更新后再次运行 BIDS Validator 和下游文件发现/配准冒烟测试；补丁不自动改名切换正式 BIDS。

## 回滚

关闭质控器并确认 pipeline 已停止。使用安装时输出的时间戳：

```bash
python /path/to/gb-visual-qc-patch/install_qc_patch.py --project "$PWD" --rollback TIMESTAMP
```

只恢复代码，不删除清单、进度、缓存、决定历史、影像备份，也不会自动逆转已应用的影像。
安装后再次编辑过的代码会使回滚拒绝执行。旧代码没有人工审核门槛，回滚后不要继续入库或
据旧代码生成合格名单；先恢复补丁。影像回退应依据具体组事务和备份人工处理，避免整库覆盖。

## 开发验证

```bash
python -m pytest -q
python -m ruff check .
python -m build
python scripts/make_qc_demo.py --output work/synthetic-demo
python qc_viewer.py --config work/synthetic-demo/demo.yaml
```

开发用 `scripts/check_qc_browser.py` 需要 Playwright 和浏览器，不是工作站运行依赖。
真实厂商数据、NFS 异常恢复和临床质量阈值仍需工作站试点验证。

Copyright (c) 2026 zhenzong. All rights reserved. No license is granted.
