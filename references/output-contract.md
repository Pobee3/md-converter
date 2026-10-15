# 转换输出与契约边界

依据 iReader 0.2.0。完整核心结构按 [core.schema.json](core.schema.json) 中的指定 `$defs` 校验；该文件是只读分发快照，出处见 [contract-snapshot.json](contract-snapshot.json)。不得因根 oneOf 任一分支通过，就认定文件满足其应有类型。

## 身份和生产目录

document_id 对应一份特定来源版本；source_revision 对应一次正式规范化原文及来源映射。书名与文件路径不作身份。生成永久 ID 后持久保存。chapter_id 对应原目录，段落 block_id=source_id，其他块使用自己的 block_id。source_map.entries.source_id 引用 blocks.block_id。

```text
books/<document_id>/
  originals/source.pdf 或 source.epub
  manifest.json
  releases/<source_revision>/
    source.md
    blocks.json
    toc.json
    page_map.json
    source_map.json
    notes.json
    assets.json
    assets/
  revisions/
  tasks/<task_id>/
    staging/
    checkpoint.json
    report.json
```

这是现行路径约定。EPUB page_map 的冲突见下文，不能把上图视为已解决。原始解析、审阅、修订及私有缓存保存在任务记录中并带 hash；正式 references、conversion-report、corrections 的必需位置/键尚待契约负责人统一，不能凭本说明另立公共文件协议。

正式 manifest 使用 `$defs.production_manifest`，不是 fixture_only 的 `$defs.manifest`。其中 source.snapshot 和 files/report 的登记为 path+sha256，source.provider 区分 local/zotero，缺少的 Zotero key 用 null 而非猜测。所有相对路径与其基准必须一致；生产 manifest 位于文献根，文件登记从该文献根解析，source.md 的图片从 source.md 所在目录解析。

未具备可用源快照、完整文件清单、通过报告和原子发布条件时，不写生产 completed。fixture_only 只能标记真实合成夹具，不能伪装用户成果。

## 核心对象

| 文件 | 核心语义 | 容易出错的地方 |
|---|---|---|
| blocks.json | block_id/source_id/chapter_id/order/kind/text/translatable | text 按原文计数；无文本图的 minLength 限制不能用编造题注规避 |
| toc.json | chapter_id/parent_id/order/title/首末块/printed_page_hint | 原目录层级，端点必须存在；提示页码不是实测来源页 |
| page_map.json | 每个 PDF 文件页、PageLabels、printed_labels、edition_markers、logical_order、rotation | 覆盖全源物理页；选章正文不等于删掉其余页图条目 |
| source_map.json | source/map revision 与每块 fragments | 每片段独立 ID；PDF 文件页从 1 起；EPUB spine 从 0 起 |
| notes.json | 原注内容、原标记、位置、责任身份、版次、证据与全部引用 | 源注 origin 限 original_document；未知责任不猜 |
| assets.json | 原图/结构、所属块、题注、来源和保真状态 | 图片可读/hash 正确不等于表格数字正确；核心 Schema 尚无完整定义 |
| source.md | 独立锚点加原书内容/原注可读表示 | 无额外导读、工具评语、用户笔记；锚点唯一，图链相对当前文件 |

公共对象带相同 schema_version、document_id、source_revision，地图还需 map_revision。未知扩展只能使用当前 Schema 允许的位置；多数对象未定义 extensions，不可任意添加。

PDF 标签保留 edition_id、sequence_id、label_raw、scheme、numeric_value、evidence、verification。罗马 xiv 原样保留，14 只可作为该编号区间的排序数值；二者不是同一标签。edition_markers 的 offset 指向相应段落，不能与当前版印刷标签混淆。

空白页由同一编号分区上下可靠页码唯一推得书页码时，在任务私有审阅记录保存 `inferred`、两端文件页和原页证据；公共标签沿用现行字段，evidence 明写推算过程，verification 至多 `approximate`，不能写 `verified` 或添加契约未定义的公共状态。没有唯一结果则保持 unknown。

fragment.range 对 blocks.text 使用 Unicode code point 半开区间。标题的 Markdown `#`、锚点和链接语法不计入；对结构表/公式，text 只代表其题注或文字锚点，不代表所有单元格/符号都已精确映射。bbox 为旋转后归一化可视坐标；未知为 null。

## 当前无法完整表达的情况

1. **EPUB 页图：** page_map.source_type 固定 pdf 且 pages 至少一项；不能填写 source_type=epub 或空页来通过。文件必需性与 EPUB 原生定位需契约确认。未确认前只保留候选，不切正式 EPUB 指针。
2. **原注精确定位：** notes.references 只有块 ID，缺引用出现范围、注正文映射，且要求至少一引用。不能给孤立注造一个引用；不能把两次同块引用压成一次后声称完全保留。原始证据可先存任务内，生产保证受限。
3. **原生/无题注资产：** blocks.text 非空，assets 无正式完整 Schema；EPUB 原生表格与派生截图的角色尚未冻结。保留原始内容，不编作者文字或私自降低必需原图规则。
4. **未完整机器定义：** references、assets、conversion-report、corrections、checkpoint 仍需文字规则与跨文件检查。私有检查点可在任务内实现；缺机器定义本身不等于所有处理都必须停止，但正式输出须能说明如何满足现行契约，不能把提案字段写进生产文件。

独立使用也不得把契约未通过的候选称为 iReader 可发布成果。候选可以展示可读原文、已保留资源及实际缺项；是否继续取决于任务目标，不隐瞒限制。

## 状态

使用既有任务状态 not_started、queued、running、validating、completed；异常为 needs_attention、interrupted、failed、cancelled。恢复回 running。转换 completed 与翻译完成无关。

verification 只用 unverified/approximate/verified；verified 要有可审查原件证据，不采用模型自报置信度。publication_state 使用 draft/published，quality_state 使用 draft/needs_review/reviewed；不要将这些不同维度相互替代。

“待选择保存目录”可以是界面提示，不是新任务状态。正式协议的字段、身份、目录布局或状态变化必须先走 CR。
