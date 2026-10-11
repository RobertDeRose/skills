# skills

Agent skills by Robert DeRose, packaged for the [`skills`](https://github.com/vercel-labs/skills) CLI.

## Install

```bash
# List the skills in this repository
npx skills add RobertDeRose/skills --list

# Install one skill
npx skills add RobertDeRose/skills --skill gh-pr-review
```

## Skills

| Skill | Description |
|-------|-------------|
| [`gh-pr-review`](skills/gh-pr-review/SKILL.md) | Collect, assess, fix, and resolve bot and human feedback on the current branch's GitHub PR, then ask before requesting another Copilot, CodeRabbit, or Greptile review. |

## Layout

Each skill lives in `skills/<name>/` with a `SKILL.md` (YAML frontmatter with `name` and `description`) plus any
supporting scripts.

## Development

`gh-pr-review` scripts use only the Python 3.13+ standard library. Run its tests with:

```bash
python3 -m unittest discover -s skills/gh-pr-review/scripts
```

## License

[MIT](LICENSE)
