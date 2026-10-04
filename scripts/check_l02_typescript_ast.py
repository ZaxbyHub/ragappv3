"""Small TypeScript compiler API bridge shared by the #773 capability guards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


_WALKER = r'''
const ts = require("typescript");
const fs = require("fs");
const files = JSON.parse(fs.readFileSync(0, "utf8"));
const result = { files: [], errors: [] };

function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node))) node = node.expression;
  return node;
}
function propertyName(node) {
  if (!node) return null;
  if (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  return null;
}
function literalBracketKey(node) {
  node = unwrap(node);
  return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) ? node.text : null;
}
function line(node) {
  return node.getSourceFile().getLineAndCharacterOfPosition(node.getStart()).line + 1;
}
function makeScope(parent) { return { parent, bindings: new Map() }; }
function lookup(scope, name) {
  for (let current = scope; current; current = current.parent) {
    if (current.bindings.has(name)) return current.bindings.get(name);
  }
  return null;
}
function bindingScope(scope, name) {
  for (let current = scope; current; current = current.parent) {
    if (current.bindings.has(name)) return current;
  }
  return scope;
}
function isLexicalBlock(node) { return ts.isBlock(node) || ts.isCatchClause(node); }
function objectKeys(object) {
  const keys = [];
  let unsupported = false;
  for (const member of object.properties) {
    if (ts.isPropertyAssignment(member) || ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) {
      const key = propertyName(member.name);
      if (key === null) unsupported = true;
      else keys.push({ key, line: line(member) });
    } else if (ts.isShorthandPropertyAssignment(member)) {
      keys.push({ key: member.name.text, line: line(member) });
    } else {
      unsupported = true;
    }
  }
  return { keys, unsupported };
}
function fixtureFacts(sf) {
  const direct = [];
  const unsupported = [];
  function bindPatternAsUnknown(scope, pattern) {
    if (ts.isIdentifier(pattern)) {
      scope.bindings.set(pattern.text, { kind: "unknown" });
      return;
    }
    if (ts.isObjectBindingPattern(pattern) || ts.isArrayBindingPattern(pattern)) {
      for (const element of pattern.elements) {
        if (ts.isBindingElement(element)) bindPatternAsUnknown(scope, element.name);
      }
    }
  }
  function declare(scope, name, initializer, node, isRest = false) {
    const value = unwrap(initializer);
    if (!name) return;
    if (isRest || !value || !ts.isObjectLiteralExpression(value)) {
      scope.bindings.set(name, { kind: "unknown" });
      return;
    }
    const facts = objectKeys(value);
    scope.bindings.set(name, { kind: "object", keys: facts.keys, unsupported: facts.unsupported });
  }
  function consumeBinding(scope, name, node) {
    const binding = lookup(scope, name);
    if (!binding || binding.kind !== "object" || binding.unsupported) {
      unsupported.push(node.getStart());
      return;
    }
    direct.push(...binding.keys);
  }
  function visit(node, scope) {
    if (ts.isFunctionLike(node)) {
      const inner = makeScope(scope);
      for (const parameter of node.parameters) {
        if (ts.isIdentifier(parameter.name)) declare(inner, parameter.name.text, parameter.initializer, parameter, !!parameter.dotDotDotToken);
        else bindPatternAsUnknown(inner, parameter.name);
      }
      ts.forEachChild(node, child => visit(child, inner));
      return;
    }
    if (isLexicalBlock(node)) {
      const inner = makeScope(scope);
      ts.forEachChild(node, child => visit(child, inner));
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      if (ts.isIdentifier(node.name)) declare(scope, node.name.text, node.initializer, node);
      else bindPatternAsUnknown(scope, node.name);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isIdentifier(node.left)) {
      bindingScope(scope, node.left.text).bindings.set(node.left.text, { kind: "unknown" });
    }
    if (ts.isPropertyAssignment(node) && propertyName(node.name) === "limits") {
      const value = unwrap(node.initializer);
      if (value && ts.isObjectLiteralExpression(value)) {
        const facts = objectKeys(value);
        direct.push(...facts.keys);
        if (facts.unsupported) unsupported.push(node.getStart());
      } else if (value && ts.isIdentifier(value)) {
        consumeBinding(scope, value.text, node);
      } else {
        unsupported.push(node.getStart());
      }
    }
    if (ts.isShorthandPropertyAssignment(node) && node.name.text === "limits") {
      consumeBinding(scope, node.name.text, node);
    }
    ts.forEachChild(node, child => visit(child, scope));
  }
  visit(sf, makeScope(null));
  return { fixtureKeys: direct, fixtureUnsupported: unsupported.length > 0 };
}
function directLimitsExpression(node) {
  node = unwrap(node);
  if (!node) return false;
  if (ts.isPropertyAccessExpression(node)) return node.name.text === "limits";
  if (ts.isElementAccessExpression(node)) return literalBracketKey(node.argumentExpression) === "limits";
  return false;
}
function bindingKeys(pattern) {
  const keys = [];
  let unsupported = false;
  for (const element of pattern.elements) {
    if (ts.isOmittedExpression(element) || !ts.isBindingElement(element)) { unsupported = true; continue; }
    if (element.dotDotDotToken || ts.isObjectBindingPattern(element.name) || ts.isArrayBindingPattern(element.name)) { unsupported = true; continue; }
    const key = propertyName(element.propertyName || element.name);
    if (key === null) unsupported = true;
    else keys.push({ key, line: line(element) });
  }
  return { keys, unsupported };
}
function readerFacts(sf) {
  const reads = [];
  const unsupported = [];
  function aliasState(scope, name) { return lookup(scope, name); }
  function bindPatternAsOther(scope, pattern) {
    if (ts.isIdentifier(pattern)) {
      scope.bindings.set(pattern.text, "other");
      return;
    }
    if (ts.isObjectBindingPattern(pattern) || ts.isArrayBindingPattern(pattern)) {
      for (const element of pattern.elements) {
        if (ts.isBindingElement(element)) bindPatternAsOther(scope, element.name);
      }
    }
  }
  function limitsExpression(node, scope) {
    node = unwrap(node);
    if (!node) return false;
    if (ts.isIdentifier(node)) return aliasState(scope, node.text) === "limits";
    return directLimitsExpression(node);
  }
  function record(node, key) { reads.push({ key, line: line(node) }); }
  function directLimitsUseIsSupported(node) {
    let expression = node;
    let parent = expression.parent;
    while (parent && (ts.isParenthesizedExpression(parent) || ts.isAsExpression(parent) || ts.isTypeAssertionExpression(parent) || ts.isNonNullExpression(parent) || ts.isSatisfiesExpression(parent)) && parent.expression === expression) {
      expression = parent;
      parent = expression.parent;
    }
    if (!parent) return false;
    if ((ts.isPropertyAccessExpression(parent) || ts.isElementAccessExpression(parent)) && parent.expression === expression) return true;
    if (ts.isVariableDeclaration(parent) && parent.initializer === expression && (ts.isIdentifier(parent.name) || ts.isObjectBindingPattern(parent.name))) return true;
    return ts.isBinaryExpression(parent) && parent.operatorToken.kind === ts.SyntaxKind.EqualsToken && parent.right === expression && ts.isIdentifier(parent.left);
  }
  function aliasEscapes(node, scope) {
    const parent = node.parent;
    if (!parent) return false;
    if ((ts.isPropertyAccessExpression(parent) || ts.isElementAccessExpression(parent)) && parent.expression === node) return false;
    if (ts.isVariableDeclaration(parent) && parent.name === node) return false;
    if (ts.isBinaryExpression(parent) && parent.left === node && parent.operatorToken.kind === ts.SyntaxKind.EqualsToken) return false;
    return true;
  }
  function visit(node, scope) {
    if (ts.isFunctionLike(node)) {
      const inner = makeScope(scope);
      for (const parameter of node.parameters) {
        bindPatternAsOther(inner, parameter.name);
      }
      ts.forEachChild(node, child => visit(child, inner));
      return;
    }
    if (isLexicalBlock(node)) {
      const inner = makeScope(scope);
      ts.forEachChild(node, child => visit(child, inner));
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      if (ts.isIdentifier(node.name)) {
        scope.bindings.set(node.name.text, directLimitsExpression(node.initializer) ? "limits" : "other");
      } else if (ts.isObjectBindingPattern(node.name)) {
        bindPatternAsOther(scope, node.name);
        if (limitsExpression(node.initializer, scope)) {
          const facts = bindingKeys(node.name);
          reads.push(...facts.keys);
          if (facts.unsupported) unsupported.push(node.getStart());
        }
      } else if (ts.isArrayBindingPattern(node.name)) {
        bindPatternAsOther(scope, node.name);
        if (limitsExpression(node.initializer, scope)) unsupported.push(node.getStart());
      }
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isIdentifier(node.left)) {
      const target = bindingScope(scope, node.left.text);
      if (directLimitsExpression(node.right)) target.bindings.set(node.left.text, "limits");
      else if (aliasState(scope, node.left.text) === "limits") unsupported.push(node.getStart());
    }
    if (directLimitsExpression(node) && !directLimitsUseIsSupported(node)) {
      unsupported.push(node.getStart());
    }
    if (ts.isPropertyAccessExpression(node) && limitsExpression(node.expression, scope)) {
      record(node, node.name.text);
    }
    if (ts.isElementAccessExpression(node) && limitsExpression(node.expression, scope)) {
      const key = literalBracketKey(node.argumentExpression);
      if (key === null) unsupported.push(node.getStart());
      else record(node, key);
    }
    if (ts.isIdentifier(node) && aliasState(scope, node.text) === "limits" && aliasEscapes(node, scope)) {
      unsupported.push(node.getStart());
    }
    ts.forEachChild(node, child => visit(child, scope));
  }
  visit(sf, makeScope(null));
  return { readerKeys: reads, readerUnsupported: unsupported.length > 0 };
}
for (const file of files) {
  let source;
  try { source = fs.readFileSync(file, "utf8"); } catch (error) { result.errors.push(`${file}: ${error.message}`); continue; }
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  if (sf.parseDiagnostics && sf.parseDiagnostics.length) { result.errors.push(`${file}: TypeScript syntax could not be parsed`); continue; }
  const fixture = fixtureFacts(sf);
  const reader = readerFacts(sf);
  result.files.push({
    file,
    fixtureKeys: fixture.fixtureKeys,
    readerKeys: reader.readerKeys,
    fixtureUnsupported: fixture.fixtureUnsupported,
    readerUnsupported: reader.readerUnsupported,
  });
}
console.log(JSON.stringify(result));
'''


def run_typescript_facts(root: Path, files: list[Path]) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("required Node.js executable is unavailable")
    env = os.environ.copy()
    package_root = root / "frontend" / "node_modules"
    node_path = [str(package_root)]
    if env.get("NODE_PATH"):
        node_path.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(node_path)
    process = subprocess.run(
        [node, "-e", _WALKER],
        cwd=str(root), env=env, input=json.dumps([str(path) for path in files]),
        capture_output=True, text=True, timeout=120, check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"TypeScript AST walker failed: {detail[-4000:]}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"TypeScript AST walker returned invalid JSON: {process.stdout[-4000:]}") from exc
    if result.get("errors"):
        raise RuntimeError("; ".join(result["errors"]))
    return result
