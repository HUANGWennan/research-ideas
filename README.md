# 研究思想库 · Research Ideas

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
