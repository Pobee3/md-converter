# 运行入口与实测边界

记录日期：2026-10-14。共享契约快照 0.2.0。移机后重新探测解释器、MinerU、依赖与本地服务；全局安装状态以实际安装检查为准。

## 用户电脑上的 MinerU 探测与部署

接收 PDF 或 EPUB 后，先在实际执行主机上检查可用的 MinerU 命令、候选 Python 环境中的安装包与版本、模型文件以及已有本机服务。找到文件或进程不等于可用：用源文件的一小段做解析，核对输出格式、物理页身份和资源。优先复用通过小样的本地安装；不要把本机回环服务误写成云端上传。

若没有可用 MinerU，助手负责依据当时的官方说明在用户电脑上部署到隔离环境，按机器的 CPU/GPU 与系统条件配置模型，并用小样验证 CLI 或本机服务，再继续转换。安装/下载/系统权限受当前执行环境约束时发起相应权限请求；失败时保留日志并解决具体依赖，不要求用户自行运行部署命令，也不覆盖现有 Python 或 MinerU 环境。已有安装但版本/模型不兼容时同样优先隔离修复，保留原环境。远程解析服务须有用户明确授权；默认使用本地资源。

部署完成仍要按以下运行入口和实际版本帮助选择参数；旧版 `backend`、新版 `tier` 不可凭名称类推。MinerU 对局部页解析不准时，另取 PDF 文字层、其他可用本地 OCR 或原页裁图核对，而非无限重跑同一引擎。每个补救结果都保留来源页、区域和选择依据。

## 现在能运行的部分

`scripts/md_converter.py` 提供统一的代理调用入口。Python 3.12.14 已测试；原生 EPUB 使用标准库 ZIP/XML，PDF 预检和适配另需 pypdf。解释器必须保留虚拟环境入口路径，不能先解析符号链接到基础 Python，否则会丢失已安装依赖。

