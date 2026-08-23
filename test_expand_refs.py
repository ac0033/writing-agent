"""expand_file_refs 的测试集。

重点覆盖多引用场景（历史上"多个文件只读到第一个"的回归），
兼测单文件、目录、带空格文件名、标点紧邻、找不到等边界。

用法：uv run python test_expand_refs.py
退出码 0 = 全部通过；1 = 有用例失败。
"""
import sys

# Windows 老终端是 GBK 编码，print emoji 会崩；测试输出只要求不崩
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(errors="replace")

from main import expand_file_refs

A = "agent的演变层次.md"                       # 无空格文件名（topic/ 下）
B = "Agent 协议生态与适用边界.md"               # 带空格文件名（topic/ 下）
C = "How to build robust agentic workflow.md"  # topic/blog1/ 下

# 目录引用会读到的文件数：topic 递归 4 个，blog1 共 2 个
TOPIC_FILES, BLOG1_FILES = 4, 2

CASES = [
    # (用例名, 输入, 期望读到的文件数, 必须出现的文件名列表)
    # ---- 多文件：本次回归的核心 ----
    ("两文件-空格分隔",        f"参考@{A} 和 @{B}",            2, [A, B]),
    ("两文件-中文逗号分隔",    f"参考@{A}，@{B}",              2, [A, B]),
    ("两文件-顿号分隔",        f"参考@{A}、@{B}",              2, [A, B]),
    ("两文件-无分隔符",        f"@{A}@{B}",                    2, [A, B]),
    ("两文件-换行分隔",        f"@{A}\n@{B}",                  2, [A, B]),
    ("两文件-第一个后跟汉字",  f"参考@{A}和@{B}的内容",        2, [A, B]),
    ("三个文件-混在句子里",    f"先看@{A}，再看@{B}，最后看@{C}", 3, [A, B, C]),
    ("同文件引用两次",         f"@{A} 还有 @{A}",              2, [A]),
    # ---- 文件与目录混合 ----
    ("文件+目录",              f"@{A} 和 @blog1",              1 + BLOG1_FILES, [A, C]),
    ("目录+目录",              f"@topic 和 @blog1",            TOPIC_FILES + BLOG1_FILES, [A]),
    ("目录+文件+目录",         f"@blog1 加上 @{A} 再加上 @topic",
     BLOG1_FILES + 1 + TOPIC_FILES, [A]),
    # ---- 单引用回归 ----
    ("单文件-裸引用",          f"@{A}",                        1, [A]),
    ("单文件-后跟句号",        f"参考@{A}。",                  1, [A]),
    ("带空格文件名-后跟逗号",  f"看看@{B},再写",               1, [B]),
    ("目录-裸引用",            f"@topic",                      TOPIC_FILES, [A, B, C]),
    ("目录-相对路径",          f"@topic/blog1",                BLOG1_FILES, [C]),
    ("目录-后跟标点",          f"@topic。再加上@blog1，谢谢",  TOPIC_FILES + BLOG1_FILES, [A]),
    # ---- 异常与边界 ----
    ("找不到-保留原文",        "@不存在的文件.md 和 @不存在的目录", 0, []),
    ("无引用-原样返回",        "没有引用的普通文本",            0, []),
    ("含PDF的目录-跳过二进制",  "@corpus",                      0, []),
]


def main() -> int:
    failed = 0
    for name, text, want_count, want_names in CASES:
        out = expand_file_refs(text)
        got_count = out.count("【引用文件：")
        ok = got_count == want_count and all(
            f"【引用文件：{n}】" in out for n in want_names)
        if want_count == 0:
            ok = ok and "【引用文件：" not in out
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{status}] {name}：期望 {want_count} 个文件，实际 {got_count} 个")
    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
