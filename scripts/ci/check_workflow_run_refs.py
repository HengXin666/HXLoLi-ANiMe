#!/usr/bin/env python3
"""Lint on.workflow_run.workflows 引用: 每条引用必须能命中仓库里某个真实 workflow name.

GitHub 按 glob (minimatch 语义) 解析 workflow_run.workflows, 因此 workflow name 里的
[ ] * ? 必须在引用侧转义 (YAML 单引号内写一个反斜杠), 否则字符类会吞掉方括号并导致
触发解析失败: 每次任意 workflow 完成都会刷一条 name 为空, path 为工作流文件,
conclusion=startup_failure 的幽灵 run, 而该 consumer 永远不触发.

本脚本只做机械校验, 不改任何文件: 收集全部 workflow 的 name, 对每条引用按 glob
语义匹配; 匹配不上即报错, 并给出该写成什么 (转义后的字面量, 强调 YAML 单引号).
"""
import argparse
import fnmatch
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失属于工具错误
    yaml = None

WORKFLOW_DIR = '.github/workflows'
# 需要转义才能在引用侧当字面量匹配的字符; 与 shell glob 一致, 反斜杠本身也要转义
ESCAPABLE = frozenset('*?[]\\')
DETAIL_LIMIT = 10


def arguments(argv=None):
    parser = argparse.ArgumentParser(description='校验 on.workflow_run.workflows 的 glob 引用能否命中真实 workflow name')
    parser.add_argument('--repo', type=Path, default=Path.cwd(), help='仓库根目录, 默认当前目录')
    parser.add_argument('--ref', help='改为校验某个 Git 提交的 workflow 文件, 而不读工作区')
    parser.add_argument('--json', type=Path, help='完整 JSON 诊断路径, 默认写入 Git 目录 reports/workflow-run-refs.json')
    return parser.parse_args(argv)


def diagnostic(rule, path, message, line=1, severity='error', related=None):
    return {'rule': rule, 'path': path, 'line': line, 'message': message,
            'severity': severity, 'related': related or []}


def git(repo, *args):
    completed = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode('utf-8', 'replace').strip() or 'git ' + args[0] + ' 失败'
        raise ValueError(detail.replace('\n', ' '))
    return completed.stdout


def workflow_sources(repo, ref=None):
    """返回 [(仓库相对路径, 文件内容)]; ref 为空时读工作区."""
    if ref:
        names = git(repo, 'ls-tree', '-r', '--name-only', ref, '--', WORKFLOW_DIR).decode().splitlines()
        return [(name, git(repo, 'show', ref + ':' + name).decode()) for name in names if name.endswith(('.yml', '.yaml'))]
    root = repo / WORKFLOW_DIR
    if not root.is_dir():
        raise ValueError('缺少目录: ' + WORKFLOW_DIR)
    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix in ('.yml', '.yaml'))
    return [(p.relative_to(repo).as_posix(), p.read_text(encoding='utf-8')) for p in files]


