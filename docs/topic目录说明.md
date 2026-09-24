# topic 与 output 目录

`topic/` 与 `output/` 都是用户数据，不随仓库发布（已 gitignore）。两个目录按主题一一对应：**每个主题一个目录，同名**。

```text
topic/<主题目录>/
  topic.json        主题归属：topic_id、当前标题、aliases（旧任务按标题得到的 id）
  README.md         可选：这个主题的说明与版本一览
  sources/          写作管道保存的输入材料（writing_prepare_topic / writing_start_v2 / 终端页面）
  materials/        原始材料
  process/<版本>/   某一版的写作过程：框架、稿件、审核、运行脚本与任务登记

output/<主题目录>/
  versions.json     版本线日志：每版的版本号、上一版、时间、来源、状态、正文 SHA256
  v1/ v2/ …         每版一个快照：article.md（确认稿）、thinking.md、evidence.json、reading.md、publication.json
```

## 一个主题一条版本线

同一主题的成稿只在线尾追加，不分叉，类似 git 提交：写作管道保存确认稿时，自动在 `output/<主题目录>/` 里建立下一个版本（最新版本号加一），并在 `versions.json` 记下上一版是谁。同一次运行重复保存同一正文（节点重试）不会重复追加。管道外改定的稿件（如人工或其他工具修订后发布）也应作为下一个版本接到这条线上，而不是另起目录。

主题按 `topic_id` 归属，不按标题：改了标题，新稿仍接在原来那条线后面。启动任务时给出 `topic_id`（小写字母、数字、连字符）最稳妥；没给时按标题生成哈希 id，目录名取自标题。`topic.json` 的 `aliases` 让旧标题得到的 id 也归到同一目录。

## 放置约定

- 顶层只放主题目录，不放散落的素材、脚本或状态文件。
- 新材料能归入已有主题就放入对应主题；不属于现有主题再新建。目录不用预先建空，有内容时再建立。
- 过程目录按版本命名（`process/v3/`），版本内部再按材料、稿件、审核和运行记录分类；没有产出版本的尝试用日期命名（`process/2026-09-21-acceptance/`）。
- 任务仍在运行时不要移动它的目录；移动后同步更新任务登记簿里的路径（`topic_file`、`output_path`）。
