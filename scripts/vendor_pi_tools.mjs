// Reproducible, narrow extraction of official Pi 1.5.0 tool definitions. No Coding Agent session runtime.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import ts from '../worker/node_modules/typescript/lib/typescript.js';
const root = path.resolve('reports/ultra-goal-stage3/sdk-inspect/package/dist/core/tools');
const target = path.resolve('worker/vendor/pi-tools');
fs.mkdirSync(target, { recursive: true });
const files = ['read', 'write', 'edit', 'find', 'grep', 'bash', 'edit-diff', 'truncate', 'file-mutation-queue'];
const manifest = { package: '@earendil-works/pi-coding-agent', version: '1.5.0', files: [] };
const printer = ts.createPrinter({ newLine: ts.NewLineKind.LineFeed, removeComments: false });
const dropFunctions = new Set(['createReadTool','createWriteTool','createEditTool','createFindTool','createGrepTool','createBashTool', 'createLocalShellOperations', 'createLocalBashOperations', 'computeEditsDiff', 'computeEditDiff']);
for (const name of files) {
  const source = fs.readFileSync(path.join(root, name + '.js'), 'utf8');
  const parsed = ts.createSourceFile(name + '.js', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const transformer = context => {
    const visit = node => {
      if (ts.isImportDeclaration(node)) {
        const spec = node.moduleSpecifier.text;
        if (['typebox', 'diff', './edit-diff.js', './truncate.js', './file-mutation-queue.js'].includes(spec)) return node;
        if (['path', './path-utils.js', '../../utils/text.js', 'node:path'].includes(spec)) {
          return context.factory.updateImportDeclaration(node, node.modifiers, node.importClause, context.factory.createStringLiteral('./remote-path.js'), node.attributes);
        }
        if (spec === './output-accumulator.js') return node;
        return undefined;
      }
      if (ts.isFunctionDeclaration(node) && dropFunctions.has(node.name?.text)) return undefined;
      if (ts.isVariableStatement(node) && node.declarationList.declarations.some(d => d.name.getText(parsed).startsWith('default'))) return undefined;
      if (ts.isSpreadAssignment(node) && /Renderers|renderers/.test(node.expression.getText(parsed))) return undefined;
      if (ts.isFunctionDeclaration(node) && node.name?.text === 'getMutationQueueKey') {
        return context.factory.updateFunctionDeclaration(node, node.modifiers, node.asteriskToken, node.name, node.typeParameters, node.parameters, node.type,
          context.factory.createBlock([context.factory.createReturnStatement(context.factory.createCallExpression(context.factory.createIdentifier('resolve'), undefined, [context.factory.createIdentifier('filePath')]))], true));
      }
      // The find factory's documented custom glob branch becomes mandatory; strip its local fd backend.
      if (name === 'find' && ts.isBlock(node)) {
        const i = node.statements.findIndex(s => ts.isIfStatement(s) && s.expression.getText(parsed) === 'customOps?.glob');
        if (i >= 0) return context.factory.updateBlock(node, [...node.statements.slice(0, i).map(s => ts.visitNode(s, visit)), ...ts.visitNode(node.statements[i].thenStatement, visit).statements]);
      }
      // Keep official grep schema/description; execution is the explicit Kali rg adapter, never local spawn.
      if (name === 'grep' && ts.isMethodDeclaration(node) && node.name.getText(parsed) === 'execute') {
        return context.factory.updateMethodDeclaration(node, node.modifiers, node.asteriskToken, node.name, node.questionToken, node.typeParameters, node.parameters, node.type,
          context.factory.createBlock([context.factory.createThrowStatement(context.factory.createNewExpression(context.factory.createIdentifier('Error'), undefined,
            [context.factory.createStringLiteral('Remote grep execute must be installed')]))], true));
      }
      if (name === 'read' && ts.isIfStatement(node) && node.expression.getText(parsed) === 'mimeType') {
        return context.factory.updateIfStatement(node, node.expression,
          context.factory.createThrowStatement(context.factory.createNewExpression(context.factory.createIdentifier('Error'), undefined, [context.factory.createStringLiteral('Remote image decoding is unavailable; original file retained')])), ts.visitNode(node.elseStatement, visit));
      }
      return ts.visitEachChild(node, visit, context);
    };
    return node => ts.visitNode(node, visit);
  };
  const result = ts.transform(parsed, [transformer]);
  let output = printer.printFile(result.transformed[0]).replace(/\/\/# sourceMappingURL=.*\n?/g, '');
  output = output.replaceAll('options?.operations ?? defaultReadOperations', 'options.operations')
    .replaceAll('options?.operations ?? defaultWriteOperations', 'options.operations')
    .replaceAll('options?.operations ?? defaultEditOperations', 'options.operations')
    .replaceAll('customOps ?? defaultFindOperations', 'customOps')
    .replace('options?.operations ?? createLocalBashOperations({ shellPath: options?.shellPath })', 'options.operations');
  if (name === 'bash') output = 'import { getShellEnv } from "./remote-path.js";\nconst BASH_UPDATE_THROTTLE_MS = 100;\n' + output;
  const header = '// Derived from @earendil-works/pi-coding-agent v1.5.0 (MIT). See provenance.json and LICENSE.\n';
  fs.writeFileSync(path.join(target, name + '.js'), header + output, 'utf8');
  manifest.files.push({ name, source_sha256: crypto.createHash('sha256').update(source).digest('hex'), result_sha256: crypto.createHash('sha256').update(header + output).digest('hex') });
  result.dispose();
}
fs.writeFileSync(path.join(target, 'provenance.json'), JSON.stringify(manifest, null, 2) + '\n', 'utf8');
