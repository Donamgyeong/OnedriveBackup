"""Fail when project Python sources use known post-3.8 language/runtime features."""

import ast
import sys
from pathlib import Path


SOURCE_DIRS = ("onedrive_backup", "tests", "packaging", "tools")
POST_38_ATTRIBUTES = {"is_relative_to", "removeprefix", "removesuffix"}
POST_38_MODULES = {"tomllib", "zoneinfo"}
BUILTIN_GENERICS = {"list", "dict", "set", "tuple", "frozenset", "type"}


class CompatibilityVisitor(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.issues = []

    def issue(self, node, message):
        self.issues.append("{}:{}: {}".format(self.path, getattr(node, "lineno", 1), message))

    def check_annotation(self, node):
        for child in ast.walk(node):
            if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                self.issue(child, "PEP 604 union annotations require Python 3.10")
            if (isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name)
                    and child.value.id in BUILTIN_GENERICS):
                self.issue(child, "built-in generic annotations require Python 3.9")

    def visit_AnnAssign(self, node):
        self.check_annotation(node.annotation)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        annotations = [arg.annotation for arg in node.args.args + node.args.kwonlyargs if arg.annotation]
        if node.args.vararg and node.args.vararg.annotation:
            annotations.append(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            annotations.append(node.args.kwarg.annotation)
        if node.returns:
            annotations.append(node.returns)
        for annotation in annotations:
            self.check_annotation(annotation)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node):
        if node.attr in POST_38_ATTRIBUTES:
            self.issue(node, "{}.{} is not available in Python 3.8".format("object", node.attr))
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split(".")[0] in POST_38_MODULES:
                self.issue(node, "{} is not in the Python 3.8 standard library".format(alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split(".")[0] in POST_38_MODULES:
            self.issue(node, "{} is not in the Python 3.8 standard library".format(node.module))
        self.generic_visit(node)


def scan(root):
    root = Path(root)
    issues = []
    for directory in SOURCE_DIRS:
        for path in sorted((root / directory).glob("**/*.py")):
            source = path.read_text(encoding="utf-8-sig")
            try:
                tree = ast.parse(source, filename=str(path), feature_version=8)
            except SyntaxError as exc:
                issues.append("{}:{}: syntax is not valid in Python 3.8: {}".format(
                    path, exc.lineno or 1, exc.msg))
                continue
            visitor = CompatibilityVisitor(path)
            visitor.visit(tree)
            issues.extend(visitor.issues)
    return issues


def main():
    root = Path(__file__).resolve().parent.parent
    issues = scan(root)
    for issue in issues:
        print(issue)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