- `inspect`：EPUB 原目录层级和原生位置候选；内部持久化身份，不修改原件。
- `parse-local`：MinerU 4.0.x 调用、来源/输出指纹、实测档位和页范围记录。PDF 默认 Standard，EPUB 用 Flash；`--service` 仅接受明确的本机地址。命令参数名 remote-url 不代表上传云端，实际地址必须是已确认的本机服务。
- `convert-pdf`：在原件快照上运行 Standard，可用 `--batch-size` 让所选范围落在单个批次，或按批次运行后合并；两者都核对页范围和指纹。按保存的服务 job_id 续查，已完成批次复用；失去提交响应时不盲目重复提交。`--pages` 限制文件页，未提供则全书；批大小是本实现配置，不是 MinerU 固定限制。当前每次解析调用有约 600 秒超时，不能只因参数允许就认定数百页单次调用可行。
- `inspect-pdf`：转换前列出书签/已审原目录、文件页、阅读器标签、已核对印刷页及依据。可带 `--review` 载入助手核对原页后的私有决定；没有书签时可用已核对的 `toc_catalog` 作为选章目录。此步不运行模型。
- 目录候选来源与取舍、正文起点和页码证据目前由助手在任务私有审阅记录中核对；执行器没有自动合并“原目录/正文标题/人工补录”的审核界面。`locate` 可按块 ID 查原位、按 `pdf-page:<文件页>` 查页级候选；段落级逆向判断仍需结合来源片段与原页人工核验，不能宣称已有自动双向定位接口。
- PDF `--chapter-id`：可重复提供预检给出的章节 ID；执行器自动计算文件页并用章起点几何裁切同页边界。父章包括子章。保留必要边界页作处理上下文，但输出只含选中章节的正文块。未核对的同页起点不能被宣称精确。
- `convert-epub`：原目录分章、行内格式、图片、原生 HTML 表格、原注依赖、独立引用位置、分批缓存/恢复、核心映射。传入已执行 Flash 的 `--engine-result` 保留双路证据；没有时只是原生 DOM 候选，不声称已运行 MinerU。
- `adapt-pdf`：从带运行指纹的真实 Standard middle JSON 产生正文/目录/页图/映射候选；保留嵌套印刷目录、已提供的行内样式/上标。支持原页证据决定的目录替换、标题分类、精确文本修订、段落合并、脚注拆分/引用与冲突选择。图片和表格区域可用原 PDF 区域渲染保存（目前限未旋转页，需要 pypdfium2），这不是原嵌入图片字节；表格单元格未转录为可编辑文字，公式资源和其他未处理内容仍明确报未支持。
- 段落拆分与续注合并：显式原页审阅后可按完整字符范围拆分段落，校验原候选指纹、无遗漏和候选 ID 迁移；续注可保留一个 ID 与多页原注区域。它们是审阅决定的执行能力，尚不等于自动识别所有自然段或续注。
- 原注调用点：从正文上标与同页唯一原注号生成私有字符范围证据；同一段的重复引用分别定位。候选会进入 notes.json，但 `verification` 仍为 `unverified`，报告保留原页复核项。同页同号、注号未读出或含义不明时不强配；普通数字和公式幂次不匹配。这一步仍不能证明已识别所有注号或责任主体。
- 页边文字：`aside_text` 的原件区域和原始块保留在私有证据，报告要求区分行号、原版页边码与边注；不会因为它是数字就并入正文或印刷页码。
- `pdf_geometry.py`：内部函数可从未旋转 PDF 的文字层取得数字区域，结合已核验页边带与相邻文本生成行号疑点建议。重复匹配拒绝，正文同号数字不因数值相同就删除；不自动采纳建议，不自动估计版心，文字层可能错序或缺失，仍需原页核验。
- `validate` / `locate`：检查核心格式、版本、范围覆盖、原件指纹、原生节点或引擎片段，按块/注释 ID 反查来源。支持任务目录与独立草稿包。
- 当前任务门禁：`validate` 对 PDF/EPUB 检查此次合并对应的检查点、每批产物指纹与目标覆盖；中断任务即使留有旧 `source.md` 也不能通过或另存。独立草稿包复核包内文件/原件/原始解析结果和范围，不把草稿标为已校读的正式 release。
- `save`：校验后保存独立草稿；默认 `--layout flat` 为旧版平铺目录，`--layout split` 在新成品文件夹中生成 `阅读原文/`、完整的 `来源包/`、顶层原件、说明、私有索引，以及默认的轻量增强型 MD 与交接索引。拆分布局按 `--formats md,word,pdf,epub`（默认 `md`）生成 `阅读原文/标注版/`；仅明确选择 Word/PDF 时才排版并保存目录、随机中间正文、原注三页预览。顶层原件与来源包内快照逐字节核对；翻译脚本仍以 `来源包/` 为来源目录。拒绝覆盖已有目录，保存中断保留失败标识，不称完成。
- `export-reading`：从已保存的来源包另存标注版原文，不改规范 `source.md`；可用 `--source-id-file` 仅输出已确定的来源 ID 范围。MD 使用内联 HTML 两端对齐；Word/PDF/EPUB 支持已映射的普通正文和标题；非 MD 格式从 `toc.json` 生成可点击目录，Word/PDF 的标题和阅读页码、EPUB 的章节及可用原书页码均链接到正文锚点。图表及行内图片等尚需逐项核对的复杂视觉块在 Word/PDF/EPUB 导出前会明确报错，不能静默丢失；MD 保留这些块的原始标记和资源。Word 需要 `python-docx`、LibreOffice 与 `pypdfium2`；PDF 需要 ReportLab、`pypdfium2` 和可嵌入的 CJK 衬线字体。执行前应通过 Codex workspace dependencies 查找可用运行时；导出后检查预览与实际文件。详见 [标注版导出](reading-export.md)。
- `service-status`：分别报告 Skill 专用服务、外部共享服务和指定 MinerU 解释器的真实可用状态；没有解释器路径时 CLI 能力为未知，不伪报可用。
- `start-service`：只在需要独立 PDF 服务时，在单独回环端口以已有 MinerU 解释器启动专用 API；持久记录进程 PID、创建时刻、完整命令和随机所有权标记。启动失败会核对并清理本次子进程；不能确认清理时保留记录和错误供处理，不接管已有端口。
- `exit` / `stop-service`：文档锁空闲时把进行中的检查点记为 interrupted，保留 PDF job 句柄供续查。默认不停止任何服务；显式 `--stop-owned-service` 仅在严格核验进程身份及完整 job 列表没有 queued/running 后，对专用服务发送 SIGTERM。iread 共享服务无所有权记录，不会被停止；若正在转换、身份不符或无法确认任务空闲则拒绝。

这些入口是转换助手内部工具，用户不需要编写参数、JSON 或计算指纹。仍按 Skill 的最少确认流程操作。

## 代理调用示例

以下变量表示执行者已确认的真实路径和身份，不是让用户手填的表单。

```sh
"$python" "$skill/scripts/md_converter.py" inspect "$epub" --work-dir "$work" --document-id "$document_id"
"$python" "$skill/scripts/md_converter.py" parse-local "$epub" --runtime "$mineru_python" --tier flash --output "$raw_flash"
"$python" "$skill/scripts/md_converter.py" convert-epub "$epub" --work-dir "$work" --document-id "$document_id" --task-id "$task_id" --engine-result "$raw_flash"
"$python" "$skill/scripts/md_converter.py" validate "$task_dir"
"$python" "$skill/scripts/md_converter.py" locate "$task_dir" "$block_id"
"$python" "$skill/scripts/md_converter.py" save "$task_dir" "$new_destination_folder" --name "$display_name" --layout split --formats md,word,pdf,epub --soffice "$soffice"
"$python" "$skill/scripts/md_converter.py" export-reading "$saved_bundle" "$new_reading_folder" --name "$display_name" --formats md
"$python" "$skill/scripts/md_converter.py" service-status --work-dir "$work" --runtime "$mineru_python" --shared-service http://127.0.0.1:16580
"$python" "$skill/scripts/md_converter.py" exit --task "$task_dir"
```