def mapping_child(node, key):
    """按 key 的字面文本取子节点; 兼容 PyYAML 把裸 on 解析成布尔 True 的情况."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None


def scalar(node):
    if isinstance(node, yaml.ScalarNode):
        return node.value
    return None


def translate(reference):
    """把 GitHub glob 引用编译成 (正则, 纯字面量或 None, 是否含 glob 运算符).

    fnmatch 不支持反斜杠转义, 而 workflow name 里的 [ ] 必须靠反斜杠转义,
    所以含转义时用本翻译器, 纯 glob 时用 fnmatch 交叉校验, 二者语义一致.
    """
    parts, plain, has_glob = [], [], False
    index, length = 0, len(reference)
    while index < length:
        char = reference[index]
        if char == '\\' and index + 1 < length:
            parts.append(re.escape(reference[index + 1]))
            plain.append(reference[index + 1])
            index += 2
            continue
        if char == '*':
            parts.append('.*')
            has_glob = True
            index += 1
            continue
        if char == '?':
            parts.append('.')
            has_glob = True
            index += 1
            continue
        if char == '[':
            cursor, negate, body = index + 1, False, ''
            if cursor < length and reference[cursor] == '!':
                negate, cursor = True, cursor + 1
            while cursor < length:
                if reference[cursor] == ']' and body:
                    break
                body += reference[cursor]
                cursor += 1
            else:
                # 未闭合的方括号按字面量处理, 与 minimatch 的宽松行为一致
                parts.append(re.escape(char))
                plain.append(char)
                index += 1
                continue
            has_glob = True
            parts.append('[' + ('^' if negate else '') + ''.join('\\' + c if c in '\\^-]' else c for c in body) + ']')
            index = cursor + 1
            continue
        parts.append(re.escape(char))
        plain.append(char)
        index += 1
    return re.compile('(?s:' + ''.join(parts) + r')\Z'), (None if has_glob else ''.join(plain)), has_glob


def bracket_classes(reference):
    """列出引用里未转义的字符类; 返回 (开始下标, 类体积字符串) 列表."""
    classes, index, length = [], 0, len(reference)
    while index < length:
        if reference[index] == '\\':
            index += 2
            continue
        if reference[index] != '[':
            index += 1
            continue
        cursor, body = index + 1, ''
        if cursor < length and reference[cursor] == '!':
            cursor += 1
        while cursor < length and not (reference[cursor] == ']' and body):
            body += reference[cursor]
            cursor += 1
        if cursor >= length:
            break
        classes.append((index, body))
        index = cursor + 1
    return classes


def matches(name, reference):
    """按 glob 语义判断引用能否命中 name; 纯 glob 走 fnmatch, 含转义走翻译器."""
    if '\\' not in reference:
        return fnmatch.fnmatchcase(name, reference)
    pattern, _, _ = translate(reference)
    return pattern.match(name) is not None


def escape_name(name):
    return ''.join('\\' + char if char in ESCAPABLE else char for char in name)


def unescape(reference):
    return re.sub(r'\\(.)', r'\1', reference, flags=re.DOTALL)


def suggest(name):
    return ("把 name 里的 glob 元字符逐个转义并放进 YAML 单引号: '"
            + escape_name(name) + "' (单引号内只写一个反斜杠)")


def collect(repo, ref=None):
    issues, workflows, references = [], [], []
    for path, text in workflow_sources(repo, ref):
        try:
            document = yaml.compose(text)
        except yaml.YAMLError as exc:
            issues.append(diagnostic('yaml-parse', path, 'YAML 解析失败, 无法校验: ' + str(exc).replace('\n', ' ')))
            continue
        if not isinstance(document, yaml.MappingNode):
            issues.append(diagnostic('yaml-parse', path, '顶层不是映射, 无法校验'))
            continue
        name_node = mapping_child(document, 'name')
        name = scalar(name_node[1]).strip() if name_node and scalar(name_node[1]) else None
        # 没有 name 时 GitHub 用仓库相对文件路径当 workflow name, 这里按同一规则登记
        declared = name is not None
        workflows.append({'path': path, 'name': name or path,
                          'line': name_node[0].start_mark.line + 1 if name_node else 1,
                          'name_source': 'name' if declared else 'path',
                          'name_glob_meta': sorted(set(name or '') & ESCAPABLE)})
        if not declared:
            issues.append(diagnostic('workflow-name-missing', path,
                                     '缺少 name, GitHub 会退化为文件路径作为 workflow name; 请显式命名',
                                     severity='warning'))
        name = name or path
        trigger = mapping_child(document, 'on') or mapping_child(document, 'true')
        if trigger is None:
            continue
        run = mapping_child(trigger[1], 'workflow_run')
        if run is None:
            continue
        flows = mapping_child(run[1], 'workflows')
        if flows is None:
            continue
        items = flows[1].value if isinstance(flows[1], yaml.SequenceNode) else [flows[1]]
        for item in items:
            reference = scalar(item)
            line = item.start_mark.line + 1
            if reference is None:
                issues.append(diagnostic('workflow-ref-invalid', path, 'workflows 条目不是字符串', line))
                continue
            references.append({'consumer': name, 'consumer_path': path, 'line': line,
                                'reference': reference})
    return workflows, references, issues


def check(repo, ref=None):
    workflows, references, issues = collect(repo, ref)
    names = {w['name']: w for w in workflows if w['name']}
    for workflow in workflows:
        if workflow['name_glob_meta']:
            meta = ''.join(workflow['name_glob_meta'])
            issues.append(diagnostic('workflow-name-glob-meta', workflow['path'],
                                     f'workflow name 含 glob 元字符 {meta}, '
                                     '消费方必须逐个转义, 否则会静默失配或触发解析失败',
                                     workflow['line'], severity='warning'))
    duplicates = Counter(w['name'] for w in workflows if w['name'])
    for name, count in sorted(duplicates.items()):
        if count > 1:
            paths = [w['path'] for w in workflows if w['name'] == name]
            issues.append(diagnostic('workflow-name-duplicate', paths[0],
                                     'workflow name 重复, workflow_run 无法区分来源: ' + ', '.join(paths),
                                     severity='warning', related=paths[1:]))
    for entry in references:
        reference = entry['reference']
        hit = [name for name in sorted(names) if matches(name, reference)]
        _, plain, has_glob = translate(reference)
        entry.update(matched=bool(hit), matched_workflows=hit, unescaped=unescape(reference),
                     glob=has_glob, literal=plain, suggestion=None)
        if hit:
            literal_brackets = [body for _, body in bracket_classes(reference) if set(body) <= {'[', ']'}]
            if literal_brackets:
                # [[... ]] 这类"用字符类转义方括号"的写法在 fnmatch 下能命中, 但 GitHub 实测解析失败
                entry['suggestion'] = suggest(hit[0])
                issues.append(diagnostic('workflow-ref-bracket-class', entry['consumer_path'],
                                         f'引用 {reference!r} 用字符类转义方括号; 该写法在真实触发解析中'
                                         '会失败 (startup_failure 幽灵 run), 请改为反斜杠转义: '
                                         + entry['suggestion'], entry['line']))
            continue
        # 去掉反斜杠后能等于某个真实 name, 说明只是转义写错/漏写
        literal = unescape(reference)
        if literal in names:
            entry['suggestion'] = suggest(literal)
            message = f'引用 {reference!r} 匹配不到任何 workflow name; ' \
                      f'它去掉反斜杠后等于真实 name, {entry["suggestion"]}'
        elif literal.replace('\\', '') in {n.replace('\\', '') for n in names}:
            guess = next(n for n in names if n.replace('\\', '') == literal.replace('\\', ''))
            entry['suggestion'] = suggest(guess)
            message = f'引用 {reference!r} 匹配不到任何 workflow name; ' \
                      f'疑似反斜杠写法有误, {entry["suggestion"]}'
        else:
            message = f'引用 {reference!r} 匹配不到任何 workflow name (按 glob 语义解析); ' \
                      f'可用 name: {", ".join(sorted(names)) or "(空)"}'
        issues.append(diagnostic('workflow-ref-unmatched', entry['consumer_path'], message, entry['line'],
                                 related=sorted(names)))
    return {'version': 2, 'repo': str(repo), 'ref': ref, 'workflows': workflows,
            'references': references, 'issues': issues, 'ok': not issues}


# 门禁入口: 保存完整诊断后按问题总数决定终端展示粒度
# .agents/notes/implemented/process/2026-11-10-workflow-run-refs-gate.md
def main(argv=None):
    args = arguments(argv)
    repo = Path(args.repo).resolve()
    try:
        if yaml is None:
            raise ImportError('缺少 PyYAML, 无法解析 workflow')
        result = check(repo, args.ref)
        code = 1 if any(issue['severity'] == 'error' for issue in result['issues']) else 0
    except (OSError, ValueError, UnicodeError) as exc:
        result = {'version': 2, 'ok': False, 'repo': str(repo), 'ref': args.ref,
                  'workflows': [], 'references': [],
                  'issues': [diagnostic('gate-error', WORKFLOW_DIR, '门禁无法完成: ' + str(exc))]}
        code = 2
    try:
        output = args.json
        if output is None:
            location = git(repo, 'rev-parse', '--path-format=absolute', '--git-path',
                           'reports/workflow-run-refs.json').decode().strip()
            output = Path(location)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError) as exc:
        print('Workflow Run Refs: 1 项错误, 类型 report-write-error, 无法保存完整报告: ' + str(exc))
        return 2
    issues = result['issues']
    if len(issues) <= DETAIL_LIMIT:
        for issue in issues:
            level = '错误' if issue['severity'] == 'error' else '警告'
            print(f'{level} {issue["path"]}:{issue["line"]} [{issue["rule"]}] {issue["message"]}')
    else:
        print(f'问题总数 {len(issues)} 超过 {DETAIL_LIMIT} 条, 仅展示数量与类型统计')
    errors = sum(issue['severity'] == 'error' for issue in issues)
    warnings = len(issues) - errors
    status = '通过' if result['ok'] else '仅有警告' if code == 0 else '需处理' if code == 1 else '工具错误'
    print(f'Workflow Run Refs: {status}, {errors} 项错误, {warnings} 项警告, '
          f'{len(result["workflows"])} 个工作流, {len(result["references"])} 条引用')
    counts = Counter(issue['rule'] for issue in issues)
    if counts:
        print('类型: ' + ', '.join(f'{rule}={count}' for rule, count in sorted(counts.items())))
    print('完整报告: ' + str(output.resolve()))
    return code


if __name__ == '__main__':
    sys.exit(main())
