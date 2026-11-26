# 研究思想库 · Research Ideas

[中文](#chinese) · [English](#english)

<a name="chinese"></a>
## 中文

在本机整理研究假设、推导、未解决问题和阅读笔记。每条思想可以保留来源段落、归属、验证状态和修订历史。Web 界面包含中文全文检索、LaTeX 公式检索、思想列表、关系图和时间线。语义检索可选用本机 Ollama 和 BGE-M3。

## 启动

需要 Python 3.9+ 和支持 FTS5 trigram 分词的 SQLite。本机后台同步和 PDF/OCR 增强适用于 macOS。

```sh
export RESEARCH_IDEAS_ROOT="$HOME/Research Ideas Sources"
export RESEARCH_IDEAS_STATE="$HOME/.local/share/research-ideas"
mkdir -p "$RESEARCH_IDEAS_ROOT"
python3 -B web.py
```

访问 [http://127.0.0.1:8767](http://127.0.0.1:8767)。将你选择的资料放入来源目录后运行 `python3 -B maintenance.py sync files`。也可以通过 `RESEARCH_IDEAS_RESEARCH_PATHS` 和 `RESEARCH_IDEAS_COURSE_PATHS` 指定目录，多个路径以平台路径分隔符隔开。

- **全文和语义检索**：全文开箱可用。语义检索先运行 `ollama pull bge-m3`，然后运行 `python3 -B maintenance.py vectors 100`。
- **Zotero**：开启本地 API 后，运行 `python3 -B maintenance.py sync zotero`。可选的 PDF/OCR 帮助程序分别用 `swiftc pdf.swift -o pdftext` 和 `swiftc vision.swift -o vision-ocr` 构建。
- **飞书**：手动导出放入状态目录的 `imports/feishu/`。实时同步可使用自己授权的飞书 CLI，通过 `RESEARCH_IDEAS_FEISHU_CLI`、`RESEARCH_IDEAS_FEISHU_PROFILE`、`RESEARCH_IDEAS_FEISHU_HOST` 指定参数。
- **学术聊天**：将选中的导出文件放入状态目录的 `imports/chatgpt/`，或调用 `capture_chat.py` 从标准输入提交 JSON，至少含 `title` 和 `quote`。只导入你想收录的记录。
- **备份**：`python3 -B maintenance.py backup` 生成并校验 SQLite 副本。使用 `RESEARCH_IDEAS_PORTABLE` 指向你自己的同步目录可管理可移植快照和历史备份。数据库和备份可能含原始材料，应当与源码分别存放。

本仓库包含应用源码。初次启动使用空数据库。检索、来源定位和备份应在你自己的设备和授权范围内使用。

---

<a name="english"></a>
## English

A local workspace for research hypotheses, derivations, open questions, and reading notes. Ideas retain source excerpts, attribution, verification status, and revision history. The web interface offers Chinese full-text search, LaTeX formula search, a list, a relationship graph, and a timeline. Semantic search can use local Ollama with BGE-M3.

### Run locally

Requires Python 3.9+ and SQLite with the FTS5 trigram tokenizer. Background sync and the optional PDF/OCR helpers are designed for macOS.

```sh
export RESEARCH_IDEAS_ROOT="$HOME/Research Ideas Sources"
export RESEARCH_IDEAS_STATE="$HOME/.local/share/research-ideas"
mkdir -p "$RESEARCH_IDEAS_ROOT"
python3 -B web.py
```

Open [http://127.0.0.1:8767](http://127.0.0.1:8767). Put selected material in the source directory and run `python3 -B maintenance.py sync files`. Additional directories can be configured through `RESEARCH_IDEAS_RESEARCH_PATHS` and `RESEARCH_IDEAS_COURSE_PATHS`, using your platform's path separator.

- **Search:** Full-text search is ready by default. To enable semantic search, run `ollama pull bge-m3`, then `python3 -B maintenance.py vectors 100`.
- **Zotero:** Enable Zotero's local API and run `python3 -B maintenance.py sync zotero`. Build optional helpers with `swiftc pdf.swift -o pdftext` and `swiftc vision.swift -o vision-ocr`.
- **Feishu:** Put manual exports in the state directory's `imports/feishu/`, or use your own authorized CLI via `RESEARCH_IDEAS_FEISHU_CLI`, `RESEARCH_IDEAS_FEISHU_PROFILE`, and `RESEARCH_IDEAS_FEISHU_HOST`.
- **Academic chats:** Place selected exports in `imports/chatgpt/` under the state directory, or provide JSON on stdin to `capture_chat.py` with at least `title` and `quote`. Import only what you intend to keep.
- **Backups:** `python3 -B maintenance.py backup` creates and validates a SQLite copy. Pass your own sync location via `RESEARCH_IDEAS_PORTABLE` for portable snapshots and backup history.

The repository contains application source and starts with an empty database. Keep imported material, databases, and backups separate from source control. Run the tools only on devices and materials you are authorized to use.