专用服务仅在没有合适现有本机服务时才启动：`start-service --work-dir "$work" --runtime "$mineru_python"` 返回自己的 URL，将它传给 `convert-pdf --service`。退出并要求关停时使用 `exit --task "$task_dir" --work-dir "$work" --stop-owned-service`；若服务仍有任务，这条命令保存检查点但拒绝关停，随后沿相同 task_id 恢复。正常批次无需向用户逐批询问。

EPUB 选择章节用可重复的 `--chapter-id`；父章节包含子章节，选择子章保留祖先目录。PDF 先运行 `inspect-pdf` 再用其章节 ID；用户不输入 `--pages`。恢复使用同一 task_id、源文件和范围；已验证批次跳过，损坏缓存重建，源文件或配置变化拒绝盲续。

PDF 可用 `inspect-pdf <原件> --work-dir <工作区> --document-id <身份> [--review <原页核对决定>]` 先看候选；`convert-pdf <原件> --runtime <MinerU解释器> --service <本机地址> --work-dir <工作区> --document-id <身份> --task-id <任务> [--chapter-id <预检章节ID> ...] [--review <原页核对决定>]` 执行。不传章节 ID 为全书。`--pages 1,2,3` 仅为内部诊断范围，不能要求用户手填。`locate <任务或保存包> <章节ID|块ID|原注ID|pdf-page:文件页>` 反查来源。恢复沿用同一来源/范围/引擎/批大小。局部 Advanced 用 `parse-local` 保存到另一输出路径。`adapt-pdf` 的 `--raw` 可多次提供重叠批次；同一来源块文字、位置、类型或其他字段不一致时阻止合并，复核选择必须绑定完整候选指纹集合。唯一自动容许的差异是布尔值或缺省的 `continues_prev` 提示：它受批次上下文影响，本实现不用它决定拼段；保留两份原始结果和私有差异记录，等价内容只输出一次，证据路径选择不随参数顺序改变。`--review` 是助手原页核验后保存的决定，不是用户必须做的预检。复杂图表和原子正式发布尚未完成。

## 已实测与未实测

本机 MinerU 4.0.10：iread 的现有本地服务成功完成 Standard（合成 PDF 文件页 2–3）与 Advanced（页 3）。结果的 page_idx 为原文件 0-based 索引，适配后加一；不是截取后重新编号。小样两档文本一致，仅 bbox 略有差异，保留 Standard。未据此推广到所有页旋转/扫描/多栏情况。

EPUB Flash 本地原生解析成功，page_count_kind=spine；不能当作 PDF 文件页。原生 DOM 适配保留 chapter/element 定位，缺 element_id 时公共映射仍 approximate，精确节点路径只在私有 trace 中，不伪造 CFI。

一份此前未用于调试的 38-spine 真实 EPUB 通过 Flash + 原生 DOM 全书批次恢复、合并、保存后验证与原 XHTML/资产反查：1659 块、28 章、29 资源，38/38 spine 有处理证据；选第一讲为 59 块/1 章。首轮暴露空白排版段落和行内 SVG 封面漏计，已通用修复，旧漏计任务现在被全书门禁拒绝。但该书报告 `technical_coverage_complete=false`，存在 8 处未建模容器文本、3 处尾部文字、6 处目录前正文和 24 处空排版块；所以只能称结构合并的待核验草稿，不能宣称全文逐字准确。证据在项目 `implementation/converter/evidence/new-real-epub-11/`。

已有模型实际位于本机 MinerU models 目录；下载缓存为空不能证明未安装。直接另启模型曾因目录锁权限失败，随后通过用户指定的 iread 现有服务完成测试。未安装、升级、下载模型或改写原环境配置。

实测包含合成 EPUB/PDF、真实服务 job_id 恢复与分批恢复、原件/资源/正文改动检出、原目录/注身份/反查。另用用户授权的 Husserliana XI 文件第 1–50 页完成七批 Standard，疑难五页 Advanced。所有批次可复用，原页页码已检查；真实文献发现行号混入、错误注号、跨页续注及段落结构问题。另存的审阅稿应用逐项文字修订和第 21–22 页续注合并；这验证修订的可追溯性，不是未干预的转换结果，不能推广成全 50 页都通过保真验收。

尚未完成：真实书全文保真验收、多栏/旋转复杂样例、可靠的行号分离及全部逻辑段落/跨页原注重组、复杂图表公式与交叉引用、通用 OCR 重转身份迁移、正式 release/manifest 原子发布及下游回执。没有证据的映射和身份保留未确认；不要将独立草稿称为完整生产成果。

## 契约边界

[core.schema.json](core.schema.json) 是权威核心结构的逐字快照，指纹见 [contract-snapshot.json](contract-snapshot.json)。校验器只实现该快照使用的 JSON Schema 词汇；遇到不支持的关键词拒绝，不自称通用 JSON Schema 引擎。

用户已同意 EPUB 无固定印刷页时不伪造 page_map（CR-converter-20261014-01），技术同步仍待契约负责人。原注来源、重复引用 occurrence、非 spine 节点与资产关系保存在任务私有 trace（CR-converter-20261014-02 proposed）。独立草稿带兼容性说明；不生成冒充已同步的 iReader 正式 manifest。
