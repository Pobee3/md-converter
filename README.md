# md-converter

A Codex skill for converting PDF and EPUB books into traceable Markdown source packages.

It preserves stable paragraph identities, table of contents structure, physical PDF pages, verified printed page labels, original notes, figures, formulas, and source mappings. It also reviews OCR artifacts, restores words split by artificial character spacing, and keeps a logical paragraph continuous across page boundaries.

## Install in Codex

Ask Codex:

> Install the `md-converter` skill from `https://github.com/Pobee3/md-converter`. The skill is at the repository root.

Or ask in Chinese:

> 请从 `https://github.com/Pobee3/md-converter` 安装 `md-converter` Skill，Skill 位于仓库根目录。

Codex will install it into the local skills directory. Start a new turn after installation so the skill becomes available.

## What it does

- Converts a complete PDF/EPUB or selected chapters.
- Detects an existing local MinerU installation, model files, and local services.
- Helps install and validate MinerU in an isolated environment when no usable installation exists.
- Produces readable Markdown plus `blocks.json`, `source_map.json`, `page_map.json`, `toc.json`, `notes.json`, and asset records.
- Keeps one `source_id` for a logical paragraph that spans multiple pages and records each physical page region as a separate fragment.
- Repairs OCR split words, line-break hyphenation, and artificial letter spacing only after checking source evidence.
- Preserves original notes, figures, tables, formulas, and cross references when the source and current data contract support them.
- Supports checkpoints, resumable batches, validation, and reverse lookup from Markdown blocks to their original locations.
- Can hand a validated source package to a separate translation skill.

## Use

After installation, provide a PDF or EPUB and ask Codex to use `$md-converter`. Codex will ask for the destination and whether translation should follow, then handle engine selection, batching, source review, and validation.

The skill creates source material. It does not translate or typeset bilingual editions by itself.

## Repository layout

```text
SKILL.md
agents/openai.yaml
references/
scripts/
```

`SKILL.md` is the entry point. Detailed PDF, EPUB, fidelity, delivery, and runtime rules live under `references/`; deterministic helpers live under `scripts/`.

## License

MIT
