"""不调用模型、不改会话，检查当前管道运行所需路径。"""
import json
import config


def inspect() -> dict:
    missing = [str(config.PROMPTS_DIR / "skills" / f"{name}.md")
               for name in {n for names in config.SKILL_ROLES.values() for n in names}
               if not (config.PROMPTS_DIR / "skills" / f"{name}.md").is_file()]
    from pathlib import Path
    return {"required_skills_missing": missing, "wiki_exists": config.WIKI_DIR.is_dir(),
            "external_style_exists": config.HUMAN_WRITING_SKILL_PATH.is_file(),
            "style_fallback_available": (config.PROMPTS_DIR / "skills/human-writing.md").is_file(),
            "blog_repository": config.BLOG_REPO_PATH,
            "blog_exists": Path(config.BLOG_REPO_PATH).is_dir() if config.BLOG_REPO_PATH else False,
            "blog_posts_directory": config.BLOG_POSTS_DIR, "blog_branch": config.BLOG_BRANCH,
            "memory_enabled": config.MEMORY_ENABLED, "memory_scope": "repo:writing-topic-<topic_id>",
            "publishing_requires_separate_confirmation": True}


if __name__ == "__main__":
    result = inspect()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result["required_skills_missing"] else 0)
